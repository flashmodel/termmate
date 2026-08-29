import sys
import unittest
from unittest.mock import MagicMock

mock_sublime = MagicMock()
mock_sublime.Region = lambda a, b: (a, b)
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


if __name__ == "__main__":
    unittest.main()

