"""
Chat view rendering helpers and quick popup manager for TermMate.
"""

import html
import logging
import webbrowser

import sublime

from .md_render import preprocess_response_markdown

try:
    import mdpopups
    HAS_MDPOPUPS = True
except ImportError:
    mdpopups = None
    HAS_MDPOPUPS = False

LOG = logging.getLogger("TermMate")

ANCHOR_KEY = "termmate_quick_popup_anchor"
SLOT_PHANTOM_KEY = "termmate_quick_popup_slot"


def extract_diff_fold_ranges(text, limit, preview_lines=0):
    """Return relative offset ranges (start, end) for complete, over-limit fenced diff blocks.

    When preview_lines > 0, the opening fence and up to preview_lines remain
    visible, folding remaining lines on an indented new line. When preview_lines
    is 0, all diff content is folded directly at the end of the diff line.

    Incomplete fences are deliberately ignored because chat output may arrive
    in streaming chunks.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []

    if (
        not isinstance(preview_lines, int)
        or isinstance(preview_lines, bool)
        or preview_lines < 0
    ):
        preview_lines = 0

    ranges = []
    opening = None
    diff_lines = 0
    fold_start = None
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        if opening is None:
            if (len(content) - len(content.lstrip(" ")) <= 3
                    and stripped.startswith("```")
                    and stripped.lstrip("`").strip() == "diff"):
                opening_indent = len(content) - len(content.lstrip(" "))
                fence_len = len(stripped) - len(stripped.lstrip("`"))
                opening = (fence_len, opening_indent)
                diff_lines = 0
                fold_start = offset + len(content) if preview_lines == 0 else None
        elif stripped and set(stripped) == {"`"}:
            fence_length = len(stripped)
            if fence_length >= opening[0]:
                if diff_lines > limit and fold_start is not None:
                    end_pos = offset + len(content)
                    if end_pos > fold_start:
                        ranges.append((fold_start, end_pos))
                opening = None
                fold_start = None
        else:
            diff_lines += 1
            if preview_lines > 0 and diff_lines == preview_lines + 1:
                line_indent = len(content) - len(content.lstrip(" "))
                fold_start = offset + min(opening[1], line_indent)
        offset += len(line)
    return ranges


COMPACT_SLOT_HEIGHT = 72


class QuickOutputPopup:
    """Manages an mdpopups popup attached to a source editor view."""

    def __init__(self, window, source_view, chat_view_id, prompt=None, chat_title=None):
        self.window = window
        self.source_view = source_view
        self.chat_view_id = chat_view_id
        self.chat_title = chat_title

        self.buffer = ""
        self.is_active = True
        self.is_visible = False
        self.is_finished = False
        self.is_expanded = False
        self.user_dismissed = False
        self._is_updating_popup = False
        self._render_timer_scheduled = False
        self._last_viewport_pos = None
        self._scroll_restore_generation = 0
        self.max_w, self.full_height = self._calculate_dimensions()
        self.slot_height = COMPACT_SLOT_HEIGHT
        self.popup_css = self._build_popup_css()
        self._last_slot_state = None
        self._last_rendered_state = None
        self.phantom_set = (
            sublime.PhantomSet(source_view, SLOT_PHANTOM_KEY)
            if hasattr(sublime, "PhantomSet")
            else None
        )

        sel = source_view.sel()[0] if source_view.sel() else sublime.Region(0, 0)
        self.target_point = self._calculate_target_point(source_view, sel)

        source_view.add_regions(
            ANCHOR_KEY,
            [sublime.Region(self.target_point, self.target_point)],
            flags=sublime.HIDDEN | sublime.PERSISTENT,
        )

    @staticmethod
    def _calculate_target_point(source_view, sel):
        """Align to column 0 of cursor line, or 2 lines below top if cursor off-screen."""
        if not source_view or not source_view.is_valid():
            return 0

        cursor_point = sel.b if isinstance(getattr(sel, "b", None), int) else sel.end()
        visible = source_view.visible_region()

        # If cursor is within visible viewport, align to line start (column 0)
        if visible.begin() <= cursor_point <= visible.end():
            cursor_line = source_view.line(cursor_point)
            line_begin = cursor_line.begin()
            return line_begin if isinstance(line_begin, int) else cursor_point

        # If cursor is off-screen, anchor 2 lines below top of visible viewport
        top_row, _ = source_view.rowcol(visible.begin())
        max_row, _ = source_view.rowcol(source_view.size())
        target_row = min(max_row, top_row + 2)
        return source_view.text_point(target_row, 0)

    def _calculate_dimensions(self):
        """Calculate responsive max_width and max_height based on viewport extent."""
        if not self.source_view or not self.source_view.is_valid():
            return 800, 420

        try:
            viewport_w, viewport_h = self.source_view.viewport_extent()
            max_w = min(850, max(400, int(viewport_w - 40)))
            max_h = min(560, max(260, int(viewport_h * 0.50)))
            return max_w, max_h
        except Exception:
            return 800, 420

    def _get_anchor_point(self):
        if self.source_view and self.source_view.is_valid():
            regions = self.source_view.get_regions(ANCHOR_KEY)
            if regions:
                return regions[0].b
        return self.target_point

    def _get_viewport_pos(self):
        if not self.source_view or not self.source_view.is_valid():
            return None
        try:
            pos = self.source_view.viewport_position()
            if isinstance(pos, (tuple, list)) and len(pos) >= 2:
                if isinstance(pos[0], (int, float)) and isinstance(pos[1], (int, float)):
                    return (float(pos[0]), float(pos[1]))
        except Exception:
            pass
        return None

    def _ensure_visible_space(self):
        """Scroll anchor to center if remaining space below is insufficient."""
        if not self.source_view or not self.source_view.is_valid():
            return
        try:
            point = self._get_anchor_point()
            layout_pos = self.source_view.text_to_layout(point)
            if not isinstance(layout_pos, (tuple, list)) or len(layout_pos) < 2:
                return
            point_y = layout_pos[1]
            vp = self.source_view.viewport_position()
            ve = self.source_view.viewport_extent()
            if (
                not isinstance(vp, (tuple, list))
                or len(vp) < 2
                or not isinstance(ve, (tuple, list))
                or len(ve) < 2
            ):
                return
            vy, vh = vp[1], ve[1]
            if (
                not isinstance(point_y, (int, float))
                or not isinstance(vy, (int, float))
                or not isinstance(vh, (int, float))
            ):
                return
            space_below = vh - (point_y - vy)
            if space_below < self.full_height + 40:
                offset_px = 60
                target_vy = max(0.0, point_y - (vh * 0.50 - offset_px))
                self.source_view.set_viewport_position((vp[0], target_vy), False)
        except Exception:
            pass

    def open(self):
        if not HAS_MDPOPUPS or not self.source_view or not self.source_view.is_valid():
            return
        self._ensure_visible_space()
        self._do_render()

    def resume(self):
        """Resume displaying the popup if the view regained focus and wasn't dismissed."""
        if not self.is_active or self.user_dismissed:
            return
        self.is_visible = False
        self._last_rendered_state = None
        self._do_render()

    def _expand_to_full(self):
        if self.is_expanded:
            return
        self.is_expanded = True
        self.slot_height = self.full_height
        self.popup_css = self._build_popup_css()

    def append(self, text):
        if not self.is_active or self.is_finished or self.user_dismissed:
            return
        self.buffer += text
        if not self.is_expanded and self.buffer.strip():
            self._expand_to_full()
        self._schedule_render()

    def finish(self):
        if not self.is_active:
            return
        self.is_finished = True
        self._render_timer_scheduled = False
        if not self.is_expanded and self.buffer.strip():
            self._expand_to_full()
        if not self.user_dismissed:
            self._do_render()

    def close(self):
        self.is_active = False
        self._cleanup_anchors()
        if self.source_view and self.source_view.is_valid() and HAS_MDPOPUPS:
            try:
                mdpopups.hide_popup(self.source_view)
            except Exception:
                pass
        self.is_visible = False
        self._last_rendered_state = None

    def _cleanup_anchors(self):
        if self.source_view and self.source_view.is_valid():
            self.source_view.erase_regions(ANCHOR_KEY)
        self._clear_slot_phantom()

    def _clear_slot_phantom(self):
        if self.phantom_set is not None and self.source_view and self.source_view.is_valid():
            try:
                self.phantom_set.update([])
            except Exception:
                pass
        self._last_slot_state = None

    def _is_source_view_active(self):
        win = self.window or (self.source_view.window() if self.source_view else None)
        active_view = win.active_view() if win else None
        if (
            active_view is not None
            and active_view != self.source_view
            and isinstance(active_view.id(), int)
            and active_view.id() != self.source_view.id()
        ):
            return False
        return True

    def _is_point_visible(self, point=None):
        if not self.source_view or not self.source_view.is_valid():
            return False
        if point is None:
            point = self._get_anchor_point()
        visible = self.source_view.visible_region()
        if not isinstance(visible.begin(), int):
            return True
        return visible.begin() <= point <= visible.end()

    def _update_slot_phantom(self, point):
        if self.phantom_set is None or not self.source_view or not self.source_view.is_valid():
            return
        state = self.slot_height
        if state == self._last_slot_state:
            return
        self._last_slot_state = state
        slot_html = (
            '<body id="termmate-quick-slot">'
            f'<div style="padding-bottom:{self.slot_height}px">&#160;</div>'
            "</body>"
        )
        try:
            self.phantom_set.update([
                sublime.Phantom(sublime.Region(point, point), slot_html, sublime.LAYOUT_BLOCK)
            ])
        except Exception:
            pass

    def _schedule_render(self):
        if self._render_timer_scheduled:
            return
        self._render_timer_scheduled = True

        def _step():
            self._render_timer_scheduled = False
            if self.is_active and not self.user_dismissed:
                self._do_render()

        sublime.set_timeout(_step, 180)

    def _build_content(self):
        title = "TermMate"
        if self.chat_title and self.chat_title != "Chat View":
            title = f"TermMate · {self.chat_title}"

        status = "Done" if self.is_finished else "Generating..."
        title_box = (
            f'<span class="quick-title-box">'
            f"<strong>[{title}]</strong> <em>{status}</em>"
            f"</span>"
        )
        header = (
            f"{title_box}"
            '<a class="quick-btn quick-btn-chat" href="action:chat">Open in Chat</a>'
            '<a class="quick-btn quick-btn-close" href="action:close">Close</a>'
            '<span class="quick-spacer">&nbsp;</span>'
        )

        body = self.buffer.strip()
        if not body:
            body = "*Waiting for response...*"

        return f"{header}\n\n---\n\n{body}"

    def _build_popup_css(self):
        min_h = max(50, self.slot_height - 16)
        return f"""
        .termmate-quick-popup {{ padding: 8px 12px; min-height: {min_h}px; }}
        .termmate-quick-popup a {{ text-decoration: none; }}
        .termmate-quick-popup .quick-title-box {{
            display: inline-block;
            width: 210px;
            white-space: nowrap;
            overflow: hidden;
            vertical-align: middle;
        }}
        .termmate-quick-popup .quick-btn {{
            display: inline-block;
            padding: 1px 5px;
            border-radius: 3px;
            font-size: 0.85em;
            font-weight: bold;
            vertical-align: middle;
        }}
        .termmate-quick-popup .quick-btn-chat {{
            margin-left: 20px;
            background-color: var(--foreground);
            color: var(--background);
            border: 1px solid var(--foreground);
        }}
        .termmate-quick-popup .quick-btn-close {{
            margin-left: 29px;
            background-color: color(var(--background) blend(var(--foreground) 75%));
            color: var(--foreground);
            border: 1px solid color(var(--foreground) alpha(0.2));
        }}
        .termmate-quick-popup .quick-spacer {{
            display: inline-block;
            padding-left: 30px;
        }}
        """

    def _do_render(self):
        if not self.source_view or not self.source_view.is_valid() or not HAS_MDPOPUPS:
            return
        if not self._is_source_view_active():
            return

        point = self._get_anchor_point()
        if not self._is_point_visible(point):
            return

        content = self._build_content()
        self._update_slot_phantom(point)
        flags = (
            sublime.COOPERATE_WITH_AUTO_COMPLETE
            | getattr(sublime, "KEEP_ON_SELECTION_MODIFIED", 0)
        )

        render_state = (content, self.slot_height)
        try:
            self._is_updating_popup = True
            if mdpopups.is_popup_visible(self.source_view):
                if render_state != self._last_rendered_state:
                    mdpopups.update_popup(
                        self.source_view,
                        content,
                        md=True,
                        css=self.popup_css,
                        wrapper_class="termmate-quick-popup",
                    )
                    self._last_rendered_state = render_state
                self.is_visible = True
            else:
                mdpopups.show_popup(
                    self.source_view,
                    content,
                    md=True,
                    css=self.popup_css,
                    flags=flags,
                    location=point,
                    max_width=self.max_w,
                    max_height=self.full_height,
                    on_navigate=self._on_navigate,
                    on_hide=self._on_hide,
                    wrapper_class="termmate-quick-popup",
                )
                self._last_rendered_state = render_state
                self.is_visible = True
            self._last_viewport_pos = self._get_viewport_pos()
            sublime.set_timeout(self._sync_viewport_pos, 0)
        except Exception:
            pass
        finally:
            self._is_updating_popup = False

    def _sync_viewport_pos(self):
        if self.is_active and self.is_visible:
            self._last_viewport_pos = self._get_viewport_pos()

    def _schedule_scroll_restore(self, delay=150):
        """Restore popup after scrolling settles if anchor is still visible."""
        if not self.is_active or self.user_dismissed:
            return
        self._scroll_restore_generation += 1
        gen = self._scroll_restore_generation

        def _restore():
            if gen != self._scroll_restore_generation:
                return
            if not self.is_active or self.user_dismissed or self.is_visible:
                return
            if not self.source_view or not self.source_view.is_valid():
                return
            point = self._get_anchor_point()
            if self._is_source_view_active() and self._is_point_visible(point):
                self._do_render()

        sublime.set_timeout(_restore, delay)

    def _on_navigate(self, href):
        if href == "action:chat":
            self.user_dismissed = True
            self.close()
            if self.window and self.chat_view_id:
                chat_view = next(
                    (v for v in self.window.views() if v.id() == self.chat_view_id),
                    None,
                )
                if chat_view:
                    self.window.focus_view(chat_view)
        elif href == "action:close":
            self.user_dismissed = True
            self.close()

    def _on_hide(self):
        self.is_visible = False
        self._last_rendered_state = None
        if not self.is_active or self.user_dismissed:
            return
        if self._is_updating_popup:
            return

        # Check if popup was hidden due to viewport scrolling
        current_pos = self._get_viewport_pos()
        scrolled = False
        if current_pos is not None and self._last_viewport_pos is not None:
            dx = abs(current_pos[0] - self._last_viewport_pos[0])
            dy = abs(current_pos[1] - self._last_viewport_pos[1])
            if dx > 2.0 or dy > 2.0:
                scrolled = True

        if scrolled:
            point = self._get_anchor_point()
            if self._is_point_visible(point):
                self._schedule_scroll_restore()
            elif self.is_finished:
                self.close()
            return

        # Hide while source view and anchor are visible means user dismissed
        # the popup (e.g. click in editor or Esc).
        if self._is_source_view_active() and self._is_point_visible():
            self.user_dismissed = True
            self.close()


class QuickOutputPopupManager:
    """Manages mdpopups instances for inline quick prompt output."""

    def __init__(self):
        self._active_popups = {}

    @property
    def is_available(self):
        return HAS_MDPOPUPS

    def start(self, window, source_view, chat_view_id, prompt=None, chat_title=None):
        self.close(chat_view_id)
        popup = QuickOutputPopup(
            window, source_view, chat_view_id, prompt=prompt, chat_title=chat_title
        )
        self._active_popups[chat_view_id] = popup
        popup.open()
        return popup

    def on_view_activated(self, view):
        if not view or not view.is_valid():
            return
        for popup in list(self._active_popups.values()):
            if popup.source_view and popup.source_view.id() == view.id():
                if popup.is_active and not popup.user_dismissed and not popup.is_visible:
                    popup.resume()

    def on_view_closed(self, view):
        if not view:
            return
        for chat_id, popup in list(self._active_popups.items()):
            if popup.source_view and popup.source_view.id() == view.id():
                popup.close()
                self._active_popups.pop(chat_id, None)

    def append(self, chat_view_id, text):
        popup = self._active_popups.get(chat_view_id)
        if popup:
            popup.append(text)

    def finish(self, chat_view_id):
        popup = self._active_popups.get(chat_view_id)
        if popup:
            popup.finish()

    def close(self, chat_view_id):
        popup = self._active_popups.pop(chat_view_id, None)
        if popup:
            popup.close()

    def get(self, chat_view_id):
        return self._active_popups.get(chat_view_id)


quick_popup_manager = QuickOutputPopupManager()


RESPONSE_PHANTOM_KEY = "chatview_responses"
RESPONSE_RAW_PHANTOM_KEY = "chatview_responses_raw"

SWITCH_CSS = """
div.mdpopups.termmate-response-switch-bar .switch-pill,
div.mdpopups .switch-pill,
.termmate-response-switch-bar .switch-pill,
.switch-pill {
    display: inline-block;
    border: 1px solid color(var(--foreground) alpha(0.32));
    background-color: color(var(--background) blend(var(--foreground) 90%));
    border-radius: 3px;
    padding: 1px;
    font-size: 0.72em;
    font-family: var(--font-mono);
    line-height: 1.35;
}
div.mdpopups.termmate-response-switch-bar .switch-active,
div.mdpopups .switch-active,
.termmate-response-switch-bar .switch-active,
.switch-active {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 2px;
    font-weight: bold;
    color: var(--accent);
    background-color: color(var(--background) blend(var(--foreground) 76%));
    line-height: 1.35;
}
.termmate-response-switch-bar a,
.chat-raw-top-bar a,
.chat-raw-bottom-bar a,
a.chat-toggle-link {
    text-decoration: none;
}
div.mdpopups.termmate-response-switch-bar a.switch-target,
div.mdpopups a.switch-target,
.termmate-response-switch-bar a.switch-target,
a.switch-target,
.switch-target {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 2px;
    border: none;
    color: color(var(--foreground) alpha(0.60));
    background-color: transparent;
    text-decoration: none;
    line-height: 1.35;
}
div.mdpopups.termmate-response-switch-bar a.switch-subtle,
div.mdpopups a.switch-subtle,
.termmate-response-switch-bar a.switch-subtle,
.chat-raw-bottom-bar a.switch-subtle,
.chat-switch-bar-bottom a.switch-subtle,
a.switch-subtle,
.switch-subtle {
    display: inline-block;
    margin: 0;
    padding: 1px 10px 1px 0;
    border: none;
    border-radius: 2px;
    font-size: 1.05em;
    font-family: var(--font-mono);
    color: color(var(--foreground) alpha(0.30));
    background-color: transparent;
    text-decoration: none;
    line-height: 1.25;
}
div.mdpopups.termmate-response-switch-bar .chat-switch-bar-top,
.termmate-response-switch-bar .chat-switch-bar-top,
.chat-switch-bar-top {
    text-align: left;
    margin: 0;
    padding: 2px 0 4px 0;
    line-height: 1.45;
}
div.mdpopups.termmate-response-switch-bar .chat-switch-bar-bottom,
.termmate-response-switch-bar .chat-switch-bar-bottom,
.chat-switch-bar-bottom {
    text-align: left;
    margin: 0;
    padding: 1px 0 2px 0;
    line-height: 1.3;
}
.chat-raw-top-bar {
    text-align: left;
    margin: 0;
    padding: 2px 0 4px 0;
    line-height: 1.45;
}
.chat-raw-bottom-bar {
    text-align: left;
    margin: 0;
    padding: 1px 0 2px 0;
    line-height: 1.3;
}
"""

PREVIEW_SWITCH_CSS = """
.mdpopups,
div.mdpopups {
    padding: 0;
    margin: 0;
    background-color: transparent;
}
div.mdpopups.termmate-response-switch-bar,
div.termmate-response-switch-bar,
.mdpopups .termmate-response-switch-bar,
.mdpopups.termmate-response-switch-bar,
.termmate-response-switch-bar {
    display: block;
    padding: 2px 0 6px 0;
    margin: 0;
    border: none;
    background-color: transparent;
    font-size: 1rem;
    line-height: 1.45;
}
""" + SWITCH_CSS

RESPONSE_CSS = """
.mdpopups,
div.mdpopups {
    padding: 0;
    margin: 0;
    background-color: transparent;
}
div.termmate-response-preview,
.mdpopups .termmate-response-preview,
.mdpopups.termmate-response-preview,
.termmate-response-preview {
    display: block;
    border-top: 2px solid color(var(--foreground) alpha(0.20));
    border-bottom: 1px solid color(var(--foreground) alpha(0.10));
    border-left: none;
    border-right: none;
    background-color: color(var(--background) blend(var(--foreground) 95%));
    padding: 0.45rem 0.85rem 0.6rem 0.85rem;
    margin: 0.15rem 0 0.15rem 0;
    line-height: 1.35;
    font-size: 1rem;
}
.termmate-response-preview p {
    margin: 0.45rem 0;
    line-height: 1.35;
}
.termmate-response-preview h1,
.termmate-response-preview h2,
.termmate-response-preview h3,
.termmate-response-preview h4,
.termmate-response-preview h5,
.termmate-response-preview h6 {
    font-weight: bold;
    line-height: 1.3;
    color: var(--foreground);
}
.termmate-response-preview h1 {
    font-size: 1.45rem;
    margin: 0.9rem 0 0.55rem 0;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid color(var(--foreground) alpha(0.18));
}
.termmate-response-preview h2 {
    font-size: 1.25rem;
    margin: 0.8rem 0 0.45rem 0;
    padding-bottom: 0.2rem;
    border-bottom: 1px solid color(var(--foreground) alpha(0.14));
}
.termmate-response-preview h3 {
    font-size: 1.1rem;
    margin: 0.7rem 0 0.4rem 0;
}
.termmate-response-preview h4,
.termmate-response-preview h5,
.termmate-response-preview h6 {
    font-size: 1rem;
    margin: 0.6rem 0 0.35rem 0;
}
.termmate-response-preview ul,
.termmate-response-preview ol {
    margin: 0.45rem 0;
    padding-left: 1.25rem;
}
.termmate-response-preview li {
    margin: 0.2rem 0;
    line-height: 1.35;
}
.termmate-response-preview li > p {
    margin: 0.25rem 0;
}
.termmate-response-preview code {
    font-family: var(--font-mono);
    font-size: 0.9em;
    background-color: color(var(--background) blend(var(--foreground) 88%));
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
}
.termmate-response-preview pre {
    background-color: color(var(--background) blend(var(--foreground) 91%));
    border: 1px solid color(var(--foreground) alpha(0.10));
    border-radius: 5px;
    padding: 0.65rem 0.85rem;
    margin: 0.65rem 0;
    line-height: 1.4;
}
.termmate-response-preview pre code {
    background-color: transparent;
    padding: 0;
    border-radius: 0;
    font-size: 0.92em;
}
.termmate-response-preview blockquote {
    margin: 0.65rem 0;
    padding: 0.4rem 0.85rem;
    border-left: 3px solid color(var(--foreground) alpha(0.28));
    color: color(var(--foreground) blend(var(--background) 70%));
    background-color: color(var(--background) blend(var(--foreground) 92%));
}
.termmate-response-preview hr {
    border: 0;
    height: 1px;
    background-color: color(var(--foreground) alpha(0.16));
    margin: 1rem 0;
}
.termmate-response-preview a {
    color: var(--accent);
    text-decoration: underline;
}
.termmate-response-preview .chat-tool-line {
    margin: 0.25rem 0;
    font-family: var(--font-mono);
    font-size: 0.92em;
}
.termmate-response-preview .chat-tool-bullet {
    color: var(--accent);
    padding-right: 2px;
}
.termmate-response-preview .chat-tool-name {
    color: var(--bluish);
    font-weight: bold;
}
.termmate-response-preview a.chat-tool-link {
    color: var(--cyanish);
    text-decoration: none;
}
"""


def _render_switch_html(key, is_raw=True, is_top=True):
    """Build top segmented switch pill or bottom subtle single-target link."""
    esc_key = html.escape(key, quote=True)
    suffix = ":top" if is_top else ":bottom"
    target_href = f"toggle:{esc_key}{suffix}"
    if not is_top:
        label = "◫&nbsp;&nbsp;" if is_raw else "‹›&nbsp;"
        title = "Switch to Markdown preview" if is_raw else "Switch to raw Markdown"
        return (
            f'<a class="switch-subtle chat-toggle-link" href="{target_href}" '
            f'title="{title}">{label}</a>'
        )
    raw_label = "&nbsp;&nbsp;raw&nbsp;&nbsp;"
    preview_label = "preview"
    if is_raw:
        left = f'<span class="switch-active">{raw_label}</span>'
        right = (
            f'<a class="switch-target chat-toggle-link" href="{target_href}" '
            f'title="Switch to Markdown preview">{preview_label}</a>'
        )
    else:
        left = (
            f'<a class="switch-target chat-toggle-link" href="{target_href}" '
            f'title="Switch to raw Markdown">{raw_label}</a>'
        )
        right = f'<span class="switch-active">{preview_label}</span>'
    return f'<span class="switch-pill">{left}{right}</span>'


def _raw_toggle_html(key, is_top=True):
    """Build a segmented switch header or subtle footer toggle for raw mode."""
    switch_html = _render_switch_html(key, is_raw=True, is_top=is_top)
    style_block = f"<style>{SWITCH_CSS}</style>"
    if is_top:
        return (
            '<body id="chatview-raw-toggle-top" style="margin:0;padding:2px 0 6px 0;">'
            f"{style_block}"
            f'<div class="chat-raw-top-bar">{switch_html}</div>'
            "</body>"
        )
    return (
        '<body id="chatview-raw-toggle-bottom" style="margin:0;padding:1px 0 2px 0;">'
        f"{style_block}"
        f'<div class="chat-raw-bottom-bar">{switch_html}</div>'
        "</body>"
    )


def _preview_toggle_html(key, is_top=True):
    """Build a standalone segmented switch bar for preview mode."""
    pill_html = _render_switch_html(key, is_raw=False, is_top=is_top)
    bar_cls = "chat-switch-bar-top" if is_top else "chat-switch-bar-bottom"
    return f'<div class="{bar_cls}">{pill_html}</div>'


def resolve_markdown_preview_raw_mode(mode):
    """Return True when markdown_preview_mode starts unfolded in raw mode."""
    if isinstance(mode, str) and mode.strip().lower() == "preview":
        return False
    return True


class ResponsePhantomManager:
    """Own agent response regions and their raw/preview toggle phantoms."""

    REGION_PREFIX = "term_chat_response_"

    def __init__(
        self,
        view,
        max_width_getter=None,
        on_navigate_link=None,
        default_raw_mode_getter=None,
    ):
        self.view = view
        self.max_width_getter = max_width_getter
        self.on_navigate_link = on_navigate_link
        self.default_raw_mode_getter = default_raw_mode_getter
        self.entries = []
        self.next_id = 0
        self.raw_phantom_set = (
            sublime.PhantomSet(view, RESPONSE_RAW_PHANTOM_KEY)
            if hasattr(sublime, "PhantomSet")
            else None
        )
        self.phantom_set = (
            mdpopups.PhantomSet(view, RESPONSE_PHANTOM_KEY)
            if HAS_MDPOPUPS and hasattr(mdpopups, "PhantomSet")
            else None
        )

    @property
    def is_available(self):
        return bool(
            HAS_MDPOPUPS
            and self.phantom_set is not None
            and self.raw_phantom_set is not None
        )

    def _get_max_width(self):
        if self.max_width_getter is not None:
            try:
                value = (
                    self.max_width_getter()
                    if callable(self.max_width_getter)
                    else self.max_width_getter
                )
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
            except Exception:
                pass
        return 100

    def _prepare_markdown(self, md_text):
        return preprocess_response_markdown(
            md_text,
            max_width=self._get_max_width(),
        ).strip()

    def _resolve_raw_mode(self, raw_mode=None):
        if raw_mode is not None:
            return bool(raw_mode)
        if self.default_raw_mode_getter is not None:
            try:
                value = (
                    self.default_raw_mode_getter()
                    if callable(self.default_raw_mode_getter)
                    else self.default_raw_mode_getter
                )
                return bool(value)
            except Exception:
                pass
        return True

    def _apply_fold_state(self, region, raw_mode):
        self.view.unfold(region)
        if not raw_mode:
            self.view.fold(region)

    def add(self, region, md_text, raw_mode=None):
        """Register the agent response region and render its toggle/preview phantoms."""
        if not self.is_available or not md_text or not md_text.strip():
            return None
        if region.empty():
            return None

        use_raw = self._resolve_raw_mode(raw_mode)
        key = f"{self.REGION_PREFIX}{self.next_id}"
        self.next_id += 1
        self.view.add_regions(key, [region], flags=sublime.HIDDEN)
        processed_md = self._prepare_markdown(md_text)
        self.entries.append({
            "key": key,
            "md_text": md_text,
            "processed_md": processed_md,
            "raw_mode": use_raw,
        })
        self._apply_fold_state(region, use_raw)
        self._update()
        return key

    def toggle_entry(self, key, is_top=True):
        """Toggle a single response entry between rendered preview and raw text."""
        target = next((e for e in self.entries if e["key"] == key), None)
        if target is None:
            return
        regions = self.view.get_regions(key)
        pre_screen_y = None
        if not is_top and regions:
            pre_bounds = self._entry_layout_bounds(regions[0])
            pre_vp = self._viewport_geometry()
            if pre_bounds is not None and pre_vp is not None:
                pre_screen_y = pre_bounds[1] - pre_vp[1]

        target["raw_mode"] = not target["raw_mode"]
        if regions:
            self._apply_fold_state(regions[0], target["raw_mode"])
        self._update()

        def _locate(retries=0):
            if not self.view or (
                hasattr(self.view, "is_valid") and not self.view.is_valid()
            ):
                return
            entry = next((e for e in self.entries if e["key"] == key), None)
            if entry is None:
                return
            current_regions = self.view.get_regions(key)
            if not current_regions:
                return
            reg = current_regions[0]
            if is_top:
                top_pt = self.view.line(reg.begin()).begin()
                self.view.show(top_pt, False)
                return
            if not self._scroll_entry_bottom(
                reg,
                pre_screen_y,
                is_raw=entry["raw_mode"],
                can_retry=retries < 1,
            ):
                sublime.set_timeout(lambda: _locate(retries + 1), 30)

        sublime.set_timeout(_locate, 25)

    @staticmethod
    def _as_float_pair(value):
        if (
            isinstance(value, (tuple, list))
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
            and not isinstance(value[0], bool)
            and not isinstance(value[1], bool)
        ):
            return (float(value[0]), float(value[1]))
        return None

    def _layout_y(self, point):
        fn = getattr(self.view, "text_to_layout", None)
        if not callable(fn):
            return None
        pair = self._as_float_pair(fn(point))
        return pair[1] if pair is not None else None

    def _viewport_geometry(self):
        pos_fn = getattr(self.view, "viewport_position", None)
        ext_fn = getattr(self.view, "viewport_extent", None)
        if not callable(pos_fn) or not callable(ext_fn):
            return None
        pos = self._as_float_pair(pos_fn())
        ext = self._as_float_pair(ext_fn())
        if pos is None or ext is None:
            return None
        return (pos[0], pos[1], ext[0], ext[1])

    def _line_height(self):
        fn = getattr(self.view, "line_height", None)
        val = fn() if callable(fn) else None
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
        return 20.0

    def _entry_bottom_y(self, region, top_y):
        """Resolve Y below any LAYOUT_BLOCK phantoms anchored at region.end()."""
        bot_pt = region.end()
        bot_line_end = self.view.line(bot_pt).end() if hasattr(self.view, "line") else bot_pt
        view_size = self.view.size() if hasattr(self.view, "size") else None
        if not isinstance(view_size, int) or isinstance(view_size, bool):
            return None
        if bot_line_end < view_size:
            after_y = self._layout_y(min(view_size, bot_line_end + 1))
            if after_y is not None and after_y >= top_y:
                return after_y
        ext_fn = getattr(self.view, "layout_extent", None)
        if callable(ext_fn):
            ext = self._as_float_pair(ext_fn())
            if ext is not None:
                return max(top_y, ext[1])
        return None

    def _entry_layout_bounds(self, region):
        top_pt = self.view.line(region.begin()).begin()
        top_y = self._layout_y(top_pt)
        if top_y is None:
            return None
        bot_y = self._entry_bottom_y(region, top_y)
        if bot_y is None:
            return None
        return (top_y, bot_y)

    def _clamp_scroll_past_end(self, target_vy, vh):
        settings_fn = getattr(self.view, "settings", None)
        settings = settings_fn() if callable(settings_fn) else None
        if settings is not None and settings.get("scroll_past_end", True) is False:
            ext_fn = getattr(self.view, "layout_extent", None)
            ext = self._as_float_pair(ext_fn()) if callable(ext_fn) else None
            if ext is not None:
                return min(target_vy, max(0.0, ext[1] - vh))
        return target_vy

    def _scroll_entry_bottom(
        self, reg, pre_screen_y, is_raw=True, can_retry=False
    ):
        """Scroll viewport so the bottom of reg and its bottom toggle stay visible."""
        bounds = self._entry_layout_bounds(reg)
        vp = self._viewport_geometry()
        set_vp = getattr(self.view, "set_viewport_position", None)
        if bounds is None or vp is None or not callable(set_vp):
            self.view.show(reg.end(), False)
            return True

        top_y, bot_y = bounds
        vx, _, _, vh = vp
        if vh <= 0:
            self.view.show(reg.end(), False)
            return True

        line_h = self._line_height()
        msg_h = max(0.0, bot_y - top_y)
        if not is_raw and can_retry and msg_h <= line_h * 1.5:
            return False

        top_pad = min(line_h, vh * 0.1)
        bottom_pad = min(line_h * 1.5, vh * 0.15)
        max_offset = max(line_h, vh - bottom_pad)
        desired = max_offset
        if pre_screen_y is not None and line_h <= pre_screen_y <= vh:
            clamped_pre = min(max_offset, pre_screen_y)
            if msg_h + top_pad <= clamped_pre:
                desired = clamped_pre
            elif msg_h + top_pad <= max_offset:
                desired = msg_h + top_pad
            elif clamped_pre >= vh * 0.6:
                desired = clamped_pre

        target_vy = max(0.0, bot_y - desired)
        target_vy = self._clamp_scroll_past_end(target_vy, vh)
        set_vp((vx, target_vy), False)
        return True

    def _on_navigate(self, href):
        if href.startswith("toggle:"):
            parts = href.split(":", 2)
            key = parts[1]
            is_top = (parts[2] == "top") if len(parts) > 2 else True
            self.toggle_entry(key, is_top=is_top)
        elif self.on_navigate_link:
            self.on_navigate_link(href)
        elif href.startswith(("http://", "https://", "mailto:")):
            webbrowser.open_new_tab(href)

    def _update(self):
        if not self.is_available or not self.view:
            return
        if hasattr(self.view, "is_valid") and not self.view.is_valid():
            return
        raw_phantoms = []
        preview_phantoms = []
        active_entries = []
        for entry in self.entries:
            key = entry["key"]
            regions = self.view.get_regions(key)
            if not regions:
                continue
            region = regions[0]
            active_entries.append(entry)
            if entry["raw_mode"]:
                top_anchor, top_layout = self._raw_top_layout(region)
                raw_phantoms.append(sublime.Phantom(
                    top_anchor,
                    _raw_toggle_html(key, is_top=True),
                    top_layout,
                    on_navigate=self._on_navigate,
                ))
                bot_pt = region.end()
                bot_anchor = sublime.Region(bot_pt, bot_pt)
                raw_phantoms.append(sublime.Phantom(
                    bot_anchor,
                    _raw_toggle_html(key, is_top=False),
                    sublime.LAYOUT_BLOCK,
                    on_navigate=self._on_navigate,
                ))
            else:
                anchor = sublime.Region(region.end(), region.end())
                preview_phantoms.extend([
                    self._make_preview_switch_phantom(anchor, key, is_top=True),
                    mdpopups.Phantom(
                        anchor,
                        entry["processed_md"],
                        sublime.LAYOUT_BLOCK,
                        md=True,
                        css=RESPONSE_CSS,
                        on_navigate=self._on_navigate,
                        wrapper_class="termmate-response-preview",
                    ),
                    self._make_preview_switch_phantom(anchor, key, is_top=False),
                ])
        self.entries = active_entries
        self.raw_phantom_set.update(raw_phantoms)
        self.phantom_set.update(preview_phantoms)

    def _raw_top_layout(self, region):
        """Place raw top switch as a block below ⋅⋅⋅ without adding buffer newlines."""
        start = region.begin()
        line_reg = self.view.line(start)
        if hasattr(self.view, "substr"):
            line_text = self.view.substr(line_reg).strip()
            if line_text and line_text != "⋅⋅⋅":
                if line_reg.begin() > 0:
                    prev_end = line_reg.begin() - 1
                    return sublime.Region(prev_end, prev_end), sublime.LAYOUT_BLOCK
                end_pt = line_reg.end()
                return sublime.Region(end_pt, end_pt), sublime.LAYOUT_INLINE
        top_pt = line_reg.end()
        return sublime.Region(top_pt, top_pt), sublime.LAYOUT_BLOCK

    def _make_preview_switch_phantom(self, anchor, key, is_top=True):
        return mdpopups.Phantom(
            anchor,
            _preview_toggle_html(key, is_top=is_top),
            sublime.LAYOUT_BLOCK,
            md=False,
            css=PREVIEW_SWITCH_CSS,
            on_navigate=self._on_navigate,
            wrapper_class="termmate-response-switch-bar",
        )

    def refresh(self):
        """Re-render all response phantoms (e.g. after theme/width changes)."""
        for entry in self.entries:
            entry["processed_md"] = self._prepare_markdown(entry["md_text"])
        self._update()

    def truncate(self, cut_point):
        """Unfold and drop response entries at or after cut_point (rewind)."""
        kept = []
        for entry in self.entries:
            key = entry["key"]
            regions = self.view.get_regions(key)
            if regions and regions[0].end() < cut_point:
                kept.append(entry)
            else:
                if regions:
                    self.view.unfold(regions[0])
                self.view.erase_regions(key)
        self.entries = kept
        self._update()

    def clear(self):
        """Unfold and remove all response regions and phantoms."""
        for entry in self.entries:
            key = entry["key"]
            regions = self.view.get_regions(key)
            if regions:
                self.view.unfold(regions[0])
            self.view.erase_regions(key)
        self.entries = []
        if self.raw_phantom_set is not None:
            self.raw_phantom_set.update([])
        if self.phantom_set is not None:
            self.phantom_set.update([])
