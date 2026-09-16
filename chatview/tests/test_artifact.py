import sys
import unittest
from unittest.mock import MagicMock

class MockRegion:
    def __init__(self, a, b):
        self._a = min(a, b)
        self._b = max(a, b)

    def contains(self, point):
        if isinstance(point, (int, float)):
            return self._a <= point <= self._b
        return False

    def begin(self):
        return self._a

    def end(self):
        return self._b

mock_sublime = MagicMock()
mock_sublime.Region = MockRegion
mock_sublime.DRAW_NO_FILL = 1
mock_sublime.DRAW_NO_OUTLINE = 2
mock_sublime.HIDDEN = 4
mock_sublime.ENCODED_POSITION = 1
sys.modules.setdefault("sublime", mock_sublime)

from chatview.artifact import FileChangesArtifact, is_agent_data_path


class TestArtifact(unittest.TestCase):

    def setUp(self):
        self.mock_view = MagicMock()
        self.mock_window = MagicMock()
        self.input_start_fn = lambda view: 100
        self.artifact = FileChangesArtifact(self.mock_view, self.mock_window, self.input_start_fn)

    def test_record_and_show(self):
        self.artifact.record("/path/to/project/file1.py", "file1.py", "+new line\n-old line\n")
        self.artifact.record("/path/to/project/file2.py", "file2.py", "+another line\n")

        self.assertEqual(len(self.artifact.pending_changed_files), 2)
        self.artifact.show()

        # Check append was called with formatted text
        self.mock_view.run_command.assert_called_once()
        args, kwargs = self.mock_view.run_command.call_args
        self.assertEqual(args[0], "term_chat_output_append")
        text = args[1]["text"]
        self.assertIn("▣ 2 files changed", text)
        self.assertIn("file1.py  +1 -1", text)
        self.assertIn("file2.py  +1", text)

        # Check that pending files were cleared after show()
        self.assertEqual(len(self.artifact.pending_changed_files), 0)

    def test_skip_agent_data_dir(self):
        import os
        claude_path = os.path.expanduser("~/.claude/memory.json")
        self.artifact.record(claude_path, "memory.json", "+some memory\n")
        self.assertEqual(len(self.artifact.pending_changed_files), 0)

    def test_pi_message_processor_artifact(self):

        from chatview.chatprocessor import PiMessageProcessor
        from genfoundry.base_agent import Message

        mock_session = MagicMock()
        mock_session.agent_thread.cwd = "/workspace"
        processor = PiMessageProcessor(mock_session)

        # 1. Assistant message with tool_use edit
        assistant_msg = Message(
            "assistant",
            content=[{
                "type": "tool_use",
                "name": "edit",
                "id": "call_123",
                "arguments": {
                    "path": "/workspace/src/app.py",
                    "oldText": "def old():\n    pass\n",
                    "newText": "def new():\n    pass\n"
                }
            }]
        )
        processor.handle_message(assistant_msg)

        # 2. Message_end with toolResult
        result_msg = Message(
            "message_end",
            content={
                "role": "toolResult",
                "toolName": "edit",
                "toolCallId": "call_123",
                "isError": False,
                "details": {"firstChangedLine": 1}
            }
        )
        processor.handle_message(result_msg)

        # Verify session.record_file_change was called
        mock_session.record_file_change.assert_called_once()
        abs_p, rel_p, diff_t = mock_session.record_file_change.call_args[0]
        self.assertEqual(abs_p, "/workspace/src/app.py")
        self.assertEqual(rel_p, "src/app.py")
        self.assertIn("-def old():", diff_t)
        self.assertIn("+def new():", diff_t)

        # 3. Turn result
        res_msg = Message("result", content={"success": True})
        processor.handle_message(res_msg)
        mock_sublime.set_timeout.assert_called_with(mock_session.show_file_changes_artifact, 0)

    def test_opencode_message_processor_artifact(self):
        from chatview.chatprocessor import OpenCodeMessageProcessor
        from genfoundry.base_agent import Message

        mock_session = MagicMock()
        mock_session.agent_thread.cwd = "/workspace"
        processor = OpenCodeMessageProcessor(mock_session)

        # 1. tool_use edit message
        edit_msg = Message(
            "tool_use",
            content={
                "name": "edit",
                "input": {
                    "filePath": "/workspace/README.md",
                    "oldString": "# Title\n",
                    "newString": "# New Title\n",
                }
            }
        )
        processor.handle_message(edit_msg)

        # Verify session.record_file_change was called
        mock_session.record_file_change.assert_called_once()
        abs_p, rel_p, diff_t = mock_session.record_file_change.call_args[0]
        self.assertEqual(abs_p, "/workspace/README.md")
        self.assertEqual(rel_p, "README.md")
        self.assertIn("-# Title", diff_t)
        self.assertIn("+# New Title", diff_t)

        # 2. Stop message
        stop_msg = Message("stop", content={})
        processor.handle_message(stop_msg)
        mock_sublime.set_timeout.assert_called_with(mock_session.show_file_changes_artifact, 0)


    def test_antigravity_message_processor_plan_artifact(self):
        from chatview.chatprocessor import AntigravityMessageProcessor
        from genfoundry.base_agent import Message

        mock_session = MagicMock()
        mock_session.agent_thread.cwd = "/workspace"
        mock_session.agent_thread.anthropic_config = {"plan_mode": True}
        processor = AntigravityMessageProcessor(mock_session)

        # 1. tool_use write_to_file for an implementation plan artifact
        plan_code = "# Implementation Plan\n\n1. Refactor auth\n2. Add tests\n"
        artifact_msg = Message(
            "tool_use",
            content={
                "name": "write_to_file",
                "parameters": {
                    "TargetFile": "/workspace/.gemini/brain/conv1/implementation_plan.md",
                    "CodeContent": plan_code,
                    "ArtifactMetadata": {
                        "RequestFeedback": True,
                        "Summary": "Auth refactoring steps",
                        "UserFacing": True
                    }
                }
            }
        )
        processor.handle_message(artifact_msg)

        # File change should be skipped for plan artifacts
        mock_session.record_file_change.assert_not_called()

        # Tool block formatting should indicate CreateArtifact
        formatted = processor._format_tool_block(artifact_msg.content)
        self.assertIn("CreateArtifact", formatted)
        self.assertIn("implementation_plan.md", formatted)
        self.assertIn("Auth refactoring steps", formatted)

        # Verify record_plan_artifact was called on mock_session
        mock_session.record_plan_artifact.assert_called()

    def test_file_changes_artifact_with_plan(self):
        # 1. Record 1 plan and 1 file change
        self.artifact.record_plan(
            "/workspace/brain/implementation_plan.md",
            "implementation_plan.md",
            summary="Refactor auth",
            content="# Plan Content",
            can_execute=True
        )
        self.artifact.record("/workspace/src/app.py", "src/app.py", "+new line\n")

        self.assertEqual(len(self.artifact.pending_plan_artifacts), 1)
        self.assertEqual(len(self.artifact.pending_changed_files), 1)

        # 2. Show artifact
        self.artifact.show()

        # Check append was called with formatted text
        self.mock_view.run_command.assert_called_once()
        args, kwargs = self.mock_view.run_command.call_args
        self.assertEqual(args[0], "term_chat_output_append")
        text = args[1]["text"]

        self.assertIn("▣ implementation_plan.md", text)
        self.assertNotIn("[Execute]", text)
        self.assertIn("src/app.py  +1", text)

        # 3. Check plan regions: view only (no [Execute] display)
        exec_regions = [r for r in self.artifact.plan_regions if r[3] == "execute"]
        view_regions = [r for r in self.artifact.plan_regions if r[3] == "view"]
        self.assertEqual(len(exec_regions), 0)
        self.assertEqual(len(view_regions), 1)

        # 4. Test clicking view
        view_point = view_regions[0][0].begin()
        self.assertFalse(self.artifact.is_execute_at(view_point))
        self.assertTrue(self.artifact.is_plan_at(view_point))
        mock_new_view = MagicMock()
        self.mock_window.new_file.return_value = mock_new_view
        self.mock_window.views.return_value = []
        handled_view = self.artifact.open_artifact_at(view_point)
        self.assertTrue(handled_view)
        self.mock_window.new_file.assert_called_once()
        mock_new_view.set_name.assert_called_with("implementation_plan.md")

    def test_plan_execute_action_confirmation(self):
        # Test confirmation dialog when an execute action is invoked
        mock_sublime.ok_cancel_dialog.reset_mock()
        mock_sublime.ok_cancel_dialog.return_value = True

        self.artifact.plan_regions = [
            (MockRegion(10, 20), "/workspace/brain/plan.md", {"name": "plan.md"}, "execute")
        ]
        self.assertTrue(self.artifact.is_execute_at(15))
        handled = self.artifact.open_artifact_at(15)
        self.assertTrue(handled)
        mock_sublime.ok_cancel_dialog.assert_called_with("Implement plan.md?", "Execute")
        self.mock_window.run_command.assert_called_with("term_chat_implement_plan")

        # Cancellation test
        self.mock_window.run_command.reset_mock()
        mock_sublime.ok_cancel_dialog.return_value = False
        handled = self.artifact.open_artifact_at(15)
        self.assertTrue(handled)
        self.mock_window.run_command.assert_not_called()

    def test_file_changes_artifact_with_walkthrough(self):
        # Record 1 plan, 1 walkthrough, and 1 code file
        self.artifact.record_plan(
            "/workspace/brain/implementation_plan.md",
            "implementation_plan.md",
            summary="Refactor auth",
            content="# Plan Content",
            can_execute=True
        )
        self.artifact.record_plan(
            "/workspace/brain/walkthrough.md",
            "walkthrough.md",
            summary="Auth verified successfully",
            content="# Walkthrough Content",
            can_execute=False,
            icon="📖"
        )
        self.artifact.record("/workspace/src/app.py", "src/app.py", "+new line\n")

        self.assertEqual(len(self.artifact.pending_plan_artifacts), 2)
        self.assertEqual(len(self.artifact.pending_changed_files), 1)

        self.artifact.show()

        self.mock_view.run_command.assert_called_once()
        text = self.mock_view.run_command.call_args[0][1]["text"]

        self.assertIn("▣ implementation_plan.md", text)
        self.assertIn("▣ walkthrough.md", text)
        self.assertNotIn("[Execute]", text)

        # Plan regions should all be view regions (no [Execute])
        exec_regions = [r for r in self.artifact.plan_regions if r[3] == "execute"]
        view_regions = [r for r in self.artifact.plan_regions if r[3] == "view"]
        self.assertEqual(len(exec_regions), 0)
        self.assertEqual(len(view_regions), 2)

    def test_file_changes_artifact_plan_and_walkthrough_only(self):
        # 1 plan, 1 walkthrough, 0 files
        self.artifact.record_plan(
            "/workspace/brain/implementation_plan.md",
            "implementation_plan.md",
            summary="Refactor auth",
            content="# Plan Content",
            can_execute=True
        )
        self.artifact.record_plan(
            "/workspace/brain/walkthrough.md",
            "walkthrough.md",
            summary="Auth verified successfully",
            content="# Walkthrough Content",
            can_execute=False,
            icon="📖"
        )
        self.artifact.show()

        self.mock_view.run_command.assert_called_once()
        text = self.mock_view.run_command.call_args[0][1]["text"]
        self.assertIn("▣ implementation_plan.md", text)
        self.assertIn("▣ walkthrough.md", text)
        self.assertNotIn("[Execute]", text)
        self.assertNotIn("file", text)

    def test_antigravity_processor_walkthrough_artifact(self):
        from chatview.chatprocessor import AntigravityMessageProcessor
        from genfoundry.base_agent import Message

        mock_session = MagicMock()
        mock_session.chat_view.settings.return_value.get.return_value = "test-session-123"
        processor = AntigravityMessageProcessor(mock_session)

        walkthrough_msg = Message(
            "tool_use",
            content={
                "id": "137",
                "name": "write_to_file",
                "parameters": {
                    "TargetFile": "/Users/sino/.gemini/antigravity-cli/brain/test-session-123/walkthrough.md",
                    "CodeContent": "# Verification Summary\n\nAll tests passed."
                }
            }
        )
        processor.handle_message(walkthrough_msg)

        # File change should NOT be recorded as workspace code diff
        mock_session.record_file_change.assert_not_called()

        # Tool block formatting should indicate CreateArtifact
        formatted = processor._format_tool_block(walkthrough_msg.content)
        self.assertIn("CreateArtifact", formatted)
        self.assertIn("walkthrough.md", formatted)

        # Verify record_plan_artifact was called with walkthrough.md and can_execute=False
        mock_session.record_plan_artifact.assert_called()
        call_args = mock_session.record_plan_artifact.call_args
        self.assertEqual(call_args[0][1], "walkthrough.md")
        self.assertEqual(call_args[1].get("can_execute"), False)
        self.assertEqual(call_args[1].get("icon"), "📖")

    def test_file_changes_artifact_priority_sorting(self):
        # Record in reverse order: other, walkthrough, implementation_plan
        self.artifact.record_plan(
            "/workspace/brain/notes.md",
            "notes.md",
            summary="Some notes",
            can_execute=False,
            icon="📖"
        )
        self.artifact.record_plan(
            "/workspace/brain/walkthrough.md",
            "walkthrough.md",
            summary="Walkthrough",
            can_execute=False,
            icon="📖"
        )
        self.artifact.record_plan(
            "/workspace/brain/implementation_plan.md",
            "implementation_plan.md",
            summary="Plan",
            can_execute=True
        )

        self.artifact.show()

        self.mock_view.run_command.assert_called_once()
        text = self.mock_view.run_command.call_args[0][1]["text"]

        # implementation_plan must be first, walkthrough second, notes third
        plan_idx = text.find("▣ implementation_plan.md")
        walk_idx = text.find("▣ walkthrough.md")
        notes_idx = text.find("▣ notes.md")

        self.assertNotEqual(plan_idx, -1)
        self.assertNotEqual(walk_idx, -1)
        self.assertNotEqual(notes_idx, -1)
        self.assertLess(plan_idx, walk_idx)
        self.assertLess(walk_idx, notes_idx)


    def test_antigravity_processor_mtime_deduplication(self):
        import tempfile
        import os
        from chatview.chatprocessor import AntigravityMessageProcessor

        mock_session = MagicMock()
        mock_session.agent_thread.session_id = "session-test-dup"
        processor = AntigravityMessageProcessor(mock_session)

        with tempfile.TemporaryDirectory() as tmpdir:
            brain_dir = os.path.join(tmpdir, "brain", "session-test-dup")
            os.makedirs(brain_dir, exist_ok=True)
            b_plan = os.path.join(brain_dir, "implementation_plan.md")
            with open(b_plan, "w") as f:
                f.write("# Brain Plan\n\nContent")

            # Sync with custom brain directory
            with unittest.mock.patch("os.path.expanduser", return_value=os.path.join(tmpdir, "brain")):
                # 1. First sync must record the plan artifact
                processor._sync_user_artifacts()
                self.assertEqual(mock_session.record_plan_artifact.call_count, 1)

                # 2. Sync again with no modification - must NOT record again
                mock_session.record_plan_artifact.reset_mock()
                processor._sync_user_artifacts()
                mock_session.record_plan_artifact.assert_not_called()

                # 3. Modify the file - mtime increases - must record again
                with open(b_plan, "w") as f:
                    f.write("# Updated Brain Plan\n\nNew Content")
                future_mtime = os.path.getmtime(b_plan) + 5
                os.utime(b_plan, (future_mtime, future_mtime))

                mock_session.record_plan_artifact.reset_mock()
                processor._sync_user_artifacts()
                self.assertEqual(mock_session.record_plan_artifact.call_count, 1)

    def test_open_diff_fallback_to_source_file(self):
        # When diffs is empty (e.g. Antigravity edit tools or multi_replace), open source file directly
        self.artifact.record("/workspace/src/app.py", "src/app.py", "")
        self.artifact.show()

        file_region = self.artifact.file_regions[0][0]
        self.mock_window.open_file.reset_mock()

        handled = self.artifact.open_artifact_at(file_region.begin())
        self.assertTrue(handled)
        self.mock_window.open_file.assert_called_once_with("/workspace/src/app.py")

if __name__ == "__main__":
    unittest.main()
