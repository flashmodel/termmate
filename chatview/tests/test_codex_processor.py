import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# Sublime Text supplies this module at plugin runtime. A mock keeps this
# formatter test runnable with ordinary Python.
sys.modules.setdefault("sublime", MagicMock())

from chatview.chatprocessor import (
    CodexMessageProcessor,
    _parse_markdown_file_target,
)


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
            "⏺ fileChange chatview/chatprocessor.py#L4\n\n"
            "````diff\n"
            "@@ -4,2 +4,3 @@\n"
            " import re\n"
            "+import urllib.parse\n"
            " import xml.etree.ElementTree\n"
            "````\n\n"
            "⏺ fileChange chatview/chatview.py#L2165\n\n"
            "````diff\n"
            "@@ -2165,2 +2165,5 @@\n"
            '                         return ("noop", {})\n'
            "+                    if session.message_processor."
            "open_local_file_link(\n"
            "+                            line_text, window, view, "
            "click_point):\n"
            '+                        return ("noop", {})\n'
            "````"
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


if __name__ == "__main__":
    unittest.main()
