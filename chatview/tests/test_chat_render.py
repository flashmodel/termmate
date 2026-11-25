import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatpanel import TablePhantomManager
from chatview.chatrender import (
    extract_diff_fold_ranges,
    PREVIEW_CARD_CSS,
    PreviewPhantomView,
    RawPhantomView,
    RESPONSE_CSS,
    RESPONSE_PHANTOM_KEY,
    ResponsePhantomManager,
    resolve_markdown_preview_raw_mode,
)
from chatview.md_render import (
    MarkdownFormatter,
    normalize_markdown_list_indentation,
    preprocess_response_markdown,
    render_ordered_lists_as_html,
)


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
        self._settings = {}
        self.fold = MagicMock()
        self.unfold = MagicMock()
        self.show = MagicMock()

    def settings(self):
        return self._settings

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
        self.assertNotIn("chat-tool-sep", out)

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
        self.assertNotIn("chat-tool-sep", out_tight)
        self.assertEqual(out_tight.count('<div class="chat-tool-line">'), 3)
        self.assertNotIn("-old", out_tight)
        self.assertNotIn("arg1", out_tight)
        self.assertNotIn("arg2", out_tight)

    def test_non_consecutive_tool_calls_preserve_blank_line_spacing(self):
        md = "⏺ Read a.py\n\n⏺ Read b.py\n\n⏺ Read c.py\n"
        out = preprocess_response_markdown(md)
        self.assertEqual(out.count('<div class="chat-tool-group">'), 3)
        self.assertEqual(out.count('<div class="chat-tool-sep">&nbsp;</div>'), 2)

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
        self.assertEqual(out_omitted.count('<div class="chat-tool-group">'), 3)
        self.assertEqual(
            out_omitted.count('<div class="chat-tool-sep">&nbsp;</div>'), 2
        )
        self.assertNotIn("-old", out_omitted)
        self.assertNotIn("M file.py", out_omitted)

    def test_text_or_code_between_tool_calls_inserts_boundary_separators(self):
        sep = '<div class="chat-tool-sep">&nbsp;</div>'
        group = '<div class="chat-tool-group">'

        md_text = "⏺ Read a.py\n\nSummary follows.\n\n⏺ Read b.py\n"
        out_text = preprocess_response_markdown(md_text)
        self.assertEqual(out_text.count(group), 2)
        self.assertEqual(out_text.count(sep), 2)
        first_group = out_text.index(group)
        first_sep = out_text.index(sep)
        summary_pos = out_text.index("Summary follows.")
        second_sep = out_text.index(sep, first_sep + len(sep))
        second_group = out_text.index(group, first_group + len(group))
        self.assertLess(first_group, first_sep)
        self.assertLess(first_sep, summary_pos)
        self.assertLess(summary_pos, second_sep)
        self.assertLess(second_sep, second_group)

        md_code = "⏺ Read a.py\n\n```python\nx = 1\n```\n\n⏺ Read b.py\n"
        out_code = preprocess_response_markdown(md_code)
        self.assertEqual(out_code.count(group), 2)
        self.assertEqual(out_code.count(sep), 2)
        self.assertIn("```python\nx = 1\n```", out_code)

        md_table = "⏺ Read a.py\n\n| H1 | H2 |\n| --- | --- |\n| 1 | 2 |\n\n⏺ Read b.py\n"
        out_table = preprocess_response_markdown(md_table)
        self.assertEqual(out_table.count(group), 2)
        self.assertEqual(out_table.count(sep), 2)
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


class ListSeparatorNormalizationTests(unittest.TestCase):
    def test_adjacent_ul_and_ol_separated(self):
        source = (
            "- Unordered 1\n"
            "  1. Nested ordered 1\n"
            "  2. Nested ordered 2\n"
            "- Unordered 2\n\n"
            "1. Ordered 1\n"
            "  - Nested unordered A\n"
            "  - Nested unordered B\n"
            "2. Ordered 2"
        )
        sep = '<div class="chat-list-sep"></div>'
        expected = (
            "- Unordered 1\n"
            "    1. Nested ordered 1\n"
            "    2. Nested ordered 2\n"
            "- Unordered 2\n\n"
            f"{sep}\n\n"
            "1. Ordered 1\n"
            "    - Nested unordered A\n"
            "    - Nested unordered B\n"
            "2. Ordered 2"
        )
        out = normalize_markdown_list_indentation(source)
        self.assertEqual(out, expected)
        self.assertNotIn("<!--", out)

    def test_tight_and_multi_blank_list_type_switch(self):
        sep = '<div class="chat-list-sep"></div>'
        tight = "1. First\n- Second"
        self.assertEqual(
            normalize_markdown_list_indentation(tight),
            f"1. First\n\n{sep}\n\n- Second",
        )

        multi_blank = "- First\n\n\n1. Second"
        self.assertEqual(
            normalize_markdown_list_indentation(multi_blank),
            f"- First\n\n\n{sep}\n\n1. Second",
        )

        with_para = "- First\n\nParagraph break\n\n1. Second"
        self.assertEqual(normalize_markdown_list_indentation(with_para), with_para)

        with_heading = "- First\n\n## Section\n\n1. Second"
        self.assertEqual(normalize_markdown_list_indentation(with_heading), with_heading)

        with_hr = "- First\n\n---\n\n1. Second"
        self.assertEqual(normalize_markdown_list_indentation(with_hr), with_hr)

        with_fence = "- First\n\n```python\nx = 1\n```\n\n1. Second"
        self.assertEqual(normalize_markdown_list_indentation(with_fence), with_fence)

    def test_nested_same_level_list_type_switch(self):
        sep = '<div class="chat-list-sep"></div>'
        source = (
            "- Parent\n"
            "  - Sub UL\n\n"
            "  1. Sub OL"
        )
        expected = (
            "- Parent\n"
            "    - Sub UL\n\n"
            f"    {sep}\n\n"
            "    1. Sub OL"
        )
        out = normalize_markdown_list_indentation(source)
        self.assertEqual(out, expected)
        self.assertNotIn("<!--", out)

    def test_same_type_and_separate_parent_sublists_no_separator(self):
        same_type = "- A\n* B"
        self.assertEqual(normalize_markdown_list_indentation(same_type), same_type)

        diff_parents = (
            "- Parent 1\n"
            "  - Sub UL\n"
            "- Parent 2\n"
            "  1. Sub OL"
        )
        diff_parents_expected = (
            "- Parent 1\n"
            "    - Sub UL\n"
            "- Parent 2\n"
            "    1. Sub OL"
        )
        self.assertEqual(
            normalize_markdown_list_indentation(diff_parents),
            diff_parents_expected,
        )

    def test_root_list_interrupting_paragraph_inserts_blank_line(self):
        nested_ul = (
            "Notes:\n"
            "- A\n"
            "  - B\n"
            "    1. C\n"
            "    2. D\n"
            "- E"
        )
        nested_ul_expected = (
            "Notes:\n\n"
            "- A\n"
            "    - B\n"
            "        1. C\n"
            "        2. D\n"
            "- E"
        )
        self.assertEqual(
            normalize_markdown_list_indentation(nested_ul),
            nested_ul_expected,
        )

        ordered_after_text = "Steps:\n1. First\n2. Second"
        self.assertEqual(
            normalize_markdown_list_indentation(ordered_after_text),
            "Steps:\n\n1. First\n2. Second",
        )

        for unchanged in ("Year:\n2026. A fresh start", "Notes:\n1) First", "Notes:\n-   "):
            self.assertEqual(normalize_markdown_list_indentation(unchanged), unchanged)

        self.assertNotIn(
            "\n\n",
            normalize_markdown_list_indentation("    code1\n    - code2"),
        )


def _num(label):
    return f'<span class="chat-ol-num">{label}</span>'


class OrderedListHtmlTests(unittest.TestCase):
    """Verify miniHTML structural rewriting for <ol>/<li> lists."""

    def test_basic_nested_and_preserved_attributes(self):
        src = (
            '<ol id="steps" class="custom chat-ol" start="2" type="a">\n'
            '<li id="i1" class="task" value="5">Outer\n'
            '<ul>\n<li>Bullet A</li>\n'
            '<li>Bullet B\n<ol>\n<li>Deep 1</li>\n<li>Deep 2</li>\n</ol>\n</li>\n'
            '</ul>\n</li>\n'
            '<li>Outer next</li>\n'
            '</ol>'
        )
        out = render_ordered_lists_as_html(src)
        self.assertNotIn("<ol", out)
        self.assertNotIn("<li id=", out)
        self.assertIn('<div class="chat-ol custom" id="steps">', out)
        self.assertIn(
            f'<div class="chat-li task" id="i1">{_num("e.")} Outer',
            out,
        )
        self.assertIn("<ul>\n<li>Bullet A</li>", out)
        self.assertIn(f'<div class="chat-li">{_num("1.")} Deep 1</div>', out)
        self.assertIn(f'<div class="chat-li">{_num("2.")} Deep 2</div>', out)
        self.assertIn(f'<div class="chat-li">{_num("f.")} Outer next</div>', out)

    def test_marker_placement_wrappers_inline_block_and_empty(self):
        src = (
            "<ol>\n"
            "<li>\n<p>First para</p>\n<p>Second para</p>\n</li>\n"
            "<li><h3>Heading</h3><p>Body</p></li>\n"
            "<li><strong>Bold</strong> &amp; &#60;tag&#62;</li>\n"
            "<li>&mdash; Entity</li>\n"
            "<li><pre><code>x = 1</code></pre></li>\n"
            "<li></li>\n"
            "</ol>"
        )
        out = render_ordered_lists_as_html(src)
        self.assertIn(f"<p>{_num('1.')} First para</p>", out)
        self.assertIn("<p>Second para</p>", out)
        self.assertIn(f"<h3>{_num('2.')} Heading</h3><p>Body</p>", out)
        self.assertIn(f"{_num('3.')} <strong>Bold</strong> &amp; &#60;tag&#62;", out)
        self.assertIn(f"{_num('4.')} &mdash; Entity", out)
        self.assertIn(f"{_num('5.')} <pre><code>x = 1</code></pre>", out)
        self.assertIn(f"{_num('6.')}</div>", out)

    def test_start_reversed_and_li_value(self):
        src = (
            '<ol start="3">\n<li>Three</li>\n<li>Four</li>\n</ol>'
            '<ol start="5" reversed>\n<li>Five</li>\n<li value="10">Ten</li>\n'
            '<li>Nine</li>\n</ol>'
            '<ol reversed>\n'
            '<li>First\n<ol reversed>\n<li>In A</li>\n<li>In B</li>\n</ol>\n</li>\n'
            '<li></li>\n'
            '<li value="10">Jump</li>\n'
            '<li>After</li>\n'
            '</ol>'
            '<ol>\n<li value="0">Zero</li>\n<li value="-2">Neg</li>\n'
            '<li value="abc">Bad</li>\n</ol>'
        )
        out = render_ordered_lists_as_html(src)
        self.assertIn(f"{_num('3.')} Three", out)
        self.assertIn(f"{_num('4.')} Four", out)
        self.assertIn(f"{_num('5.')} Five", out)
        self.assertIn(f"{_num('10.')} Ten", out)
        self.assertIn(f"{_num('9.')} Nine", out)
        self.assertIn(f"{_num('4.')} First", out)
        self.assertIn(f"{_num('2.')} In A", out)
        self.assertIn(f"{_num('1.')} In B", out)
        self.assertIn(f"{_num('3.')}</div>", out)
        self.assertIn(f"{_num('10.')} Jump", out)
        self.assertIn(f"{_num('9.')} After", out)
        self.assertIn(f"{_num('0.')} Zero", out)
        self.assertIn(f"{_num('-2.')} Neg", out)
        self.assertIn(f"{_num('-1.')} Bad", out)

    def test_ol_type_formatting_and_fallbacks(self):
        src = (
            '<ol type="a" start="26"><li>Z</li><li>AA</li></ol>'
            '<ol type="A"><li>U1</li><li>U2</li></ol>'
            '<ol type="i"><li>I</li><li value="4">IV</li>'
            '<li value="9">IX</li><li value="42">XLII</li></ol>'
            '<ol type="I" start="4"><li>Four</li></ol>'
            '<ol type="a" start="0"><li>ZeroA</li></ol>'
            '<ol type="i" start="0"><li>ZeroI</li></ol>'
            '<ol type="I" start="4000"><li>Big</li></ol>'
            '<ol type="unknown"><li>Dec</li></ol>'
        )
        out = render_ordered_lists_as_html(src)
        for label, text in (
            ("z.", "Z"), ("aa.", "AA"), ("A.", "U1"), ("B.", "U2"),
            ("i.", "I"), ("iv.", "IV"), ("ix.", "IX"), ("xlii.", "XLII"),
            ("IV.", "Four"), ("0.", "ZeroA"), ("0.", "ZeroI"),
            ("4000.", "Big"), ("1.", "Dec"),
        ):
            self.assertIn(f"{_num(label)} {text}", out)

    def test_pre_blocks_untouched(self):
        literal = "<pre><ol><li>literal</li></ol></pre>"
        escaped = "<pre><code>&lt;ol&gt;&lt;li&gt;raw&lt;/li&gt;&lt;/ol&gt;</code></pre>"
        self.assertEqual(render_ordered_lists_as_html(literal), literal)
        self.assertEqual(render_ordered_lists_as_html(escaped), escaped)

    def test_malformed_implicit_and_unclosed_tags(self):
        self.assertEqual(
            render_ordered_lists_as_html(
                "<ol><li>Outer<ul><li>U1<li>U2</ul></li><li>O2</li></ol>"
            ),
            '<div class="chat-ol">'
            f'<div class="chat-li">{_num("1.")} Outer'
            '<ul><li>U1</li><li>U2</li></ul></div>'
            f'<div class="chat-li">{_num("2.")} O2</div></div>',
        )
        self.assertEqual(
            render_ordered_lists_as_html("<ol><li>Outer<ul><li>U1</ol>"),
            '<div class="chat-ol">'
            f'<div class="chat-li">{_num("1.")} Outer'
            '<ul><li>U1</li></ul></div></div>',
        )
        self.assertEqual(
            render_ordered_lists_as_html("<ul><li>A<ol><li>B</ul>"),
            '<ul><li>A<div class="chat-ol">'
            f'<div class="chat-li">{_num("1.")} B</div>'
            '</div></li></ul>',
        )
        self.assertEqual(
            render_ordered_lists_as_html("<ol><li>Outer<ul></li><li>U1</li></ul></li></ol>"),
            '<div class="chat-ol">'
            f'<div class="chat-li">{_num("1.")} Outer'
            '<ul><li>U1</li></ul></div></div>',
        )
        self.assertEqual(
            render_ordered_lists_as_html("<ol reversed><li>A<li>B"),
            '<div class="chat-ol">'
            f'<div class="chat-li">{_num("2.")} A</div>'
            f'<div class="chat-li">{_num("1.")} B</div></div>',
        )

    def test_strips_internal_list_separator_markers(self):
        sep = '<div class="chat-list-sep"></div>'
        with_ol = f"<ul>\n<li>U</li>\n</ul>\n{sep}\n<ol>\n<li>O<p>{sep}</p></li>\n</ol>"
        out = render_ordered_lists_as_html(with_ol)
        self.assertNotIn("chat-list-sep", out)
        self.assertIn(f"{_num('1.')} O", out)

        without_ol = f"<ul>\n<li>U</li>\n</ul>\n{sep}\n<p>Text</p>"
        self.assertEqual(
            render_ordered_lists_as_html(without_ol),
            "<ul>\n<li>U</li>\n</ul>\n<p>Text</p>",
        )

        escaped = '<pre><code>&lt;div class="chat-list-sep"&gt;</code></pre>'
        self.assertEqual(render_ordered_lists_as_html(escaped), escaped)


class PreviewOrderedListIntegrationTests(unittest.TestCase):
    def test_preview_render_body_html_rewrites_ordered_lists(self):
        view = _FakeView()
        preview = PreviewPhantomView(view, MagicMock())
        mock_mdpopups = MagicMock()
        mock_mdpopups.md2html.return_value = (
            '<ul><li>Bullet</li></ul>\n'
            '<div class="chat-list-sep"></div>\n'
            '<ol><li>One</li><li>Two</li></ol>'
        )
        with patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            out = preview._render_body_html("- Bullet\n\n1. One\n2. Two")
        self.assertIn('<div class="chat-ol">', out)
        self.assertIn(f'<div class="chat-li">{_num("1.")} One</div>', out)
        self.assertIn(f'<div class="chat-li">{_num("2.")} Two</div>', out)
        self.assertNotIn("chat-list-sep", out)

    def test_preview_render_body_html_fallback_strips_list_separators(self):
        view = _FakeView()
        preview = PreviewPhantomView(view, MagicMock())
        mock_mdpopups = MagicMock()
        mock_mdpopups.md2html.side_effect = RuntimeError("boom")
        with patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            out = preview._render_body_html("- Bullet\n1. One")
        self.assertTrue(out.startswith("<pre>"))
        self.assertNotIn("chat-list-sep", out)
        self.assertIn("- Bullet", out)
        self.assertIn("1. One", out)

        with patch("chatview.chatrender.HAS_MDPOPUPS", False), \
             patch("chatview.chatrender.mdpopups", None):
            out_no_md = preview._render_body_html("- Bullet\n1. One")
        self.assertTrue(out_no_md.startswith("<pre>"))
        self.assertNotIn("chat-list-sep", out_no_md)

    def test_response_css_has_ordered_list_styles_without_native_li_overrides(self):
        for css in (RESPONSE_CSS, PREVIEW_CARD_CSS):
            self.assertIn("padding-left: 1.2rem;", css)
            self.assertIn(".termmate-response-preview .chat-ol", css)
            self.assertIn(".termmate-response-preview .chat-ol-num", css)
            self.assertIn("margin-left: -1.1rem;", css)
            self.assertIn(".termmate-response-preview .chat-li", css)
            self.assertIn(".termmate-response-preview .chat-li p", css)
        self.assertNotIn(".termmate-response-preview li", RESPONSE_CSS)


class TestRawTableBottomAnchor(unittest.TestCase):
    def test_has_table_ending_at_respects_visibility_and_point(self):
        view = _FakeView("≡\n  | a |\n  |---|\n  | 1 |\n\n")
        mgr = TablePhantomManager(view)
        mgr.add(_FakeRegion(1, 21), "<body>table</body>")

        self.assertTrue(mgr.has_table_ending_at(21))
        self.assertFalse(mgr.has_table_ending_at(20))

        mgr.sync_response_region(_FakeRegion(0, 22), raw_mode=False)
        self.assertFalse(mgr.has_table_ending_at(21))

        mgr.sync_response_region(_FakeRegion(0, 22), raw_mode=True)
        self.assertTrue(mgr.has_table_ending_at(21))

    def test_bottom_anchor_keeps_normalized_empty_line_and_strips_others(self):
        raw = RawPhantomView(_FakeView("hello\n\n"), MagicMock())
        with patch("chatview.chatrender.sublime.Region", _FakeRegion):
            # Ends on normalized empty line after newline -> stays at region.end()
            anchor_nl = raw._bottom_anchor(_FakeRegion(0, 6))
            self.assertEqual((anchor_nl.begin(), anchor_nl.end()), (6, 6))

            # Ends on NBSP fold-terminator line -> anchors at start of NBSP line
            raw_nbsp = RawPhantomView(_FakeView("hello\n\u00a0\n"), MagicMock())
            anchor_nbsp = raw_nbsp._bottom_anchor(_FakeRegion(0, 7))
            self.assertEqual((anchor_nbsp.begin(), anchor_nbsp.end()), (6, 6))

            # Plain text without trailing newline -> stays at last non-blank line end
            anchor_plain = raw._bottom_anchor(_FakeRegion(0, 5))
            self.assertEqual((anchor_plain.begin(), anchor_plain.end()), (5, 5))

            # Trailing newline followed by non-empty line -> backs up to last non-blank line
            raw_non_empty = RawPhantomView(_FakeView("hello\nworld"), MagicMock())
            anchor_back = raw_non_empty._bottom_anchor(_FakeRegion(0, 6))
            self.assertEqual((anchor_back.begin(), anchor_back.end()), (5, 5))

    def test_markdown_formatter_flush_pads_trailing_html_table(self):
        table_md = "| A | B |\n|---|---|\n| 1 | 2 |"
        for suffix in ("", "\n", "\n\n"):
            fmt = MarkdownFormatter(table_style_getter=lambda: "html")
            out = fmt.format(table_md + suffix, flush=True)
            tables = fmt.take_html_tables()
            self.assertEqual(len(tables), 1)
            end = tables[0]["end"]
            self.assertTrue(out[:end].endswith("| 1 | 2 |"))
            self.assertEqual(out[end:], "\n\u00a0\n")

        fmt_stream = MarkdownFormatter(table_style_getter=lambda: "html")
        out_stream = fmt_stream.format(table_md + "\n\n", flush=False)
        tables_stream = fmt_stream.take_html_tables()
        self.assertEqual(len(tables_stream), 1)
        self.assertEqual(out_stream[tables_stream[0]["end"]:], "\n\u00a0\n")

        fmt_text = MarkdownFormatter(table_style_getter=lambda: "html")
        out_text = fmt_text.format(table_md + "\n\ntail", flush=True)
        tables_text = fmt_text.take_html_tables()
        self.assertEqual(len(tables_text), 1)
        self.assertEqual(out_text[tables_text[0]["end"]:], "\n\u00a0\ntail")


if __name__ == "__main__":
    unittest.main()
