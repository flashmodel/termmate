import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatrender import (
    extract_diff_fold_ranges,
    ResponsePhantomManager,
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
    def __init__(self):
        self._regions = {}
        self._region_meta = {}
        self.fold = MagicMock()
        self.unfold = MagicMock()

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
            '<a class="chat-tool-link" href="toolfile:path/to/file.py#L10">',
            out,
        )
        self.assertIn("Summary follows.", out)

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


class ResponsePhantomManagerTests(unittest.TestCase):
    def _make_sublime_mock(self):
        mock_sublime = MagicMock()
        mock_sublime.Region = _FakeRegion
        mock_sublime.LAYOUT_BLOCK = 2
        mock_sublime.LAYOUT_INLINE = 0
        mock_sublime.HIDDEN = 32
        phantom_set = MagicMock()
        mock_sublime.PhantomSet.return_value = phantom_set
        mock_sublime.Phantom.side_effect = lambda *args, **kwargs: (
            SimpleNamespace(args=args, kwargs=kwargs)
        )
        return mock_sublime, phantom_set

    def _make_mdpopups_mock(self):
        mock_mdpopups = MagicMock()
        mock_mdpopups.md2html.side_effect = lambda view, md, **kw: (
            f"<div class='mdpopups'>{md}</div>"
        )
        return mock_mdpopups

    def test_add_toggle_truncate_and_clear(self):
        view = _FakeView()
        mock_sublime, phantom_set = self._make_sublime_mock()
        mock_mdpopups = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            r1 = _FakeRegion(10, 50)
            k1 = mgr.add(r1, "# Heading\n\nBody")
            self.assertIsNotNone(k1)
            self.assertEqual(view.get_regions(k1), [r1])
            # Default is Raw mode: region stays unfolded, no folding
            view.unfold.assert_called_with(r1)
            view.fold.assert_not_called()

            # Raw mode initially: 2 phantoms (top inline after ⋅⋅⋅, bottom block at end)
            phantoms = phantom_set.update.call_args[0][0]
            self.assertEqual(len(phantoms), 2)
            # Top raw toggle (inline right after ⋅⋅⋅)
            self.assertEqual(phantoms[0].args[0].begin(), 10)
            self.assertEqual(phantoms[0].args[2], mock_sublime.LAYOUT_INLINE)
            self.assertIn("chatview-raw-toggle-top", phantoms[0].args[1])
            self.assertIn(">preview<", phantoms[0].args[1])
            self.assertIn(f"toggle:{k1}", phantoms[0].args[1])
            # Bottom raw toggle
            self.assertEqual(phantoms[1].args[0].begin(), 50)
            self.assertEqual(phantoms[1].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("chatview-raw-toggle-bottom", phantoms[1].args[1])
            self.assertIn(">preview<", phantoms[1].args[1])
            self.assertIn(f"toggle:{k1}", phantoms[1].args[1])

            # Toggle to Preview mode: 1 block phantom with top and bottom raw bars
            view.fold.reset_mock()
            view.unfold.reset_mock()
            mgr.toggle_entry(k1)
            view.unfold.assert_called_once_with(r1)
            view.fold.assert_called_once_with(r1)
            phantoms = phantom_set.update.call_args[0][0]
            self.assertEqual(len(phantoms), 1)
            self.assertEqual(phantoms[0].args[0].begin(), 50)
            self.assertEqual(phantoms[0].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("termmate-response-preview", phantoms[0].args[1])
            self.assertIn("chat-response-top-bar", phantoms[0].args[1])
            self.assertIn("chat-response-bottom-bar", phantoms[0].args[1])
            self.assertIn(f"toggle:{k1}", phantoms[0].args[1])
            self.assertIn(">raw<", phantoms[0].args[1])

            # Toggle back to Raw mode via raw link click
            view.fold.reset_mock()
            view.unfold.reset_mock()
            mgr._on_navigate(f"toggle:{k1}")
            view.unfold.assert_called_once_with(r1)
            view.fold.assert_not_called()
            phantoms = phantom_set.update.call_args[0][0]
            self.assertEqual(len(phantoms), 2)
            self.assertEqual(phantoms[0].args[2], mock_sublime.LAYOUT_INLINE)
            self.assertEqual(phantoms[1].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn(">preview<", phantoms[0].args[1])
            self.assertIn(">preview<", phantoms[1].args[1])

            # Add second response (default raw mode) and test truncate at cut_point
            r2 = _FakeRegion(70, 120)
            k2 = mgr.add(r2, "Second response")
            # 2 phantoms from r1 + 2 phantoms from r2 = 4 total
            self.assertEqual(len(phantom_set.update.call_args[0][0]), 4)
            view.unfold.reset_mock()
            mgr.truncate(70)
            self.assertEqual(view.get_regions(k1), [r1])
            self.assertEqual(view.get_regions(k2), [])
            view.unfold.assert_called_once_with(r2)
            self.assertEqual(len(phantom_set.update.call_args[0][0]), 2)

            # Clear removes remaining regions and resets PhantomSet
            view.unfold.reset_mock()
            mgr.clear()
            self.assertEqual(view.get_regions(k1), [])
            view.unfold.assert_called_once_with(r1)
            self.assertEqual(phantom_set.update.call_args[0][0], [])


if __name__ == "__main__":
    unittest.main()
