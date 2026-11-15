import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatrender import (
    PREVIEW_SWITCH_CSS,
    extract_diff_fold_ranges,
    RESPONSE_CSS,
    ResponsePhantomManager,
    SWITCH_CSS,
    _preview_toggle_html,
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
        raw_phantom_set = MagicMock()
        mock_sublime.PhantomSet.return_value = raw_phantom_set
        mock_sublime.Phantom.side_effect = lambda *args, **kwargs: (
            SimpleNamespace(args=args, kwargs=kwargs)
        )
        return mock_sublime, raw_phantom_set

    def _make_mdpopups_mock(self):
        mock_mdpopups = MagicMock()
        preview_phantom_set = MagicMock()
        mock_mdpopups.PhantomSet.return_value = preview_phantom_set
        mock_mdpopups.Phantom.side_effect = lambda *args, **kwargs: (
            SimpleNamespace(args=args, kwargs=kwargs)
        )
        return mock_mdpopups, preview_phantom_set

    def test_add_toggle_truncate_and_clear(self):
        view = _FakeView()
        mock_sublime, raw_phantom_set = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_set = self._make_mdpopups_mock()

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

            # Raw mode initially: 2 raw phantoms, 0 preview phantoms
            raw_phantoms = raw_phantom_set.update.call_args[0][0]
            self.assertEqual(len(raw_phantoms), 2)
            self.assertEqual(preview_phantom_set.update.call_args[0][0], [])
            # Top raw toggle (block right below ⋅⋅⋅)
            self.assertEqual(raw_phantoms[0].args[0].begin(), 10)
            self.assertEqual(raw_phantoms[0].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("chatview-raw-toggle-top", raw_phantoms[0].args[1])
            self.assertIn(">preview<", raw_phantoms[0].args[1])
            self.assertIn(f"toggle:{k1}:top", raw_phantoms[0].args[1])
            # Bottom raw toggle
            self.assertEqual(raw_phantoms[1].args[0].begin(), 50)
            self.assertEqual(raw_phantoms[1].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn("chatview-raw-toggle-bottom", raw_phantoms[1].args[1])
            self.assertIn(">◫&nbsp;&nbsp;<", raw_phantoms[1].args[1])
            self.assertIn(f"toggle:{k1}:bottom", raw_phantoms[1].args[1])

            # Toggle to Preview mode via top button
            view.fold.reset_mock()
            view.unfold.reset_mock()
            view.show.reset_mock()
            mgr.toggle_entry(k1, is_top=True)
            view.unfold.assert_called_once_with(r1)
            view.fold.assert_called_once_with(r1)
            # Execute async location callback
            mock_sublime.set_timeout.call_args[0][0]()
            view.show.assert_called_with(7, False)

            self.assertEqual(raw_phantom_set.update.call_args[0][0], [])
            preview_phantoms = preview_phantom_set.update.call_args[0][0]
            self.assertEqual(len(preview_phantoms), 3)

            # 1. Top switch phantom (md=False, separate wrapper, same end anchor)
            top_ph, body_ph, bot_ph = preview_phantoms
            self.assertEqual(top_ph.args[0].begin(), 50)
            self.assertEqual(top_ph.args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertFalse(top_ph.kwargs["md"])
            self.assertEqual(top_ph.kwargs["css"], PREVIEW_SWITCH_CSS)
            self.assertEqual(
                top_ph.kwargs["wrapper_class"],
                "termmate-response-switch-bar",
            )
            self.assertIn("chat-switch-bar-top", top_ph.args[1])
            self.assertIn(f"toggle:{k1}:top", top_ph.args[1])
            self.assertIn(">&nbsp;&nbsp;raw&nbsp;&nbsp;<", top_ph.args[1])

            # 2. Markdown body phantom (md=True, pure preprocessed markdown)
            self.assertEqual(body_ph.args[0].begin(), 50)
            self.assertEqual(body_ph.args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertTrue(body_ph.kwargs["md"])
            self.assertEqual(body_ph.kwargs["css"], RESPONSE_CSS)
            self.assertEqual(
                body_ph.kwargs["wrapper_class"],
                "termmate-response-preview",
            )
            self.assertNotIn("chat-toggle-link", body_ph.args[1])
            self.assertNotIn("chat-switch-bar", body_ph.args[1])

            # 3. Bottom switch phantom (md=False, separate wrapper, same end anchor)
            self.assertEqual(bot_ph.args[0].begin(), 50)
            self.assertEqual(bot_ph.args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertFalse(bot_ph.kwargs["md"])
            self.assertEqual(bot_ph.kwargs["css"], PREVIEW_SWITCH_CSS)
            self.assertEqual(
                bot_ph.kwargs["wrapper_class"],
                "termmate-response-switch-bar",
            )
            self.assertIn("chat-switch-bar-bottom", bot_ph.args[1])
            self.assertIn(f"toggle:{k1}:bottom", bot_ph.args[1])
            self.assertIn("switch-subtle", bot_ph.args[1])
            self.assertIn(">‹›&nbsp;<", bot_ph.args[1])
            self.assertNotIn("switch-pill", bot_ph.args[1])

            # Toggle back to Raw mode via bottom raw link click
            view.fold.reset_mock()
            view.unfold.reset_mock()
            view.show.reset_mock()
            mgr._on_navigate(f"toggle:{k1}:bottom")
            view.unfold.assert_called_once_with(r1)
            view.fold.assert_not_called()
            # Execute async location callback for bottom click
            mock_sublime.set_timeout.call_args[0][0]()
            view.show.assert_called_with(50, False)
            raw_phantoms = raw_phantom_set.update.call_args[0][0]
            self.assertEqual(preview_phantom_set.update.call_args[0][0], [])
            self.assertEqual(len(raw_phantoms), 2)
            self.assertEqual(raw_phantoms[0].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertEqual(raw_phantoms[1].args[2], mock_sublime.LAYOUT_BLOCK)
            self.assertIn(">preview<", raw_phantoms[0].args[1])
            self.assertIn(">◫&nbsp;&nbsp;<", raw_phantoms[1].args[1])

            # Add second response (default raw mode) and test truncate at cut_point
            r2 = _FakeRegion(70, 120)
            k2 = mgr.add(r2, "Second response")
            # 2 phantoms from r1 + 2 phantoms from r2 = 4 total
            self.assertEqual(len(raw_phantom_set.update.call_args[0][0]), 4)
            self.assertEqual(preview_phantom_set.update.call_args[0][0], [])
            view.unfold.reset_mock()
            mgr.truncate(70)
            self.assertEqual(view.get_regions(k1), [r1])
            self.assertEqual(view.get_regions(k2), [])
            view.unfold.assert_called_once_with(r2)
            self.assertEqual(len(raw_phantom_set.update.call_args[0][0]), 2)
            self.assertEqual(preview_phantom_set.update.call_args[0][0], [])

            # Clear removes remaining regions and resets both PhantomSets
            view.unfold.reset_mock()
            mgr.clear()
            self.assertEqual(view.get_regions(k1), [])
            view.unfold.assert_called_once_with(r1)
            self.assertEqual(raw_phantom_set.update.call_args[0][0], [])
            self.assertEqual(preview_phantom_set.update.call_args[0][0], [])

    def test_raw_top_toggle_block_below_separator_and_no_sep_fallback(self):
        buf = "⋅⋅⋅\n\n# Heading\n\nBody"
        view = _FakeView(buf)
        mock_sublime, raw_phantom_set = self._make_sublime_mock()
        mock_mdpopups, _ = self._make_mdpopups_mock()

        with patch("chatview.chatrender.sublime", mock_sublime), \
             patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            mgr = ResponsePhantomManager(view)
            # region starts right after ⋅⋅⋅ (index 3), anchoring block below ⋅⋅⋅
            r_sep = _FakeRegion(3, len(buf))
            mgr.add(r_sep, "# Heading\n\nBody")
            raw_phantoms = raw_phantom_set.update.call_args[0][0]
            self.assertEqual(raw_phantoms[0].args[0].begin(), 3)
            self.assertEqual(raw_phantoms[0].args[2], mock_sublime.LAYOUT_BLOCK)

            # Fallback when resumed response has no ⋅⋅⋅ separator line
            no_sep_buf = "\n[Resuming]\n\n# Heading\n\nBody"
            view_no_sep = _FakeView(no_sep_buf)
            mgr_no_sep = ResponsePhantomManager(view_no_sep)
            heading_start = no_sep_buf.index("# Heading")
            r_no_sep = _FakeRegion(heading_start, len(no_sep_buf))
            mgr_no_sep.add(r_no_sep, "# Heading\n\nBody")
            no_sep_phantoms = raw_phantom_set.update.call_args[0][0]
            self.assertEqual(no_sep_phantoms[0].args[0].begin(), heading_start - 1)
            self.assertEqual(no_sep_phantoms[0].args[2], mock_sublime.LAYOUT_BLOCK)

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

    def test_toggle_bottom_retry_and_eof_layout_extent_fallback(self):
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
            # Simulate EOF phantom layout not yet expanded at first callback (220 - 200 <= 30)
            state["layout_h"] = 220.0
            view.set_viewport_position.reset_mock()
            mock_sublime.set_timeout.reset_mock()
            mgr.toggle_entry(key, is_top=False)
            self.assertEqual(mock_sublime.set_timeout.call_count, 1)

            # First timer fires before phantom finishes expanding -> schedules one retry
            first_cb = mock_sublime.set_timeout.call_args[0][0]
            first_cb()
            view.set_viewport_position.assert_not_called()
            self.assertEqual(mock_sublime.set_timeout.call_count, 2)

            # Second callback observes full Preview height via layout_extent()[1] and clamps EOF
            state["layout_h"] = 1400.0
            retry_cb = mock_sublime.set_timeout.call_args[0][0]
            retry_cb()
            # max_y = 1400 - 600 = 800
            view.set_viewport_position.assert_called_once_with((0.0, 800.0), False)

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
        mock_sublime, raw_phantom_set = self._make_sublime_mock()
        mock_mdpopups, preview_phantom_set = self._make_mdpopups_mock()
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
            self.assertEqual(len(raw_phantom_set.update.call_args[0][0]), 0)
            self.assertEqual(len(preview_phantom_set.update.call_args[0][0]), 3)

            # Updating getter dynamically changes subsequent add() initial mode.
            mode_state["raw"] = True
            view.unfold.reset_mock()
            view.fold.reset_mock()
            mgr.add(region, "# Title\n\nBody")
            self.assertTrue(mgr.entries[-1]["raw_mode"])
            view.unfold.assert_called_once_with(region)
            view.fold.assert_not_called()
            self.assertEqual(len(raw_phantom_set.update.call_args[0][0]), 2)
            self.assertEqual(len(preview_phantom_set.update.call_args[0][0]), 3)

            # Explicit raw_mode overrides getter.
            view.unfold.reset_mock()
            view.fold.reset_mock()
            mgr.add(region, "# Title\n\nBody", raw_mode=False)
            self.assertFalse(mgr.entries[-1]["raw_mode"])
            view.unfold.assert_called_once_with(region)
            view.fold.assert_called_once_with(region)
            self.assertEqual(len(preview_phantom_set.update.call_args[0][0]), 6)


class SegmentedSwitchRenderingTests(unittest.TestCase):
    def test_switch_pill_raw_mode_active_and_target(self):
        html_top = _render_switch_html("k1", is_raw=True, is_top=True)
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
        html_top = _render_switch_html("k2", is_raw=False, is_top=True)
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
        top_html = _raw_toggle_html("k3", is_top=True)
        self.assertIn("chatview-raw-toggle-top", top_html)
        self.assertIn('class="chat-raw-top-bar"', top_html)
        self.assertIn(".chat-raw-top-bar", top_html)
        self.assertIn("<style>", top_html)
        self.assertIn(".switch-pill", top_html)
        self.assertIn(".switch-active", top_html)

        bot_html = _raw_toggle_html("k3", is_top=False)
        self.assertIn("chatview-raw-toggle-bottom", bot_html)
        self.assertIn('class="chat-raw-bottom-bar"', bot_html)
        self.assertIn(".chat-raw-bottom-bar", bot_html)
        self.assertIn("switch-subtle", bot_html)
        self.assertIn(">◫&nbsp;&nbsp;<", bot_html)
        self.assertNotIn("border-top", SWITCH_CSS)
        self.assertNotIn(":hover", SWITCH_CSS)

    def test_preview_toggle_html_structure(self):
        top_html = _preview_toggle_html("k4", is_top=True)
        self.assertIn('class="chat-switch-bar-top"', top_html)
        self.assertIn("toggle:k4:top", top_html)
        self.assertIn(">&nbsp;&nbsp;raw&nbsp;&nbsp;<", top_html)
        self.assertIn('<span class="switch-active">preview</span>', top_html)
        self.assertNotIn("<style>", top_html)

        bot_html = _preview_toggle_html("k4", is_top=False)
        self.assertIn('class="chat-switch-bar-bottom"', bot_html)
        self.assertIn("toggle:k4:bottom", bot_html)
        self.assertIn("switch-subtle", bot_html)
        self.assertIn(">‹›&nbsp;<", bot_html)
        self.assertNotIn("switch-pill", bot_html)
        self.assertNotIn("<style>", bot_html)

    def test_switch_css_included_in_preview_switch_css(self):
        self.assertIn(".switch-pill", PREVIEW_SWITCH_CSS)
        self.assertIn(".switch-active", PREVIEW_SWITCH_CSS)
        self.assertIn("color: var(--accent);", SWITCH_CSS)
        self.assertIn(".switch-target", PREVIEW_SWITCH_CSS)
        self.assertIn(".switch-subtle", PREVIEW_SWITCH_CSS)
        self.assertIn("text-decoration: none;", SWITCH_CSS)
        self.assertIn(".chat-switch-bar-top", PREVIEW_SWITCH_CSS)
        self.assertIn(".chat-switch-bar-bottom", PREVIEW_SWITCH_CSS)
        self.assertIn(".termmate-response-switch-bar", PREVIEW_SWITCH_CSS)
        self.assertIn(".chat-raw-bottom-bar", SWITCH_CSS)
        self.assertNotIn(".switch-pill", RESPONSE_CSS)
        self.assertNotIn(".switch-subtle", RESPONSE_CSS)


if __name__ == "__main__":
    unittest.main()
