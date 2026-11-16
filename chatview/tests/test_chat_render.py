import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatrender import (
    BOTTOM_SWITCH_CSS,
    extract_diff_fold_ranges,
    PREVIEW_CARD_CSS,
    PREVIEW_SWITCH_CSS,
    RESPONSE_CSS,
    RESPONSE_PHANTOM_KEY,
    ResponsePhantomManager,
    SWITCH_CSS,
    TOP_SWITCH_CSS,
    _preview_toggle_html,
    _preview_top_inline_html,
    _render_switch_html,
    _raw_toggle_html,
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


class ResponsePhantomManagerTests(unittest.TestCase):
    def _make_sublime_mock(self):
        mock_sublime = MagicMock()
        mock_sublime.Region = _FakeRegion
        mock_sublime.LAYOUT_BLOCK = 2
        mock_sublime.LAYOUT_INLINE = 0
        mock_sublime.HIDDEN = 32
        phantom_sets = {}

        def _make_phantom_set(_view, key):
            ps = MagicMock()
            phantom_sets[key] = ps
            return ps

        mock_sublime.PhantomSet.side_effect = _make_phantom_set
        mock_sublime.Phantom.side_effect = lambda *args, **kwargs: (
            SimpleNamespace(args=args, kwargs=kwargs)
        )
        return mock_sublime, phantom_sets

    def _make_mdpopups_mock(self):
        mock_mdpopups = MagicMock()
        preview_phantom_sets = {}

        def _make_preview_set(_view, key):
            ps = MagicMock()
            preview_phantom_sets[key] = ps
            return ps

        mock_mdpopups.PhantomSet.side_effect = _make_preview_set
        mock_mdpopups.Phantom.side_effect = lambda *args, **kwargs: (
            SimpleNamespace(args=args, kwargs=kwargs)
        )
        mock_mdpopups.md2html.side_effect = lambda _view, md: f"<p>{md}</p>"
        return mock_mdpopups, preview_phantom_sets

    def test_add_toggle_truncate_and_clear(self):
        view = _FakeView()
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_sets = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            r1 = _FakeRegion(10, 50)
            k1 = mgr.add(r1, "# Heading\n\nBody")
            self.assertIsNotNone(k1)
            self.assertEqual(view.get_regions(k1), [r1])
            # Default is Raw mode: unfolded, lazy (no markdown/mdpopups work yet).
            view.unfold.assert_called_with(r1)
            view.fold.assert_not_called()
            self.assertEqual(
                set(mgr.entries[0].keys()),
                {"key", "md_text", "raw_mode"},
            )
            mock_mdpopups.md2html.assert_not_called()
            mock_mdpopups.PhantomSet.assert_not_called()

            raw_ps1 = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{k1}"]
            raw_phantoms = raw_ps1.update.call_args[0][0]
            self.assertEqual(len(raw_phantoms), 2)
            # Top raw toggle defaults to subtle inline icon after ⋅⋅⋅
            self.assertEqual(raw_phantoms[0].args[0].begin(), 10)
            self.assertEqual(raw_phantoms[0].args[2], mock_sublime.LAYOUT_INLINE)
            self.assertIn("chatview-raw-toggle-top", raw_phantoms[0].args[1])
            self.assertIn("chat-raw-top-inline", raw_phantoms[0].args[1])
            self.assertIn("switch-subtle", raw_phantoms[0].args[1])
            self.assertIn(">◫&nbsp;&nbsp;<", raw_phantoms[0].args[1])
            self.assertIn(f"toggle:{k1}:top", raw_phantoms[0].args[1])
            # Bottom raw toggle
            self.assertEqual(raw_phantoms[1].args[0].begin(), 50)
            self.assertEqual(raw_phantoms[1].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("chatview-raw-toggle-bottom", raw_phantoms[1].args[1])
            self.assertIn(">◫&nbsp;&nbsp;<", raw_phantoms[1].args[1])
            self.assertIn(f"toggle:{k1}:bottom", raw_phantoms[1].args[1])

            # Toggle to Preview mode via top button: clears raw PhantomSet and
            # renders 1 combined public mdpopups.Phantom.
            view.fold.reset_mock()
            view.unfold.reset_mock()
            view.show.reset_mock()
            mgr.toggle_entry(k1, is_top=True)
            view.unfold.assert_called_once_with(r1)
            view.fold.assert_called_once_with(r1)
            mock_sublime.set_timeout.call_args[0][0]()
            view.show.assert_called_with(7, False)

            self.assertEqual(raw_ps1.update.call_args[0][0], [])
            self.assertEqual(mock_mdpopups.md2html.call_count, 1)
            preview_ps1 = preview_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview:{k1}"]
            preview_phantoms = preview_ps1.update.call_args[0][0]
            self.assertEqual(len(preview_phantoms), 1)

            card_ph = preview_phantoms[0]
            self.assertEqual(card_ph.args[0].begin(), 50)
            self.assertEqual(card_ph.args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertFalse(card_ph.kwargs["md"])
            self.assertEqual(card_ph.kwargs["css"], PREVIEW_CARD_CSS)
            self.assertEqual(
                card_ph.kwargs["wrapper_class"],
                "termmate-response-card",
            )
            card_html = card_ph.args[1]
            self.assertNotIn("chat-switch-bar-top", card_html)
            self.assertNotIn(f"toggle:{k1}:top", card_html)
            self.assertIn('<div class="termmate-response-top-spacer">', card_html)
            self.assertIn('<div class="termmate-response-preview"><p># Heading', card_html)
            self.assertIn("chat-switch-bar-bottom", card_html)
            self.assertIn(f"toggle:{k1}:bottom", card_html)
            self.assertIn("switch-subtle", card_html)
            self.assertIn(">‹›&nbsp;<", card_html)
            self.assertLess(
                card_html.index("termmate-response-preview"),
                card_html.index("chat-switch-bar-bottom"),
            )
            top_ps1 = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview_top:{k1}"]
            top_phantoms = top_ps1.update.call_args[0][0]
            self.assertEqual(len(top_phantoms), 1)
            self.assertEqual(top_phantoms[0].args[0].begin(), 50)
            self.assertEqual(top_phantoms[0].args[2], mock_sublime.LAYOUT_INLINE)
            self.assertIn("chatview-preview-toggle-top", top_phantoms[0].args[1])
            self.assertIn("chat-preview-top-inline", top_phantoms[0].args[1])
            self.assertIn(f"toggle:{k1}:top", top_phantoms[0].args[1])
            self.assertIn(">‹›&nbsp;<", top_phantoms[0].args[1])

            # Toggle back to Raw via bottom click, then back to Preview:
            # Preview body HTML is rendered freshly on each switch into Preview.
            view.fold.reset_mock()
            view.unfold.reset_mock()
            view.show.reset_mock()
            mgr._on_navigate(f"toggle:{k1}:bottom")
            self.assertEqual(top_ps1.update.call_args[0][0], [])
            view.unfold.assert_called_once_with(r1)
            view.fold.assert_not_called()
            mock_sublime.set_timeout.call_args[0][0]()
            view.show.assert_called_with(50, False)
            self.assertEqual(preview_ps1.update.call_args[0][0], [])
            raw_ps1 = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{k1}"]
            raw_phantoms = raw_ps1.update.call_args[0][0]
            self.assertEqual(len(raw_phantoms), 2)
            self.assertIn(">◫&nbsp;&nbsp;<", raw_phantoms[0].args[1])
            self.assertIn(">◫&nbsp;&nbsp;<", raw_phantoms[1].args[1])

            mgr.toggle_entry(k1, is_top=True)
            top_ps1 = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview_top:{k1}"]
            self.assertEqual(len(top_ps1.update.call_args[0][0]), 1)
            self.assertEqual(mock_mdpopups.md2html.call_count, 2)
            preview_ps1 = preview_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview:{k1}"]

            # Adding second response in preview does not re-update k1, and
            # truncate() cleans up k2's inline top and preview card phantoms.
            ps1_calls_before = preview_ps1.update.call_count
            r2 = _FakeRegion(70, 120)
            k2 = mgr.add(r2, "Second response", raw_mode=False)
            self.assertEqual(preview_ps1.update.call_count, ps1_calls_before)
            top_ps2 = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview_top:{k2}"]
            self.assertEqual(len(top_ps2.update.call_args[0][0]), 1)
            preview_ps2 = preview_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview:{k2}"]

            view.unfold.reset_mock()
            mgr.truncate(70)
            self.assertEqual(view.get_regions(k1), [r1])
            self.assertEqual(len(top_ps1.update.call_args[0][0]), 1)
            self.assertEqual(view.get_regions(k2), [])
            self.assertEqual(top_ps2.update.call_args[0][0], [])
            view.unfold.assert_called_once_with(r2)
            self.assertEqual(preview_ps1.update.call_count, ps1_calls_before)
            self.assertEqual(preview_ps2.update.call_args[0][0], [])

            # Clear removes remaining regions and clears all PhantomSets.
            view.unfold.reset_mock()
            mgr.clear()
            self.assertEqual(view.get_regions(k1), [])
            self.assertEqual(top_ps1.update.call_args[0][0], [])
            view.unfold.assert_called_once_with(r1)
            self.assertEqual(preview_ps1.update.call_args[0][0], [])

    def test_raw_top_toggle_inline_after_separator_and_fallbacks(self):
        buf = "⋅⋅⋅\n\n# Heading\n\nBody"
        view = _FakeView(buf)
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, _ = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            # region starts right after ⋅⋅⋅ (index 3), placing subtle icon inline
            r_sep = _FakeRegion(3, len(buf))
            k_sep = mgr.add(r_sep, "# Heading\n\nBody")
            raw_phantoms = (
                raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{k_sep}"].update.call_args[0][0]
            )
            self.assertEqual(raw_phantoms[0].args[0].begin(), 3)
            self.assertEqual(raw_phantoms[0].args[2], mock_sublime.LAYOUT_INLINE)
            self.assertIn("chat-raw-top-inline", raw_phantoms[0].args[1])

            # With show_toolbar=True, raw top pill sits as a block below ⋅⋅⋅
            view_tb = _FakeView(buf)
            mgr_tb = ResponsePhantomManager(view_tb, show_toolbar_getter=lambda: True)
            k_tb = mgr_tb.add(_FakeRegion(3, len(buf)), "# Heading\n\nBody")
            tb_phantoms = (
                raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{k_tb}"].update.call_args[0][0]
            )
            self.assertEqual(tb_phantoms[0].args[0].begin(), 3)
            self.assertEqual(tb_phantoms[0].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("chat-raw-top-bar", tb_phantoms[0].args[1])
            self.assertIn('<span class="switch-pill">', tb_phantoms[0].args[1])

            # Fallback when resumed response has no ⋅⋅⋅ separator line
            no_sep_buf = "\n[Resuming]\n\n# Heading\n\nBody"
            view_no_sep = _FakeView(no_sep_buf)
            mgr_no_sep = ResponsePhantomManager(view_no_sep)
            heading_start = no_sep_buf.index("# Heading")
            r_no_sep = _FakeRegion(heading_start, len(no_sep_buf))
            k_no_sep = mgr_no_sep.add(r_no_sep, "# Heading\n\nBody")
            no_sep_phantoms = (
                raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{k_no_sep}"].update.call_args[0][0]
            )
            self.assertEqual(no_sep_phantoms[0].args[0].begin(), heading_start - 1)
            self.assertEqual(no_sep_phantoms[0].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("chat-raw-top-bar", no_sep_phantoms[0].args[1])

            # Without ⋅⋅⋅ separator, preview falls back to card-top ‹› instead of inline
            mgr_no_sep.toggle_entry(k_no_sep, is_top=True)
            self.assertNotIn(
                f"{RESPONSE_PHANTOM_KEY}:preview_top:{k_no_sep}",
                raw_phantom_sets,
            )

    def _make_layout_view(
        self,
        buf,
        layout_map,
        vy=0.0,
        vh=600.0,
        layout_h=2400.0,
        scroll_past_end=True,
    ):
        view = _FakeView(buf)
        state = {"vy": float(vy), "layout_h": float(layout_h)}
        view.size = lambda: len(buf)
        view.line_height = lambda: 20.0
        view.text_to_layout = lambda pt: (0.0, float(layout_map[pt]))
        view.viewport_position = lambda: (0.0, state["vy"])
        view.viewport_extent = lambda: (800.0, float(vh))
        view.layout_extent = lambda: (800.0, state["layout_h"])
        view.settings = lambda: {"scroll_past_end": scroll_past_end}
        view.set_viewport_position = MagicMock(
            side_effect=lambda pos, animate=True: state.__setitem__("vy", float(pos[1]))
        )
        return view, state

    def test_toggle_bottom_raw_to_preview_uses_post_message_buffer_layout(self):
        buf = "⋅⋅⋅\n\nline1\nline2\nline3\n\n> "
        end_pt = buf.index("line3") + len("line3")
        after_pt = end_pt + 1
        # Initially in Raw mode: line3 ends at 1780, after_pt sits at 1830 (below raw toggle).
        layout_map = {0: 200.0, end_pt: 1780.0, after_pt: 1830.0}
        view, _ = self._make_layout_view(buf, layout_map, vy=1280.0, vh=600.0)
        mock_sublime, _ = self._make_sublime_mock()
        mock_mdpopups, _ = self._make_mdpopups_mock()

        def _on_fold(_reg):
            layout_map[end_pt] = 200.0
            layout_map[after_pt] = 1100.0

        view.fold.side_effect = _on_fold

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            key = mgr.add(_FakeRegion(3, end_pt), "line1\nline2\nline3")
            view.show.reset_mock()
            mgr.toggle_entry(key, is_top=False)
            mock_sublime.set_timeout.call_args[0][0]()

            view.show.assert_not_called()
            view.set_viewport_position.assert_called_once_with((0.0, 550.0), False)

    def test_toggle_bottom_preview_to_raw_handles_taller_and_shorter_body(self):
        buf = "⋅⋅⋅\n\nline1\nline2\n\n> "
        end_pt = buf.index("line2") + len("line2")
        after_pt = end_pt + 1
        layout_map = {0: 200.0, end_pt: 200.0, after_pt: 1100.0}
        view, state = self._make_layout_view(buf, layout_map, vy=550.0, vh=600.0)
        mock_sublime, _ = self._make_sublime_mock()
        mock_mdpopups, _ = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            key = mgr.add(_FakeRegion(3, end_pt), "line1\nline2")
            mgr.toggle_entry(key, is_top=True)
            state["vy"] = 550.0

            # Preview (200..1100) -> taller Raw (200..1830): keep bottom switch at screen y=550
            view.set_viewport_position.reset_mock()
            mgr.toggle_entry(key, is_top=False)
            layout_map[end_pt] = 1780.0
            layout_map[after_pt] = 1830.0
            mock_sublime.set_timeout.call_args[0][0]()
            view.set_viewport_position.assert_called_once_with((0.0, 1280.0), False)

            # Tall Raw (200..1830) -> Short Preview (200..450): stays visible at 0 instead of
            # hiding the message above the viewport top
            mgr.toggle_entry(key, is_top=False)
            layout_map[end_pt] = 200.0
            layout_map[after_pt] = 450.0
            view.set_viewport_position.reset_mock()
            mock_sublime.set_timeout.call_args[0][0]()
            view.set_viewport_position.assert_called_once_with((0.0, 0.0), False)

            # Toggle back to tall Raw while button was near top (vy=100 -> screen_y=350 < 360):
            # jumps cleanly down to Raw bottom rather than slicing top of screen
            state["vy"] = 100.0
            view.set_viewport_position.reset_mock()
            mgr.toggle_entry(key, is_top=False)
            layout_map[end_pt] = 1780.0
            layout_map[after_pt] = 1830.0
            mock_sublime.set_timeout.call_args[0][0]()
            # 600 - min(30, 90) = 570 -> 1830 - 570 = 1260
            view.set_viewport_position.assert_called_once_with((0.0, 1260.0), False)

    def test_toggle_bottom_eof_layout_extent_fallback(self):
        buf = "⋅⋅⋅\n\nline1"
        end_pt = len(buf)
        layout_map = {0: 200.0, end_pt: 200.0}
        view, state = self._make_layout_view(
            buf,
            layout_map,
            vy=500.0,
            vh=600.0,
            layout_h=1200.0,
            scroll_past_end=False,
        )
        mock_sublime, _ = self._make_sublime_mock()
        mock_mdpopups, _ = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            key = mgr.add(_FakeRegion(3, end_pt), "line1")
            view.set_viewport_position.reset_mock()
            mock_sublime.set_timeout.reset_mock()
            mgr.toggle_entry(key, is_top=False)
            self.assertEqual(mock_sublime.set_timeout.call_count, 1)
            self.assertEqual(mock_sublime.set_timeout.call_args[0][1], 30)

            # Single 30ms timeout folds and reads layout_extent()[1] to clamp EOF
            state["layout_h"] = 1400.0
            cb = mock_sublime.set_timeout.call_args[0][0]
            cb()
            self.assertEqual(mock_sublime.set_timeout.call_count, 1)
            # max_y = 1400 - 600 = 800
            view.set_viewport_position.assert_called_once_with((0.0, 800.0), False)

    def test_toggle_top_raw_to_preview_defers_fold_with_single_timeout(self):
        buf = "⋅⋅⋅\n\nline1\nline2\n\n> "
        end_pt = buf.index("line2") + len("line2")
        after_pt = end_pt + 1
        layout_map = {0: 1000.0, end_pt: 2200.0, after_pt: 2250.0}
        view, state = self._make_layout_view(
            buf,
            layout_map,
            vy=960.0,
            vh=600.0,
            layout_h=2400.0,
            scroll_past_end=False,
        )
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_sets = self._make_mdpopups_mock()

        def _on_fold(_reg):
            # Collapse raw range down onto line 0; card height stays cached.
            layout_map[end_pt] = 1000.0
            layout_map[after_pt] = 1700.0
            state["layout_h"] = 1760.0

        view.fold.side_effect = _on_fold

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            key = mgr.add(_FakeRegion(3, end_pt), "line1\nline2")
            raw_ps = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{key}"]
            view.fold.reset_mock()
            view.show.reset_mock()
            mock_sublime.set_timeout.reset_mock()

            # Before toggle: top_y=1000, vy=960 -> pre_screen_y=40.
            # Deferring fold keeps raw text expanded while preview card renders,
            # and registers inline preview_top before block card at region.end().
            events = []
            prev_factory = mock_mdpopups.PhantomSet.side_effect

            def _track_preview_set(v, k):
                ps = prev_factory(v, k)
                ps.update.side_effect = lambda phs, name=k: events.append(
                    ("preview", len(phs))
                )
                return ps

            mock_mdpopups.PhantomSet.side_effect = _track_preview_set
            orig_sub_ps = mock_sublime.PhantomSet.side_effect

            def _track_sub_set(v, k):
                ps = orig_sub_ps(v, k)
                ps.update.side_effect = lambda phs, name=k: events.append(
                    (name.split(":")[1], len(phs))
                )
                return ps

            mock_sublime.PhantomSet.side_effect = _track_sub_set

            mgr.toggle_entry(key, is_top=True)
            view.fold.assert_not_called()
            self.assertEqual(state["vy"], 960.0)
            self.assertEqual(len(raw_ps.update.call_args[0][0]), 1)
            self.assertEqual(
                len(
                    raw_phantom_sets[
                        f"{RESPONSE_PHANTOM_KEY}:preview_top:{key}"
                    ].update.call_args[0][0]
                ),
                1,
            )
            self.assertEqual(
                len(
                    preview_phantom_sets[
                        f"{RESPONSE_PHANTOM_KEY}:preview:{key}"
                    ].update.call_args[0][0]
                ),
                1,
            )
            self.assertLess(
                events.index(("preview_top", 1)),
                events.index(("preview", 1)),
            )
            self.assertEqual(mock_sublime.set_timeout.call_count, 1)
            self.assertEqual(mock_sublime.set_timeout.call_args[0][1], 30)

            # Single 30ms timeout folds raw, clears raw toggle, and pins viewport
            # without any extra retry timers.
            cb = mock_sublime.set_timeout.call_args[0][0]
            cb()
            self.assertEqual(mock_sublime.set_timeout.call_count, 1)
            view.fold.assert_called_once()
            self.assertEqual(raw_ps.update.call_args[0][0], [])
            self.assertEqual(
                len(
                    raw_phantom_sets[
                        f"{RESPONSE_PHANTOM_KEY}:preview_top:{key}"
                    ].update.call_args[0][0]
                ),
                1,
            )
            view.show.assert_not_called()
            view.set_viewport_position.assert_called_once_with((0.0, 960.0), False)
            self.assertEqual(state["vy"], 960.0)

            # Toggle back Preview -> Raw schedules 0ms and preserves pre_screen_y=40
            view.set_viewport_position.reset_mock()
            mgr.toggle_entry(key, is_top=True)
            self.assertEqual(mock_sublime.set_timeout.call_args[0][1], 0)
            layout_map[end_pt] = 2200.0
            layout_map[after_pt] = 2250.0
            state["layout_h"] = 2400.0
            mock_sublime.set_timeout.call_args[0][0]()
            view.set_viewport_position.assert_called_once_with((0.0, 960.0), False)

    def test_rapid_double_toggle_cancels_pending_deferred_fold(self):
        buf = "⋅⋅⋅\n\nline1\nline2\n\n> "
        end_pt = buf.index("line2") + len("line2")
        after_pt = end_pt + 1
        layout_map = {0: 100.0, end_pt: 300.0, after_pt: 320.0}
        view, _ = self._make_layout_view(buf, layout_map, vy=0.0, vh=600.0)
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_sets = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            key = mgr.add(_FakeRegion(3, end_pt), "line1\nline2")
            view.fold.reset_mock()

            mgr.toggle_entry(key, is_top=True)
            pending_cb = mock_sublime.set_timeout.call_args[0][0]
            mgr.toggle_entry(key, is_top=True)
            # Stale deferred-fold callback from first toggle becomes a no-op.
            pending_cb()
            view.fold.assert_not_called()
            self.assertTrue(mgr.entries[0]["raw_mode"])
            self.assertEqual(
                raw_phantom_sets[
                    f"{RESPONSE_PHANTOM_KEY}:preview_top:{key}"
                ].update.call_args[0][0],
                [],
            )
            self.assertEqual(
                preview_phantom_sets[
                    f"{RESPONSE_PHANTOM_KEY}:preview:{key}"
                ].update.call_args[0][0],
                [],
            )
            self.assertEqual(
                len(
                    raw_phantom_sets[
                        f"{RESPONSE_PHANTOM_KEY}:raw:{key}"
                    ].update.call_args[0][0]
                ),
                2,
            )

    def test_resolve_markdown_preview_raw_mode(self):
        self.assertTrue(resolve_markdown_preview_raw_mode("raw"))
        self.assertFalse(resolve_markdown_preview_raw_mode("preview"))
        self.assertFalse(resolve_markdown_preview_raw_mode(" PREVIEW "))
        self.assertTrue(resolve_markdown_preview_raw_mode("invalid"))
        self.assertTrue(resolve_markdown_preview_raw_mode(""))
        self.assertTrue(resolve_markdown_preview_raw_mode(None))

    def test_add_respects_default_raw_mode_getter_and_explicit_raw_mode(self):
        buf = "⋅⋅⋅\n\n# Title\n\nBody"
        view = _FakeView(buf)
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_sets = self._make_mdpopups_mock()
        mode_state = {"raw": False}

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(
                view,
                default_raw_mode_getter=lambda: mode_state["raw"],
            )
            region = _FakeRegion(3, len(buf))
            key_preview = mgr.add(region, "# Title\n\nBody")
            self.assertEqual(mgr.entries[-1]["key"], key_preview)
            self.assertFalse(mgr.entries[-1]["raw_mode"])
            view.unfold.assert_called_once_with(region)
            view.fold.assert_called_once_with(region)
            self.assertEqual(
                len(
                    preview_phantom_sets[
                        f"{RESPONSE_PHANTOM_KEY}:preview:{key_preview}"
                    ].update.call_args[0][0]
                ),
                1,
            )

            # Updating getter dynamically changes subsequent add() initial mode.
            mode_state["raw"] = True
            view.unfold.reset_mock()
            view.fold.reset_mock()
            key_raw = mgr.add(region, "# Title\n\nBody")
            self.assertTrue(mgr.entries[-1]["raw_mode"])
            view.unfold.assert_called_once_with(region)
            view.fold.assert_not_called()
            self.assertEqual(
                len(
                    raw_phantom_sets[
                        f"{RESPONSE_PHANTOM_KEY}:raw:{key_raw}"
                    ].update.call_args[0][0]
                ),
                2,
            )

            # Explicit raw_mode overrides getter.
            view.unfold.reset_mock()
            view.fold.reset_mock()
            key_forced = mgr.add(region, "# Title\n\nBody", raw_mode=False)
            self.assertFalse(mgr.entries[-1]["raw_mode"])
            view.unfold.assert_called_once_with(region)
            view.fold.assert_called_once_with(region)
            self.assertEqual(
                len(
                    preview_phantom_sets[
                        f"{RESPONSE_PHANTOM_KEY}:preview:{key_forced}"
                    ].update.call_args[0][0]
                ),
                1,
            )

    def test_raw_mode_add_skips_preprocessing_mdpopups_and_tracked_keys(self):
        buf = "⋅⋅⋅\n\n# A\n\n⋅⋅⋅\n\n# B\n\n⋅⋅⋅\n\n# C"
        view = _FakeView(buf)
        fake_settings = MagicMock()
        fake_settings.get.return_value = None
        view.settings = lambda: fake_settings
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, _ = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups), \
             patch("chatview.chatrender.preprocess_response_markdown") as mock_prep:
            mgr = ResponsePhantomManager(view, default_raw_mode_getter=lambda: False)
            r0 = _FakeRegion(3, 10)
            r1 = _FakeRegion(15, 22)
            r2 = _FakeRegion(27, len(buf))
            k0 = mgr.add(r0, "# A", raw_mode=True)
            k1 = mgr.add(r1, "# B", raw_mode=True)
            k2 = mgr.add(r2, "# C", raw_mode=True)

            for k in (k0, k1, k2):
                ps = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{k}"]
                self.assertEqual(ps.update.call_count, 1)
                self.assertEqual(len(ps.update.call_args[0][0]), 2)
            fake_settings.set.assert_not_called()
            mock_prep.assert_not_called()
            mock_mdpopups.md2html.assert_not_called()
            mock_mdpopups.PhantomSet.assert_not_called()

    def test_refresh_only_rerenders_active_preview_entries(self):
        buf = "⋅⋅⋅\n\n# Raw\n\n⋅⋅⋅\n\n# Preview"
        view = _FakeView(buf)
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_sets = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            k_raw = mgr.add(_FakeRegion(3, 12), "# Raw", raw_mode=True)
            k_prev = mgr.add(_FakeRegion(17, len(buf)), "# Preview", raw_mode=False)
            self.assertEqual(mock_mdpopups.md2html.call_count, 1)

            ps_raw = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:raw:{k_raw}"]
            ps_prev = preview_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview:{k_prev}"]
            raw_updates_before = ps_raw.update.call_count
            prev_updates_before = ps_prev.update.call_count

            mgr.refresh()
            self.assertEqual(ps_raw.update.call_count, raw_updates_before)
            self.assertEqual(ps_prev.update.call_count, prev_updates_before + 1)
            self.assertEqual(mock_mdpopups.md2html.call_count, 2)

    def test_show_toolbar_getter_controls_top_switch_style(self):
        buf = "⋅⋅⋅\n\n# Title\n\nBody"
        view = _FakeView(buf)
        mock_sublime, raw_phantom_sets = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_sets = self._make_mdpopups_mock()
        toolbar_state = {"show": False}

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(
                view,
                show_toolbar_getter=lambda: toolbar_state["show"],
            )
            region = _FakeRegion(3, len(buf))
            key = mgr.add(region, "# Title\n\nBody")
            raw_top_phantom = raw_phantom_sets[
                f"{RESPONSE_PHANTOM_KEY}:raw:{key}"
            ].update.call_args[0][0][0]
            raw_top_icon = raw_top_phantom.args[1]
            self.assertEqual(raw_top_phantom.args[0].begin(), 3)
            self.assertEqual(raw_top_phantom.args[2], mock_sublime.LAYOUT_INLINE)
            self.assertIn("chat-raw-top-inline", raw_top_icon)
            self.assertIn(">◫&nbsp;&nbsp;<", raw_top_icon)
            self.assertNotIn('<span class="switch-pill">', raw_top_icon)

            # Toggle to Preview with show_toolbar=False -> inline ‹› at region.end()
            mgr.toggle_entry(key, is_top=True)
            top_ps = raw_phantom_sets[f"{RESPONSE_PHANTOM_KEY}:preview_top:{key}"]
            top_phantoms = top_ps.update.call_args[0][0]
            self.assertEqual(len(top_phantoms), 1)
            self.assertEqual(top_phantoms[0].args[0].begin(), len(buf))
            self.assertEqual(top_phantoms[0].args[2], mock_sublime.LAYOUT_INLINE)
            self.assertIn("chat-preview-top-inline", top_phantoms[0].args[1])
            card_icon_html = preview_phantom_sets[
                f"{RESPONSE_PHANTOM_KEY}:preview:{key}"
            ].update.call_args[0][0][0].args[1]
            self.assertNotIn("chat-switch-bar-top", card_icon_html)
            self.assertIn(">‹›&nbsp;<", card_icon_html)

            # Enabling toolbar + refresh clears inline top and renders card-top pill
            toolbar_state["show"] = True
            mgr.refresh()
            self.assertEqual(top_ps.update.call_args[0][0], [])
            card_html = preview_phantom_sets[
                f"{RESPONSE_PHANTOM_KEY}:preview:{key}"
            ].update.call_args[0][0][0].args[1]
            self.assertNotIn("termmate-response-top-spacer", card_html)
            self.assertIn("chat-switch-bar-top", card_html)
            self.assertIn('<span class="switch-active">preview</span>', card_html)
            self.assertIn(">&nbsp;&nbsp;raw&nbsp;&nbsp;<", card_html)
            self.assertIn(">‹›&nbsp;<", card_html)

            mgr.toggle_entry(key, is_top=True)
            self.assertEqual(top_ps.update.call_args[0][0], [])
            raw_pill_phantom = raw_phantom_sets[
                f"{RESPONSE_PHANTOM_KEY}:raw:{key}"
            ].update.call_args[0][0][0]
            raw_top_pill = raw_pill_phantom.args[1]
            self.assertEqual(raw_pill_phantom.args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("chat-raw-top-bar", raw_top_pill)
            self.assertIn('<span class="switch-pill">', raw_top_pill)
            self.assertIn(">preview<", raw_top_pill)


class SegmentedSwitchRenderingTests(unittest.TestCase):
    def test_switch_pill_raw_mode_active_and_target(self):
        html_top_icon = _render_switch_html("k1", is_raw=True, is_top=True)
        self.assertNotIn("switch-pill", html_top_icon)
        self.assertIn('class="switch-subtle chat-toggle-link"', html_top_icon)
        self.assertIn('href="toggle:k1:top"', html_top_icon)
        self.assertIn(">◫&nbsp;&nbsp;<", html_top_icon)

        html_top = _render_switch_html("k1", is_raw=True, is_top=True, show_toolbar=True)
        self.assertIn('<span class="switch-pill">', html_top)
        self.assertNotIn("switch-pill-inline", html_top)
        self.assertIn(
            '<span class="switch-active">&nbsp;&nbsp;raw&nbsp;&nbsp;</span>',
            html_top,
        )
        self.assertIn('href="toggle:k1:top"', html_top)
        self.assertIn('>preview<', html_top)

        html_bot = _render_switch_html("k1", is_raw=True, is_top=False)
        self.assertNotIn("switch-pill", html_bot)
        self.assertNotIn("switch-active", html_bot)
        self.assertIn('class="switch-subtle chat-toggle-link"', html_bot)
        self.assertIn('href="toggle:k1:bottom"', html_bot)
        self.assertIn(">◫&nbsp;&nbsp;<", html_bot)

    def test_switch_pill_preview_mode_active_and_target(self):
        html_top_icon = _render_switch_html("k2", is_raw=False, is_top=True)
        self.assertNotIn("switch-pill", html_top_icon)
        self.assertIn('class="switch-subtle chat-toggle-link"', html_top_icon)
        self.assertIn('href="toggle:k2:top"', html_top_icon)
        self.assertIn(">‹›&nbsp;<", html_top_icon)

        html_top = _render_switch_html("k2", is_raw=False, is_top=True, show_toolbar=True)
        self.assertNotIn("switch-pill-inline", html_top)
        self.assertNotIn("switch-pill-indent", html_top)
        self.assertIn('<span class="switch-active">preview</span>', html_top)
        self.assertIn('href="toggle:k2:top"', html_top)
        self.assertIn(">&nbsp;&nbsp;raw&nbsp;&nbsp;<", html_top)

        html_bot = _render_switch_html("k2", is_raw=False, is_top=False)
        self.assertNotIn("switch-pill", html_bot)
        self.assertNotIn("switch-active", html_bot)
        self.assertIn('class="switch-subtle chat-toggle-link"', html_bot)
        self.assertIn('href="toggle:k2:bottom"', html_bot)
        self.assertIn(">‹›&nbsp;<", html_bot)

    def test_raw_toggle_html_structure(self):
        top_icon_html = _raw_toggle_html("k3", is_top=True)
        self.assertIn("chatview-raw-toggle-top", top_icon_html)
        self.assertIn("margin:0;padding:0;", top_icon_html)
        self.assertIn('class="chat-raw-top-inline"', top_icon_html)
        self.assertIn(".chat-raw-top-inline", top_icon_html)
        self.assertIn("padding: 0 0.5em 0 1.4em;", top_icon_html)
        self.assertIn(".switch-subtle", top_icon_html)
        self.assertIn(">◫&nbsp;&nbsp;<", top_icon_html)
        self.assertNotIn('<span class="switch-pill">', top_icon_html)

        top_fallback_html = _raw_toggle_html("k3", is_top=True, inline=False)
        self.assertIn("chatview-raw-toggle-top", top_fallback_html)
        self.assertIn("margin:0;padding:2px 0 6px 0;", top_fallback_html)
        self.assertIn('class="chat-raw-top-bar"', top_fallback_html)
        self.assertIn(".switch-subtle", top_fallback_html)
        self.assertIn(">◫&nbsp;&nbsp;<", top_fallback_html)
        self.assertNotIn('<span class="switch-pill">', top_fallback_html)

        top_html = _raw_toggle_html("k3", is_top=True, show_toolbar=True)
        self.assertIn("chatview-raw-toggle-top", top_html)
        self.assertIn("margin:0;padding:2px 0 6px 0;", top_html)
        self.assertIn('class="chat-raw-top-bar"', top_html)
        self.assertIn(".chat-raw-top-bar", top_html)
        self.assertIn("padding: 2px 0 0 0;", top_html)
        self.assertIn("<style>", top_html)
        self.assertIn(".switch-pill", top_html)
        self.assertIn(".switch-active", top_html)
        self.assertNotIn("switch-subtle", top_html)

        bot_html = _raw_toggle_html("k3", is_top=False)
        self.assertIn("chatview-raw-toggle-bottom", bot_html)
        self.assertIn('class="chat-raw-bottom-bar"', bot_html)
        self.assertIn(".chat-raw-bottom-bar", bot_html)
        self.assertIn("switch-subtle", bot_html)
        self.assertIn(">◫&nbsp;&nbsp;<", bot_html)
        self.assertNotIn("switch-pill", bot_html)
        self.assertNotIn("border-top", SWITCH_CSS)
        self.assertNotIn(":hover", SWITCH_CSS)

    def test_preview_toggle_html_structure(self):
        top_icon_html = _preview_toggle_html("k4", is_top=True)
        self.assertIn('class="termmate-response-switch-bar"', top_icon_html)
        self.assertIn('class="chat-switch-bar-top"', top_icon_html)
        self.assertIn("toggle:k4:top", top_icon_html)
        self.assertIn("switch-subtle", top_icon_html)
        self.assertIn(">‹›&nbsp;<", top_icon_html)
        self.assertNotIn("switch-pill", top_icon_html)

        top_html = _preview_toggle_html("k4", is_top=True, show_toolbar=True)
        self.assertIn('class="termmate-response-switch-bar"', top_html)
        self.assertIn('class="chat-switch-bar-top"', top_html)
        self.assertIn("toggle:k4:top", top_html)
        self.assertIn(">&nbsp;&nbsp;raw&nbsp;&nbsp;<", top_html)
        self.assertIn('<span class="switch-active">preview</span>', top_html)
        self.assertNotIn("<style>", top_html)

        bot_html = _preview_toggle_html("k4", is_top=False)
        self.assertIn('class="termmate-response-switch-bar"', bot_html)
        self.assertIn('class="chat-switch-bar-bottom"', bot_html)
        self.assertIn("toggle:k4:bottom", bot_html)
        self.assertIn("switch-subtle", bot_html)
        self.assertIn(">‹›&nbsp;<", bot_html)
        self.assertNotIn("switch-pill", bot_html)
        self.assertNotIn("<style>", bot_html)

        inline_html = _preview_top_inline_html("k4")
        self.assertIn("chatview-preview-toggle-top", inline_html)
        self.assertIn("margin:0;padding:0;", inline_html)
        self.assertIn('class="chat-preview-top-inline"', inline_html)
        self.assertIn(".chat-preview-top-inline", inline_html)
        self.assertIn("padding: 0 8px 0 0;", inline_html)
        self.assertIn("toggle:k4:top", inline_html)
        self.assertIn("switch-subtle", inline_html)
        self.assertIn(">‹›&nbsp;<", inline_html)
        self.assertNotIn("switch-pill", inline_html)

    def test_switch_css_split_and_isolated_from_response_css(self):
        self.assertIn(".switch-pill", TOP_SWITCH_CSS)
        self.assertIn(".switch-active", TOP_SWITCH_CSS)
        self.assertIn(".switch-target", TOP_SWITCH_CSS)
        self.assertIn(".chat-switch-bar-top", TOP_SWITCH_CSS)
        self.assertNotIn(".switch-subtle", TOP_SWITCH_CSS)
        self.assertNotIn(".mdpopups", TOP_SWITCH_CSS)

        self.assertIn(".switch-subtle", BOTTOM_SWITCH_CSS)
        self.assertIn(".chat-raw-top-inline", BOTTOM_SWITCH_CSS)
        self.assertIn(".chat-preview-top-inline", BOTTOM_SWITCH_CSS)
        self.assertIn(".chat-switch-bar-bottom", BOTTOM_SWITCH_CSS)
        self.assertIn(".chat-raw-bottom-bar", BOTTOM_SWITCH_CSS)
        self.assertNotIn(".switch-pill", BOTTOM_SWITCH_CSS)
        self.assertNotIn(".mdpopups", BOTTOM_SWITCH_CSS)

        self.assertIn(".termmate-response-card", PREVIEW_SWITCH_CSS)
        self.assertIn(".termmate-response-top-spacer", PREVIEW_SWITCH_CSS)
        self.assertIn("div.mdpopups .switch-pill", PREVIEW_SWITCH_CSS)
        self.assertIn(".termmate-response-switch-bar .switch-pill", PREVIEW_SWITCH_CSS)
        self.assertIn(".termmate-response-switch-bar .chat-switch-bar-top", PREVIEW_SWITCH_CSS)
        self.assertIn(".termmate-response-switch-bar a", PREVIEW_SWITCH_CSS)
        self.assertIn("padding: 2px 3px;", PREVIEW_SWITCH_CSS)
        self.assertIn("line-height: 1.05rem;", PREVIEW_SWITCH_CSS)
        self.assertIn("line-height: 0.9rem;", PREVIEW_SWITCH_CSS)
        self.assertTrue(PREVIEW_CARD_CSS.startswith(PREVIEW_SWITCH_CSS))
        self.assertLess(
            PREVIEW_CARD_CSS.index(".switch-pill"),
            PREVIEW_CARD_CSS.index(".termmate-response-preview"),
        )
        self.assertEqual(PREVIEW_CARD_CSS.count(".switch-pill {"), 1)
        self.assertEqual(PREVIEW_CARD_CSS.count(".switch-active {"), 1)
        self.assertNotIn(".chat-raw-top-bar", PREVIEW_CARD_CSS)
        self.assertNotIn("\n    height:", RESPONSE_CSS)
        self.assertNotIn("border: 0;", RESPONSE_CSS)
        self.assertIn(RESPONSE_CSS, PREVIEW_CARD_CSS)
        self.assertIn(PREVIEW_SWITCH_CSS, PREVIEW_CARD_CSS)

        self.assertIn("color: var(--accent);", SWITCH_CSS)
        self.assertIn("text-decoration: none;", SWITCH_CSS)
        self.assertNotIn(".switch-pill", RESPONSE_CSS)
        self.assertNotIn(".switch-subtle", RESPONSE_CSS)
        self.assertNotIn("li > p", RESPONSE_CSS)
        for tag in ("h1", "code", "pre", "blockquote", "hr", "ul", "ol"):
            self.assertNotIn(f".termmate-response-preview {tag}", RESPONSE_CSS)
        self.assertIn("div.mdpopups .termmate-response-preview a,", RESPONSE_CSS)
        self.assertIn(".termmate-response-preview a,", RESPONSE_CSS)
        self.assertIn("a,\na code {", RESPONSE_CSS)
        self.assertIn("color: var(--bluish);", RESPONSE_CSS)
        self.assertIn("text-decoration: underline;", RESPONSE_CSS)
        self.assertNotIn("var(--accent);\n    text-decoration: underline;", RESPONSE_CSS)
        self.assertIn(".chat-tool-line {", RESPONSE_CSS)
        self.assertIn(".chat-tool-name {", RESPONSE_CSS)
        self.assertIn(".chat-tool-bullet {", RESPONSE_CSS)
        self.assertIn(".chat-tool-link {", RESPONSE_CSS)
        self.assertIn("entity.name.function.chatview", RESPONSE_CSS)
        self.assertNotIn("chat-tool-arg", RESPONSE_CSS)
        self.assertNotIn("var(--cyanish)", RESPONSE_CSS)


if __name__ == "__main__":
    unittest.main()
