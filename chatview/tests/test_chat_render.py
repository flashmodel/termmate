import sys
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatrender import extract_diff_fold_ranges
from chatview.md_render import MarkdownFormatter


class TestDiffFolding(unittest.TestCase):

    def test_fold_diff_block_over_limit_zero_preview(self):
        text = (
            "```diff\n"
            "+line 1\n"
            "+line 2\n"
            "+line 3\n"
            "+line 4\n"
            "```\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=2, preview_lines=0)
        # fold starts right after ```diff (before the newline), through end of closing fence
        self.assertEqual(len(ranges), 1)
        start, end = ranges[0]
        self.assertEqual(text[start:end], "\n+line 1\n+line 2\n+line 3\n+line 4\n```")

    def test_fold_diff_block_with_preview_lines(self):
        text = (
            "  ```diff\n"
            "  +line 1\n"
            "  +line 2\n"
            "  +line 3\n"
            "  +line 4\n"
            "  ```\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=2, preview_lines=2)
        self.assertEqual(len(ranges), 1)
        start, end = ranges[0]
        # offset + min(opening_indent, line_indent), which starts after the 2-space indent
        self.assertEqual(text[start:end], "+line 3\n  +line 4\n  ```")

    def test_fold_tool_call_diff_indented_four(self):
        # Tool-call diffs (_render_diff_block) are indented 4 spaces
        text = (
            "    ````diff\n"
            "    +line 1\n"
            "    +line 2\n"
            "    +line 3\n"
            "    ````\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=2, preview_lines=1)
        self.assertEqual(len(ranges), 1)
        start, end = ranges[0]
        self.assertEqual(text[start:end], "+line 2\n    +line 3\n    ````")

    def test_ignores_under_limit(self):
        text = (
            "```diff\n"
            "+line 1\n"
            "+line 2\n"
            "```\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=3, preview_lines=0)
        self.assertEqual(ranges, [])

    def test_ignores_unclosed_fence(self):
        text = (
            "```diff\n"
            "+line 1\n"
            "+line 2\n"
            "+line 3\n"
        )
        ranges = extract_diff_fold_ranges(text, limit=1, preview_lines=0)
        self.assertEqual(ranges, [])


class TestMarkdownFormatterListIndent(unittest.TestCase):

    def setUp(self):
        self.list_indent = None  # None: setting not set, so the default
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            self.list_indent if k == "list_indent" and self.list_indent is not None else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

    def fmt(self, text):
        # Whole lines, as the chat feeds it while streaming (flush=True would drop the last newline)
        return MarkdownFormatter().format(text)

    def test_items_indented_by_default(self):
        text = "**Why it works better**\n\n- **Clean repo.** One\n- Two\n1. Three\n\nDone.\n"
        self.assertEqual(
            self.fmt(text),
            "**Why it works better**\n    - **Clean repo.** One\n    - Two\n    1. Three\n\nDone.\n")

    def test_nested_items_and_continuation_lines(self):
        text = "- One\n  - Nested\n  continues here\n\n  Second paragraph\n- Two\n"
        self.assertEqual(
            self.fmt(text),
            "    - One\n      - Nested\n      continues here\n\n      Second paragraph\n    - Two\n")

    def test_left_edge_text_ends_the_list(self):
        text = "- One\nplain\n  indented but not in a list\n## Heading\n"
        self.assertEqual(self.fmt(text), "    - One\nplain\n  indented but not in a list\n## Heading\n")

    def test_not_a_list(self):
        text = "**bold** and -dash and 2026-10-23\n---\n*emphasis* text\n"
        self.assertEqual(self.fmt(text), text)

    def test_code_block_is_left_alone(self):
        text = "```yaml\n- a: 1\n- b: 2\n```\n- after\n"
        self.assertEqual(self.fmt(text), "```yaml\n- a: 1\n- b: 2\n```\n    - after\n")

    def test_code_block_inside_item_moves_with_it(self):
        text = "- Run:\n  ```bash\n  ls\n\n  pwd\n  ```\n- Next\n\n```text\n- plain\n```\n"
        self.assertEqual(
            self.fmt(text),
            "    - Run:\n      ```bash\n      ls\n\n      pwd\n      ```\n    - Next\n\n```text\n- plain\n```\n")

    def test_streamed_in_pieces_matches_one_shot(self):
        text = "Intro\n\n- One\n  - Nested\n  ```sh\n  ls\n  ```\n- Two\n"
        formatter = MarkdownFormatter()
        streamed = "".join(formatter.format(text[i:i + 3]) for i in range(0, len(text), 3))
        streamed += formatter.format("", flush=True)  # nothing left over: the text ends with a newline
        self.assertEqual(streamed, self.fmt(text))

    def test_blank_line_before_a_list_dropped(self):
        # Text, a blank, a list: the blank goes, the list sits under the text (with or without ":")
        text = "What I edited:\n\n- One\n- Two\n\nDone.\n"
        self.assertEqual(self.fmt(text), "What I edited:\n    - One\n    - Two\n\nDone.\n")
        self.assertEqual(self.fmt("Intro\n\n- One\n"), "Intro\n    - One\n")
        self.assertEqual(self.fmt("**Bold intro:**\n\n1. One\n"), "**Bold intro:**\n    1. One\n")
        # Several blank lines become none; a held blank at the very end of the reply is dropped (a trailing
        # blank line is dropped on flush anyway)
        self.assertEqual(self.fmt("Edited:\n\n\n- One\n"), "Edited:\n    - One\n")
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("Note:\n\n") + formatter.format("", flush=True), "Note:\n")
        # A blank line inside a list, and one after it, stay
        self.assertEqual(self.fmt("- One\n\n- Two\n\nAfter\n"), "    - One\n\n    - Two\n\nAfter\n")

    def test_intro_line_pulls_in_the_next_block(self):
        # A line ending with ":" introduces the next block: the blank goes and the block is indented
        self.assertEqual(self.fmt("Note:\n\nParagraph\nmore\n\nPlain again\n"),
                         "Note:\n    Paragraph\n    more\n\nPlain again\n")
        self.assertEqual(self.fmt("Note:\nParagraph\n"), "Note:\n    Paragraph\n")
        self.assertEqual(self.fmt("Run:\n\n```sh\nls\n```\nAfter\n"), "Run:\n    ```sh\n    ls\n    ```\nAfter\n")
        self.assertEqual(MarkdownFormatter().format("Table:\n\n| a | b |\n", flush=True), "Table:\n| a | b |")
        # Without ":" a paragraph keeps its blank line and its place
        self.assertEqual(self.fmt("Intro\n\nParagraph\n"), "Intro\n\nParagraph\n")
        self.assertEqual(self.fmt("Intro\n\n```\nx\n```\n"), "Intro\n\n```\nx\n```\n")

    def test_tool_call_after_intro_line_not_indented(self):
        # A tool call is not the block a "Text:" line introduces; the calls stay at the left edge
        self.assertEqual(self.fmt("Let me check:\n⏺ Bash ls\n⏺ Read a.py\n"),
                         "Let me check:\n⏺ Bash ls\n⏺ Read a.py\n")
        self.assertEqual(self.fmt("Let me check:\n\n⏺ Bash ls\n"), "Let me check:\n\n⏺ Bash ls\n")
        self.assertEqual(self.fmt("- item\n⏺ Bash ls\n"), "    - item\n⏺ Bash ls\n")

    def test_tool_call_body_left_alone(self):
        # A command's lines are not list items or intro blocks of the reply: shown as they ran
        text = "Run it:\n⏺ Bash git commit -m 'x\n\n    - fix x\n    more\n    '\nDone:\n- a\n"
        self.assertEqual(self.fmt(text),
                         "Run it:\n⏺ Bash git commit -m 'x\n\n    - fix x\n    more\n    '\nDone:\n    - a\n")

    def test_text_starts_after_tool_calls(self):
        formatter = MarkdownFormatter()
        out = formatter.format("Text\n⏺ Read a.py\n\n- X\n⏺ Bash ls\nDone\n")
        self.assertEqual([out[i:i + 7] for i in formatter.take_text_starts()], ["    - X", "Done\n"])
        self.assertEqual(formatter.take_text_starts(), [])
        # Streamed: the mark comes with the chunk holding the first text line
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("⏺ Read a.py\n\n"), "⏺ Read a.py\n\n")
        self.assertEqual(formatter.take_text_starts(), [])
        self.assertEqual(formatter.format("- X\n"), "    - X\n")
        self.assertEqual(formatter.take_text_starts(), [0])

    def test_heading_after_intro_line(self):
        # A ":" line introduces nothing when a heading follows: the blank stays, the heading is not indented
        self.assertEqual(self.fmt("Here is what changed:\n\n## Details\nText\n"),
                         "Here is what changed:\n\n## Details\nText\n")
        self.assertEqual(self.fmt("Steps:\n## Next\n"), "Steps:\n## Next\n")

    def test_blank_line_before_a_list_streamed(self):
        text = "Steps:\n\n- One\n  - Nested\n- Two\n\nThen:\n\nText\n"
        formatter = MarkdownFormatter()
        streamed = "".join(formatter.format(text[i:i + 2]) for i in range(0, len(text), 2))
        streamed += formatter.format("", flush=True)
        self.assertEqual(streamed, self.fmt(text))
        self.assertEqual(streamed, "Steps:\n    - One\n      - Nested\n    - Two\n\nThen:\n    Text\n")
        # A last item without a newline is indented too
        formatter = MarkdownFormatter()
        self.assertEqual(formatter.format("Steps:\n\n- One") + formatter.format("", flush=True),
                         "Steps:\n    - One")

    def test_blank_lines_kept_without_indent(self):
        self.list_indent = 0
        self.assertEqual(self.fmt("Steps:\n\n- One\n\nNote:\n\nText\n"), "Steps:\n\n- One\n\nNote:\n\nText\n")

    def test_setting(self):
        self.list_indent = 2
        self.assertEqual(self.fmt("- One\n"), "  - One\n")
        self.list_indent = 0
        self.assertEqual(self.fmt("- One\n  - Two\n"), "- One\n  - Two\n")
        self.list_indent = -1  # invalid: the default
        self.assertEqual(self.fmt("- One\n"), "    - One\n")


class TestMarkdownFormatterCodeFence(unittest.TestCase):

    def setUp(self):
        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda k, default=None: (
            "html" if k == "table_style" else default
        )
        sys.modules["sublime"].load_settings.return_value = mock_settings

    def test_nested_code_fence_preserves_table_formatting(self):
        text = (
            "````markdown\n"
            "```python\n"
            "# nested code block\n"
            "```\n"
            "````\n"
            "\n"
            "| Col A | Col B |\n"
            "|---|---|\n"
            "| val 1 | val 2 |\n"
        )
        formatter = MarkdownFormatter()
        output = formatter.format(text, flush=True)
        tables = formatter.take_html_tables()

        self.assertFalse(formatter.in_code_block)
        self.assertEqual(len(tables), 1)
        self.assertIn("≡", output)

    def test_table_inside_code_block_not_formatted(self):
        text = (
            "```text\n"
            "| Col A | Col B |\n"
            "|---|---|\n"
            "| val 1 | val 2 |\n"
            "```\n"
        )
        formatter = MarkdownFormatter()
        output = formatter.format(text, flush=True)
        tables = formatter.take_html_tables()

        self.assertFalse(formatter.in_code_block)
        self.assertEqual(len(tables), 0)
        self.assertNotIn("≡", output)

    def test_reset_clears_code_block_state(self):
        text = (
            "```python\n"
            "# unclosed block\n"
        )
        formatter = MarkdownFormatter()
        formatter.format(text, flush=True)
        self.assertTrue(formatter.in_code_block)

        formatter.reset()
        self.assertFalse(formatter.in_code_block)
        self.assertEqual(formatter.table_buffer, [])


if __name__ == "__main__":
    unittest.main()
