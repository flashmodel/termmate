import sys
import unittest
from unittest.mock import MagicMock
import re

# Sublime Text supplies this module at plugin runtime. A mock keeps this
# test runnable with ordinary Python.
mock_sublime = MagicMock()
mock_sublime.ENCODED_POSITION = 1
sys.modules.setdefault("sublime", mock_sublime)

from chatview.diffview import (
    format_numbered_diff,
    format_raw_unified_diff,
    strip_numbered_diff,
    handle_diff_view_click,
    parse_numbered_line,
    _NUMBERED_LINE_RE,
    _HUNK_HEADER_RE,
    _DIFF_HEADER_RE,
)


class TestDiffView(unittest.TestCase):

    def test_format_numbered_diff_basic(self):
        old_text = "line 1\nline 2 (old)\nline 3\n"
        new_text = "line 1\nline 2 (new)\nline 3\n"
        formatted = format_numbered_diff(old_text, new_text, "test.py", context=2)

        self.assertIn("diff a/test.py b/test.py", formatted)
        self.assertIn("--- a/test.py", formatted)
        self.assertIn("+++ b/test.py", formatted)
        self.assertIn("@@ -1,3 +1,3 @@", formatted)
        # Context line 1
        self.assertIn("  1   1  line 1", formatted)
        # Deleted line 2
        self.assertIn("  2     -line 2 (old)", formatted)
        # Added line 2
        self.assertIn("      2 +line 2 (new)", formatted)
        # Context line 3
        self.assertIn("  3   3  line 3", formatted)

    def test_format_raw_unified_diff_multi_hunk(self):
        raw_diff = (
            "diff a/example.py b/example.py\n"
            "--- a/example.py\n"
            "+++ b/example.py\n"
            "@@ -10,3 +10,4 @@\n"
            " line 10\n"
            "-line 11 old\n"
            "+line 11 new\n"
            "+line 11.5 added\n"
            " line 12\n"
            "@@ -50,3 +51,2 @@\n"
            " line 50\n"
            "-line 51 deleted\n"
            " line 52\n"
        )
        formatted = format_raw_unified_diff(raw_diff, "example.py")

        self.assertIn(" 10  10  line 10", formatted)
        self.assertIn(" 11     -line 11 old", formatted)
        self.assertIn("     11 +line 11 new", formatted)
        self.assertIn("     12 +line 11.5 added", formatted)
        self.assertIn(" 12  13  line 12", formatted)

        # Second hunk
        self.assertIn(" 50  51  line 50", formatted)
        self.assertIn(" 51     -line 51 deleted", formatted)
        self.assertIn(" 52  52  line 52", formatted)

    def test_strip_numbered_diff(self):
        old_text = "def hello():\n    print('old')\n"
        new_text = "def hello():\n    print('new')\n"
        formatted = format_numbered_diff(old_text, new_text, "hello.py")
        stripped = strip_numbered_diff(formatted)

        self.assertIn("--- a/hello.py", stripped)
        self.assertIn("+++ b/hello.py", stripped)
        self.assertIn("@@ -1,2 +1,2 @@", stripped)
        self.assertIn("-    print('old')", stripped)
        self.assertIn("+    print('new')", stripped)
        self.assertIn(" def hello():", stripped)

    def test_parse_numbered_line(self):
        # Context line
        res_ctx = parse_numbered_line("  42  42  def unchanged():")
        self.assertIsNotNone(res_ctx)
        old_no, new_no, sign, content = res_ctx
        self.assertEqual(old_no, 42)
        self.assertEqual(new_no, 42)
        self.assertEqual(sign, " ")
        self.assertEqual(content, "def unchanged():")

        # Deletion line
        res_del = parse_numbered_line("  43     -def removed()")
        self.assertIsNotNone(res_del)
        old_no, new_no, sign, content = res_del
        self.assertEqual(old_no, 43)
        self.assertIsNone(new_no)
        self.assertEqual(sign, "-")
        self.assertEqual(content, "def removed()")

        # Addition line
        res_add = parse_numbered_line("      43 +def added()")
        self.assertIsNotNone(res_add)
        old_no, new_no, sign, content = res_add
        self.assertIsNone(old_no)
        self.assertEqual(new_no, 43)
        self.assertEqual(sign, "+")
        self.assertEqual(content, "def added()")

    def test_handle_diff_view_click_numbered_lines(self):
        # Mock sublime view & window
        mock_view = MagicMock()
        mock_window = MagicMock()
        mock_view.window.return_value = mock_window

        # 1. Click on added line 43
        mock_view.substr.return_value = "      43 +def added()"
        mock_view.line.return_value = MagicMock()
        
        import sublime
        # Test with a mock abs path
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".py") as tf:
            handled = handle_diff_view_click(mock_view, tf.name, 100)
            self.assertTrue(handled)
            mock_window.open_file.assert_called_with(f"{tf.name}:43", sublime.ENCODED_POSITION)

        # 2. Click on deleted line 42
        mock_window.reset_mock()
        mock_view.substr.return_value = "  42     -def removed()"
        with tempfile.NamedTemporaryFile(suffix=".py") as tf:
            handled = handle_diff_view_click(mock_view, tf.name, 100)
            self.assertTrue(handled)
            mock_window.open_file.assert_called_with(f"{tf.name}:42", sublime.ENCODED_POSITION)

        # 3. Click on hunk banner
        mock_window.reset_mock()
        mock_view.substr.return_value = "        @@ -42,5 +50,6 @@"
        with tempfile.NamedTemporaryFile(suffix=".py") as tf:
            handled = handle_diff_view_click(mock_view, tf.name, 100)
            self.assertTrue(handled)
            mock_window.open_file.assert_called_with(f"{tf.name}:50", sublime.ENCODED_POSITION)


if __name__ == "__main__":
    unittest.main()
