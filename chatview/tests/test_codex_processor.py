import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# Sublime Text supplies this module at plugin runtime. A mock keeps this
# formatter test runnable with ordinary Python.
sys.modules.setdefault("sublime", MagicMock())

from chatview.chatprocessor import (
    CodexMessageProcessor,
    OpenCodeMessageProcessor,
    _parse_markdown_file_target,
)
from genfoundry.base_agent import Message


class TestMarkdownFileTargetParsing(unittest.TestCase):

    @patch("chatview.chatprocessor.os.path.isfile", return_value=True)
    def test_parses_windows_targets(self, _isfile):
        cases = [
            (r"C:\project\src\app.py:12:5",
             (r"C:\project\src\app.py", 12, 5)),
            ("file:///C:/project/src/app.py#L12C5",
             ("C:/project/src/app.py", 12, 5)),
            ("file://server/share/app.py#L8",
             ("//server/share/app.py", 8, None)),
        ]
        for target, expected in cases:
            with self.subTest(target=target):
                self.assertEqual(
                    _parse_markdown_file_target(target, "/worktree"),
                    expected,
                )


class TestCodexFileChangeFormatting(unittest.TestCase):

    def test_formats_multi_file_change_from_app_server_log(self):
        session = SimpleNamespace(
            agent_thread=SimpleNamespace(
                cwd="/Users/sino/workbench/codeform",
            ),
            cwd="/Users/sino/workbench/codeform",
        )
        processor = CodexMessageProcessor(session)
        block = {
            "name": "fileChange",
            "changes": [
                {
                    "path": (
                        "/Users/sino/workbench/codeform/"
                        "chatview/chatprocessor.py"
                    ),
                    "kind": {"type": "update", "move_path": None},
                    "diff": (
                        "@@ -4,2 +4,3 @@\n"
                        " import re\n"
                        "+import urllib.parse\n"
                        " import xml.etree.ElementTree\n"
                    ),
                },
                {
                    "path": (
                        "/Users/sino/workbench/codeform/"
                        "chatview/chatview.py"
                    ),
                    "kind": {"type": "update", "move_path": None},
                    "diff": (
                        "@@ -2165,2 +2165,5 @@\n"
                        '                         return ("noop", {})\n'
                        "+                    if session.message_processor."
                        "open_local_file_link(\n"
                        "+                            line_text, window, view, "
                        "click_point):\n"
                        '+                        return ("noop", {})\n'
                    ),
                },
            ],
            "status": "completed",
        }

        output = processor._format_tool_block(block)

        expected = (
            "⏺ fileChange chatview/chatprocessor.py#L4\n"
            "  ````diff\n"
            "  @@ -4,2 +4,3 @@\n"
            "   import re\n"
            "  +import urllib.parse\n"
            "   import xml.etree.ElementTree\n"
            "  ````\n\n"
            "⏺ fileChange chatview/chatview.py#L2165\n"
            "  ````diff\n"
            "  @@ -2165,2 +2165,5 @@\n"
            '                           return ("noop", {})\n'
            "  +                    if session.message_processor."
            "open_local_file_link(\n"
            "  +                            line_text, window, view, "
            "click_point):\n"
            '  +                        return ("noop", {})\n'
            "  ````"
        )
        self.assertEqual(output, expected)


class TestCodexToolCallFormatting(unittest.TestCase):

    def setUp(self):
        session = SimpleNamespace(
            agent_thread=SimpleNamespace(
                cwd="/Users/sino/workbench/codeform",
            ),
            cwd="/Users/sino/workbench/codeform",
        )
        self.processor = CodexMessageProcessor(session)

    def test_formats_mcp_tool_call(self):
        output = self.processor._format_tool_block({
            "name": "mcpToolCall",
            "server": "github",
            "tool": "search_repositories",
            "arguments": {"query": "codex", "limit": 10},
        })

        self.assertEqual(
            output,
            "⏺ github·search_repositories (query: codex, limit: 10)",
        )

    def test_formats_dynamic_tool_call_without_namespace(self):
        output = self.processor._format_tool_block({
            "name": "dynamicToolCall",
            "tool": "choose_design",
            "arguments": {"theme": "editorial"},
        })

        self.assertEqual(
            output,
            "⏺ choose_design (theme: editorial)",
        )

    def test_formats_dynamic_tool_namespace(self):
        output = self.processor._format_tool_block({
            "name": "dynamicToolCall",
            "namespace": "design",
            "tool": "choose",
            "arguments": {"theme": "editorial"},
        })

        self.assertEqual(
            output,
            "⏺ design·choose (theme: editorial)",
        )

    def test_formats_collab_agent_tool_call(self):
        output = self.processor._format_tool_block({
            "name": "collabAgentToolCall",
            "tool": "spawnAgent",
            "receiverThreadIds": ["thread-2"],
            "prompt": "Inspect tests",
            "model": "gpt-5",
            "reasoningEffort": "high",
        })

        self.assertEqual(
            output,
            "⏺ spawnAgent (agents: thread-2, prompt: Inspect tests, "
            "model: gpt-5, reasoning: high)",
        )

    def test_formats_web_search(self):
        output = self.processor._format_tool_block({
            "name": "webSearch",
            "query": "Codex app-server protocol",
        })

        self.assertEqual(
            output,
            "⏺ webSearch (Codex app-server protocol)",
        )

    def test_formats_web_search_action_detail(self):
        output = self.processor._format_tool_block({
            "name": "webSearch",
            "query": "",
            "action": {
                "type": "findInPage",
                "url": "https://example.com/docs",
                "pattern": "tool call",
            },
        })

        self.assertEqual(
            output,
            "⏺ webSearch ('tool call' in https://example.com/docs)",
        )

    def test_formats_image_view_with_relative_path(self):
        output = self.processor._format_tool_block({
            "name": "imageView",
            "path": "/Users/sino/workbench/codeform/assets/preview.png",
        })

        self.assertEqual(output, "⏺ imageView assets/preview.png")

    def test_formats_image_generation(self):
        output = self.processor._format_tool_block({
            "name": "imageGeneration",
            "status": "completed",
        })

        self.assertEqual(output, "⏺ imageGeneration")


class TestOpenCodeMessageProcessor(unittest.TestCase):

    def setUp(self):
        self.session = SimpleNamespace(
            agent_thread=SimpleNamespace(
                cwd="/Users/sino/workbench/codeform",
                anthropic_config={"plan_mode": False},
            ),
            cwd="/Users/sino/workbench/codeform",
            start_loading=MagicMock(),
            stop_loading=MagicMock(),
            show_file_changes_artifact=MagicMock(),
            record_file_change=MagicMock(),
            update_last_prompt_uuid=MagicMock(),
        )
        self.processor = OpenCodeMessageProcessor(self.session)

    def test_renders_streaming_text_directly(self):
        with patch.object(self.processor, "append_content") as append:
            self.processor.handle_message(Message("text", "hello"))

        self.session.start_loading.assert_called_once_with()
        append.assert_called_once_with("hello")

    def test_attaches_server_generated_user_message_id_to_prompt(self):
        self.processor.handle_message(Message(
            "user_message_id",
            {"message_id": "msg_server_generated"},
        ))

        self.session.update_last_prompt_uuid.assert_called_once_with(
            "msg_server_generated"
        )
        self.assertEqual(self.processor._active_turn_id, "msg_server_generated")

    def test_formats_completed_file_tool(self):
        output = self.processor._format_tool_block({
            "name": "read",
            "input": {
                "filePath": (
                    "/Users/sino/workbench/codeform/"
                    "genfoundry/opencode_agent.py"
                ),
            },
            "status": "completed",
        })

        self.assertEqual(output, "⏺ read genfoundry/opencode_agent.py")

    def test_formats_read_offset_as_line_number(self):
        output = self.processor._format_tool_block({
            "name": "read",
            "input": {
                "filePath": (
                    "/Users/sino/workbench/codeform/"
                    "genfoundry/opencode_agent.py"
                ),
                "offset": 205,
                "limit": 50,
            },
            "status": "completed",
        })

        self.assertEqual(output, "⏺ read genfoundry/opencode_agent.py#L205")

    def test_formats_edit_diff_start_as_line_number(self):
        output = self.processor._format_tool_block({
            "name": "edit",
            "input": {
                "filePath": (
                    "/Users/sino/workbench/codeform/"
                    "chatview/chatprocessor.py"
                ),
                "oldString": "old",
                "newString": "new",
            },
            "metadata": {
                "diff": (
                    "Index: chatview/chatprocessor.py\n"
                    "===============================================\n"
                    "--- chatview/chatprocessor.py\n"
                    "+++ chatview/chatprocessor.py\n"
                    "@@ -807,2 +812,3 @@\n"
                    "-old\n+new\n"
                ),
            },
            "status": "completed",
        })

        self.assertEqual(
            output,
            "⏺ edit chatview/chatprocessor.py#L812\n"
            "  ````diff\n"
            "  @@ -807,2 +812,3 @@\n"
            "  -old\n"
            "  +new\n"
            "  ````",
        )

    def test_formats_completed_shell_tool(self):
        output = self.processor._format_tool_block({
            "name": "command_execution",
            "command": "python3 -m unittest",
            "status": "completed",
        })

        self.assertEqual(output, "⏺ command (python3 -m unittest)")


if __name__ == "__main__":
    unittest.main()
