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


if __name__ == "__main__":
    unittest.main()
