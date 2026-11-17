import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatpanel import TablePhantomManager
from chatview.chatrender import (
    extract_diff_fold_ranges,
    RESPONSE_PHANTOM_KEY,
    ResponsePhantomManager,
    resolve_markdown_preview_raw_mode,
)
from chatview.md_render import MarkdownFormatter, preprocess_response_markdown


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
        self.assertEqual(text[start:end], "+line 3\n  +line 4\n  ```")

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


class _FakeRegion:
    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def empty(self):
        return self.a == self.b


class _FakeView:
    def __init__(self, text=""):
        self._text = text
        self._regions = {}
        self._region_meta = {}
        self.fold = MagicMock()
        self.unfold = MagicMock()
        self.show = MagicMock()

    def substr(self, region_or_point):
        if isinstance(region_or_point, int):
            return self._text[region_or_point:region_or_point + 1]
        return self._text[region_or_point.begin():region_or_point.end()]

    def add_regions(
        self,
        key,
        regions,
        flags=0,
        annotations=None,
        annotation_color=None,
        on_navigate=None,
    ):
        self._regions[key] = list(regions)
        self._region_meta[key] = {
            "flags": flags,
            "annotations": list(annotations) if annotations is not None else None,
            "annotation_color": annotation_color,
            "on_navigate": on_navigate,
        }

    def get_regions(self, key):
        return list(self._regions.get(key, []))

    def erase_regions(self, key):
        self._regions.pop(key, None)
        self._region_meta.pop(key, None)

    def line(self, point):
        if self._text:
            pt = max(0, min(point, len(self._text)))
            line_start = self._text.rfind("\n", 0, pt) + 1
            nl_end = self._text.find("\n", pt)
            line_end = nl_end if nl_end >= 0 else len(self._text)
            return _FakeRegion(line_start, line_end)
        return _FakeRegion(max(0, point - 3), point)


class PreprocessResponseMarkdownTests(unittest.TestCase):
    def test_table_converted_outside_code_fence_only(self):
        md = (
            "| Name | Role |\n"
            "| --- | --- |\n"
            "| Ada | Engineer |\n\n"
            "```python\n"
            "| not | a_table |\n"
            "```\n"
        )
        out = preprocess_response_markdown(md)
        self.assertIn('<div class="table">', out)
        self.assertIn('header-row', out)
        self.assertIn("Name", out)
        self.assertIn("Ada", out)
        self.assertIn("| not | a_table |", out)

    def test_tool_line_converted_to_toolfile_link(self):
        md = "⏺ Read path/to/file.py#L10\n\nSummary follows."
        out = preprocess_response_markdown(md)
        self.assertIn('<div class="chat-tool-line">', out)
        self.assertIn(
            '<span class="chat-tool-bullet">⏺</span> '
            '<span class="chat-tool-name">Read</span> '
            '<a class="chat-tool-link" href="toolfile:path/to/file.py#L10">',
            out,
        )
        self.assertIn("Summary follows.", out)

    def test_non_file_tool_calls_use_unified_markup(self):
        md = (
            "⏺ Bash git status\n"
            "⏺ RunCommand (python3 -m unittest)\n"
            "⏺ imageGeneration\n"
            "⏺\n"
        )
        out = preprocess_response_markdown(md)
        self.assertIn(
            '<div class="chat-tool-line">'
            '<span class="chat-tool-bullet">⏺</span> '
            '<span class="chat-tool-name">Bash</span> git status</div>',
            out,
        )
        self.assertIn(
            '<div class="chat-tool-line">'
            '<span class="chat-tool-bullet">⏺</span> '
            '<span class="chat-tool-name">RunCommand</span> '
            '(python3 -m unittest)</div>',
            out,
        )
        self.assertIn(
            '<div class="chat-tool-line">'
            '<span class="chat-tool-bullet">⏺</span> '
            '<span class="chat-tool-name">imageGeneration</span></div>',
            out,
        )
        self.assertIn(
            '<div class="chat-tool-line">'
            '<span class="chat-tool-bullet">⏺</span></div>',
            out,
        )
        self.assertNotIn("chat-tool-arg", out)

    def test_multiline_tool_command_and_diff_bodies_omitted_from_preview(self):
        md = (
            "⏺ RunCommand (python3 -c ')\n\n"
            "    print('line1')\n\n"
            "    | not | a_table |\n"
            "    ```python\n"
            "    ⏺ fake_nested\n"
            "    ```\n\n"
            "⏺ Bash git log\n\n"
            "    --oneline -n 5\n\n"
            "⏺ Edit chatview/md_render.py#L10\n"
            "  ````diff\n"
            "  -old\n"
            "  +new\n"
            "  ````\n\n"
            "Summary follows.\n\n"
            "- item\n"
            "  - nested\n"
        )
        out = preprocess_response_markdown(md)
        self.assertIn('<span class="chat-tool-name">RunCommand</span> (python3 -c \')', out)
        self.assertIn('<span class="chat-tool-name">Bash</span> git log', out)
        self.assertIn('<span class="chat-tool-name">Edit</span>', out)
        self.assertNotIn("print('line1')", out)
        self.assertNotIn("not | a_table", out)
        self.assertNotIn("fake_nested", out)
        self.assertNotIn("--oneline -n 5", out)
        self.assertNotIn("-old", out)
        self.assertNotIn("+new", out)
        self.assertIn("Summary follows.", out)
        self.assertIn("- item\n    - nested", out)

    def test_diff_fence_omitted_from_preview(self):
        md = (
            "Summary before diff\n\n"
            "```diff\n"
            "+line1\n"
            "-line2\n"
            "```\n\n"
            "Summary after diff"
        )
        out = preprocess_response_markdown(md)
        self.assertIn("Summary before diff", out)
        self.assertIn("Summary after diff", out)
        self.assertNotIn("```diff", out)
        self.assertNotIn("+line1", out)
        self.assertNotIn("-line2", out)

    def test_two_space_nested_list_normalized_to_four_spaces(self):
        md = "- outer\n  - inner\n    - deep\n"
        out = preprocess_response_markdown(md)
        self.assertIn("- outer\n    - inner\n        - deep", out)

    def test_consecutive_tool_calls_grouped_together(self):
        md = (
            "⏺ Read a.py\n"
            "⏺ Read b.py\n"
        )
        out = preprocess_response_markdown(md)
        self.assertEqual(out.count('<div class="chat-tool-group">'), 1)
        self.assertNotIn("chat-tool-group-spaced", out)

        tight_omitted = (
            "⏺ Edit a.py\n"
            "  ````diff\n"
            "  -old\n"
            "  +new\n"
            "  ````\n"
            "⏺ Bash cmd1\n\n"
            "    arg1\n\n"
            "    arg2\n"
            "⏺ Read b.py\n"
        )
        out_tight = preprocess_response_markdown(tight_omitted)
        self.assertEqual(out_tight.count('<div class="chat-tool-group">'), 1)
        self.assertNotIn("chat-tool-group-spaced", out_tight)
        self.assertEqual(out_tight.count('<div class="chat-tool-line">'), 3)
        self.assertNotIn("-old", out_tight)
        self.assertNotIn("arg1", out_tight)
        self.assertNotIn("arg2", out_tight)

    def test_non_consecutive_tool_calls_preserve_blank_line_spacing(self):
        md = "⏺ Read a.py\n\n⏺ Read b.py\n\n⏺ Read c.py\n"
        out = preprocess_response_markdown(md)
        self.assertEqual(out.count('<div class="chat-tool-group">'), 1)
        self.assertEqual(out.count('<div class="chat-tool-group-spaced">'), 2)

        with_omitted = (
            "⏺ Edit a.py\n"
            "  ````diff\n"
            "  -old\n"
            "  +new\n"
            "  ````\n\n"
            "⏺ Bash git status\n\n"
            "    M file.py\n\n"
            "⏺ Read b.py\n"
        )
        out_omitted = preprocess_response_markdown(with_omitted)
        self.assertEqual(out_omitted.count('<div class="chat-tool-group">'), 1)
        self.assertEqual(out_omitted.count('<div class="chat-tool-group-spaced">'), 2)
        self.assertNotIn("-old", out_omitted)
        self.assertNotIn("M file.py", out_omitted)

    def test_text_or_code_between_tool_calls_resets_spaced_group(self):
        md_text = "⏺ Read a.py\n\nSummary follows.\n\n⏺ Read b.py\n"
        out_text = preprocess_response_markdown(md_text)
        self.assertEqual(out_text.count('<div class="chat-tool-group">'), 2)
        self.assertNotIn("chat-tool-group-spaced", out_text)
        self.assertIn("Summary follows.", out_text)

        md_code = "⏺ Read a.py\n\n```python\nx = 1\n```\n\n⏺ Read b.py\n"
        out_code = preprocess_response_markdown(md_code)
        self.assertEqual(out_code.count('<div class="chat-tool-group">'), 2)
        self.assertNotIn("chat-tool-group-spaced", out_code)
        self.assertIn("```python\nx = 1\n```", out_code)

        md_table = "⏺ Read a.py\n\n| H1 | H2 |\n| --- | --- |\n| 1 | 2 |\n\n⏺ Read b.py\n"
        out_table = preprocess_response_markdown(md_table)
        self.assertEqual(out_table.count('<div class="chat-tool-group">'), 2)
        self.assertNotIn("chat-tool-group-spaced", out_table)
        self.assertIn('<div class="table">', out_table)

    def test_inline_math_converted_and_multiline_block_math_untouched(self):
        md = (
            "# Loss \\( \\mathcal{L} \\)\n\n"
            "Complexity is $O(n^2)$ and $$E = mc^2$$.\n\n"
            "| not_table | $\\alpha + \\beta$ |\n\n"
            "$$\n"
            "\\int_0^\\infty f(x) dx\n"
            "$$\n\n"
            "```python\n"
            "val = '$x^2$'\n"
            "```\n\n"
            "⏺ Bash echo \"$a\" \"$b\"\n\n"
            "| Name | Formula |\n"
            "| --- | --- |\n"
            "| Energy | $E = mc^2$ |\n"
        )
        out = preprocess_response_markdown(md)
        self.assertIn("# Loss ℒ", out)
        self.assertIn("Complexity is O(n²) and E = mc².", out)
        self.assertIn("| not_table | α + β |", out)
        self.assertIn("$$\n\\int_0^\\infty f(x) dx\n$$", out)
        self.assertIn("val = '$x^2$'", out)
        self.assertIn('echo "$a" "$b"', out)
        self.assertIn('<div class="table">', out)
        self.assertIn("E = mc²", out)

        # Idempotent when fed buffer text produced by format(..., format_tables=False)
        buf_text = MarkdownFormatter().format(md, flush=True, format_tables=False)
        self.assertEqual(preprocess_response_markdown(buf_text).strip(), out.strip())

    def test_html_table_fold_header_omitted_before_pipe_table(self):
        md = (
            "Standalone ≡ symbol.\n\n"
            "≡\n"
            "  | Name | Formula |\n"
            "  | --- | --- |\n"
            "  | Circle | π r² |\n"
        )
        out = preprocess_response_markdown(md)
        self.assertIn("Standalone ≡ symbol.", out)
        self.assertNotIn("\n≡\n", out)
        self.assertIn('<div class="table">', out)
        self.assertIn("π r²", out)


if __name__ == "__main__":
    unittest.main()
