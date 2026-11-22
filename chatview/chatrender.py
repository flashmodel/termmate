"""
Chat view rendering helpers and quick popup manager for TermMate.
"""

import html
import logging
import sys

import sublime

from .md_render import (
    FOLD_TERMINATOR,
    preprocess_response_markdown,
    render_ordered_lists_as_html,
    strip_list_separators,
)

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

TOP_SWITCH_CSS = """
.chat-raw-top-bar,
.chat-switch-bar-top {
    text-align: left;
    margin: 0;
    padding: 2px 0 0 0;
    line-height: 1.45;
}
.chat-raw-top-bar a,
.chat-switch-bar-top a,
a.chat-toggle-link {
    text-decoration: none;
}
.switch-pill {
    display: inline-block;
    border: 1px solid color(var(--foreground) alpha(0.32));
    background-color: color(var(--background) blend(var(--foreground) 90%));
    border-radius: 3px;
    padding: 2px;
    font-size: 0.72em;
    font-family: var(--font-mono);
    line-height: 1.35;
}
.switch-active {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 2px;
    font-weight: bold;
    color: var(--accent);
    background-color: color(var(--background) blend(var(--foreground) 76%));
    line-height: 1.35;
}
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
"""

BOTTOM_SWITCH_CSS = """
.chat-raw-top-inline,
.chat-raw-bottom-inline,
.chat-preview-top-inline {
    margin: 0;
    padding: 0;
    line-height: 1;
}
.chat-raw-top-inline a,
.chat-raw-bottom-inline a,
.chat-preview-top-inline a,
a.chat-toggle-link {
    text-decoration: none;
}
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
.chat-raw-top-inline a.switch-subtle,
.chat-raw-top-inline .switch-subtle,
.chat-raw-bottom-inline a.switch-subtle,
.chat-raw-bottom-inline .switch-subtle,
.chat-preview-top-inline a.switch-subtle,
.chat-preview-top-inline .switch-subtle {
    line-height: 1;
}
.chat-raw-top-inline a.switch-subtle,
.chat-raw-top-inline .switch-subtle {
    padding: 0 0.5em 0 1.3em;
}
.chat-raw-bottom-inline a.switch-subtle,
.chat-raw-bottom-inline .switch-subtle {
    padding: 0 0.4em 0 0.8em;
    color: color(var(--cyanish) alpha(0.65));
}
.chat-preview-top-inline a.switch-subtle,
.chat-preview-top-inline .switch-subtle {
    padding: 0 8px 0 0;
}
"""

SWITCH_CSS = TOP_SWITCH_CSS + BOTTOM_SWITCH_CSS

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
    border-top: none;
    border-bottom: 1px solid color(var(--foreground) alpha(0.10));
    border-left: none;
    border-right: none;
    background-color: var(--background);
    padding: 0.45rem 0.85rem 0.25rem 0;
    margin: 0.15rem 0 0 0;
    font-family: var(--font-mono);
    line-height: 1.35;
    font-size: 1rem;
}
div.mdpopups .termmate-response-preview a,
div.mdpopups .termmate-response-preview a code,
.termmate-response-preview a,
.termmate-response-preview a code,
a,
a code {
    color: var(--bluish);
    text-decoration: underline;
}
div.mdpopups .termmate-response-preview ul,
div.mdpopups .termmate-response-preview ol,
.termmate-response-preview ul,
.termmate-response-preview ol {
    padding-left: 1.2rem;
}
div.mdpopups .termmate-response-preview .chat-ol,
.termmate-response-preview .chat-ol,
.chat-ol {
    display: block;
    margin: 0.6rem 0;
    padding-left: 1.2rem;
}
div.mdpopups .termmate-response-preview .chat-ol-num,
.termmate-response-preview .chat-ol-num,
.chat-ol-num {
    display: inline-block;
    margin-left: -1.1rem;
}
div.mdpopups .termmate-response-preview .chat-li,
.termmate-response-preview .chat-li,
.chat-li {
    display: block;
    margin: 0.25rem 0;
    line-height: 1.35;
}
div.mdpopups .termmate-response-preview .chat-li p,
.termmate-response-preview .chat-li p,
.chat-li p {
    margin: 0.3rem 0;
}
div.mdpopups .termmate-response-preview code {
    font-size: 0.9em;
    background-color: color(var(--background) blend(var(--foreground) 90%));
    padding: 0 0.25rem;
    border-radius: 3px;
}
div.mdpopups .termmate-response-preview pre {
    font-size: 0.94em;
    line-height: 1.4;
    background-color: color(var(--background) blend(var(--foreground) 92%));
    border: 1px solid color(var(--foreground) alpha(0.18));
    border-radius: 5px;
    padding: 0.65rem 0.85rem;
    margin: 0.75rem 0;
}
div.mdpopups .termmate-response-preview div.highlight,
div.mdpopups .termmate-response-preview pre code {
    background-color: transparent;
    border-style: none;
    border-width: 0;
    border-radius: 0;
    padding: 0;
    margin: 0;
}
div.mdpopups .termmate-response-preview pre code {
    font-size: 1em;
}
.termmate-response-preview .chat-tool-group,
.chat-tool-group {
    display: block;
    margin: 0;
    padding: 0;
}
.termmate-response-preview .chat-tool-sep,
.chat-tool-sep {
    display: block;
    margin: 0;
    padding: 0;
    font-size: 1rem;
    line-height: 1;
}
div.mdpopups .termmate-response-preview .chat-tool-line,
.termmate-response-preview .chat-tool-line,
.chat-tool-line {
    margin: 0;
    padding: 0;
    font-family: var(--font-mono);
    font-size: 1rem;
    line-height: 1.35;
    color: color(var(--foreground) alpha(0.75));
}
.termmate-response-preview .chat-tool-bullet,
.chat-tool-line .chat-tool-bullet,
.chat-tool-bullet,
.termmate-response-preview .chat-tool-name,
.chat-tool-line .chat-tool-name,
.chat-tool-name {
    {{ 'entity.name.function.chatview' | css('color') }}
    {{ 'entity.name.function.chatview' | css('font-weight') }}
}
.termmate-response-preview .chat-tool-bullet,
.chat-tool-line .chat-tool-bullet,
.chat-tool-bullet {
    padding-right: 2px;
}
div.mdpopups .termmate-response-preview a.chat-tool-link,
.termmate-response-preview a.chat-tool-link,
.chat-tool-line a.chat-tool-link,
a.chat-tool-link,
.chat-tool-link {
    color: color(var(--bluish) alpha(0.85));
    text-decoration: none;
}
"""

PREVIEW_SWITCH_CSS = """
div.mdpopups .termmate-response-card,
div.termmate-response-card,
.termmate-response-card {
    display: block;
    padding: 0;
    margin: 0;
    border: none;
    background-color: transparent;
}
div.mdpopups .termmate-response-top-spacer,
div.termmate-response-top-spacer,
.termmate-response-top-spacer {
    display: block;
    padding: 0.65rem 0 0 0;
    margin: 0;
    border: none;
    background-color: transparent;
}
div.mdpopups .termmate-response-switch-bar,
div.termmate-response-switch-bar,
.termmate-response-switch-bar {
    display: block;
    padding: 2px 0 6px 0;
    margin: 0;
    border: none;
    background-color: transparent;
    font-size: 1rem;
    line-height: 1.45rem;
}
.termmate-response-switch-bar .chat-switch-bar-top,
.chat-switch-bar-top {
    text-align: left;
    margin: 0;
    padding: 2px 0 4px 0;
    line-height: 1.45rem;
}
.termmate-response-switch-bar a,
.chat-switch-bar-top a,
a.chat-toggle-link {
    text-decoration: none;
}
div.mdpopups .switch-pill,
.termmate-response-switch-bar .switch-pill,
.switch-pill {
    display: inline-block;
    border: 1px solid color(var(--foreground) alpha(0.32));
    background-color: color(var(--background) blend(var(--foreground) 90%));
    border-radius: 3px;
    padding: 2px 3px;
    font-size: 0.72rem;
    font-family: var(--font-mono);
    line-height: 1.05rem;
}
div.mdpopups .switch-active,
.termmate-response-switch-bar .switch-active,
.switch-active {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 2px;
    font-weight: bold;
    font-size: 0.72rem;
    color: var(--accent);
    background-color: color(var(--background) blend(var(--foreground) 76%));
    line-height: 0.9rem;
}
div.mdpopups a.switch-target,
.termmate-response-switch-bar a.switch-target,
a.switch-target,
.switch-target {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 2px;
    border: none;
    font-size: 0.72rem;
    color: color(var(--foreground) alpha(0.60));
    background-color: transparent;
    text-decoration: none;
    line-height: 0.9rem;
}
div.mdpopups a.switch-subtle,
.termmate-response-switch-bar a.switch-subtle,
a.switch-subtle,
.switch-subtle {
    display: inline-block;
    margin: 0;
    padding: 1px 10px 1px 0;
    border: none;
    border-radius: 2px;
    font-size: 1.05rem;
    font-family: var(--font-mono);
    color: color(var(--foreground) alpha(0.30));
    background-color: transparent;
    text-decoration: none;
    line-height: 1.25rem;
}
div.mdpopups .termmate-response-preview .chat-card-footer,
.termmate-response-preview .chat-card-footer,
.chat-card-footer {
    display: block;
    text-align: left;
    margin: 0;
    padding: 0;
    line-height: 1.0rem;
}
div.mdpopups .termmate-response-preview .chat-card-footer a.switch-subtle,
.termmate-response-preview .chat-card-footer a.switch-subtle,
.chat-card-footer a.switch-subtle {
    display: inline-block;
    margin: 0;
    padding: 0 10px 0 0;
    border: none;
    border-radius: 2px;
    font-size: 1.05rem;
    font-family: var(--font-mono);
    color: color(var(--cyanish) alpha(0.65));
    background-color: transparent;
    text-decoration: none;
    line-height: 1.05rem;
}
"""

PREVIEW_CARD_CSS = PREVIEW_SWITCH_CSS + RESPONSE_CSS


def _render_switch_html(key, is_raw=True, is_top=True, show_toolbar=False):
    """Build top segmented switch pill or subtle single-target icon link."""
    esc_key = html.escape(key, quote=True)
    suffix = ":top" if is_top else ":bottom"
    target_href = f"toggle:{esc_key}{suffix}"
    if not is_top or not show_toolbar:
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


def _inline_phantom_html(body_id, span_cls, switch_html, css):
    """Wrap a switch link for native minihtml LAYOUT_INLINE phantom rendering."""
    return (
        f'<body id="{body_id}" style="margin:0;padding:0;">'
        f"<style>{css}</style>"
        f'<span class="{span_cls}">{switch_html}</span>'
        "</body>"
    )


def _raw_toggle_html(key, is_top=True, show_toolbar=False, inline=None):
    """Build native minihtml toggle bar for raw mode using position-specific CSS."""
    switch_html = _render_switch_html(
        key, is_raw=True, is_top=is_top, show_toolbar=show_toolbar
    )
    if not is_top:
        return _inline_phantom_html(
            "chatview-raw-toggle-bottom",
            "chat-raw-bottom-inline",
            switch_html,
            BOTTOM_SWITCH_CSS,
        )
    is_inline = (not show_toolbar) if inline is None else bool(inline)
    if is_inline:
        inline_css = SWITCH_CSS if show_toolbar else BOTTOM_SWITCH_CSS
        return _inline_phantom_html(
            "chatview-raw-toggle-top",
            "chat-raw-top-inline",
            switch_html,
            inline_css,
        )
    top_css = TOP_SWITCH_CSS if show_toolbar else SWITCH_CSS
    return (
        '<body id="chatview-raw-toggle-top" style="margin:0;padding:2px 0 6px 0;">'
        f"<style>{top_css}</style>"
        f'<div class="chat-raw-top-bar">{switch_html}</div>'
        "</body>"
    )


def _preview_toggle_html(key, show_toolbar=False):
    """Build a top switch bar fragment for a combined preview card phantom."""
    pill_html = _render_switch_html(
        key, is_raw=False, is_top=True, show_toolbar=show_toolbar
    )
    return (
        '<div class="termmate-response-switch-bar">'
        f'<div class="chat-switch-bar-top">{pill_html}</div>'
        "</div>"
    )


def _preview_footer_html(key):
    """Build an inside-card bottom switch footer for a preview card."""
    switch_html = _render_switch_html(
        key, is_raw=False, is_top=False, show_toolbar=False
    )
    return f'<div class="chat-card-footer">{switch_html}</div>'


def _preview_top_inline_html(key):
    """Build native minihtml inline toggle phantom placed after ⋅⋅⋅⋯ in preview."""
    switch_html = _render_switch_html(
        key, is_raw=False, is_top=True, show_toolbar=False
    )
    return _inline_phantom_html(
        "chatview-preview-toggle-top",
        "chat-preview-top-inline",
        switch_html,
        BOTTOM_SWITCH_CSS,
    )


def resolve_markdown_preview_raw_mode(mode):
    """Return True when markdown_preview_mode starts unfolded in raw mode."""
    if isinstance(mode, str) and mode.strip().lower() == "preview":
        return False
    return True


class _BasePhantomView:
    """Shared PhantomSet lifecycle and line helpers for raw and preview views."""

    def __init__(self, view, on_navigate):
        self.view = view
        self.on_navigate = on_navigate

    def _first_line_is_separator(self, region):
        """Return (is_separator_or_blank, first_line_region) for region.begin()."""
        line_reg = self.view.line(region.begin())
        if not hasattr(self.view, "substr"):
            return True, line_reg
        line_text = self.view.substr(line_reg).strip()
        return (not line_text or line_text == "⋅⋅⋅"), line_reg

    def _set_phantoms(self, store, cls, suffix, key, phantoms):
        ps = store.get(key)
        if ps is None:
            ps = cls(self.view, f"{RESPONSE_PHANTOM_KEY}:{suffix}:{key}")
            store[key] = ps
        ps.update(phantoms)

    @staticmethod
    def _clear_phantom_set(store, key):
        ps = store.pop(key, None)
        if ps is not None:
            ps.update([])


class RawPhantomView(_BasePhantomView):
    """Manage raw-mode top and bottom toggle phantoms for response entries."""

    def __init__(self, view, on_navigate):
        super().__init__(view, on_navigate)
        self.phantom_sets = {}

    def show(self, key, region, show_toolbar=False):
        """Render and mount both top and bottom raw toggle phantoms."""
        phantoms = [
            self._build_top_phantom(key, region, show_toolbar),
            sublime.Phantom(
                self._bottom_anchor(region),
                _raw_toggle_html(key, is_top=False),
                sublime.LAYOUT_INLINE,
                on_navigate=self.on_navigate,
            ),
        ]
        self._set_phantoms(
            self.phantom_sets, sublime.PhantomSet, "raw", key, phantoms
        )

    def show_top_only(self, key, region, show_toolbar=False):
        """Keep only the top raw toggle phantom during deferred fold."""
        if key in self.phantom_sets:
            self.phantom_sets[key].update(
                [self._build_top_phantom(key, region, show_toolbar)]
            )

    def clear(self, key):
        """Clear all raw phantoms associated with key."""
        self._clear_phantom_set(self.phantom_sets, key)

    def _build_top_phantom(self, key, region, show_toolbar):
        top_anchor, top_layout = self._top_layout(
            region, show_toolbar=show_toolbar
        )
        return sublime.Phantom(
            top_anchor,
            _raw_toggle_html(
                key,
                is_top=True,
                show_toolbar=show_toolbar,
                inline=top_layout == sublime.LAYOUT_INLINE,
            ),
            top_layout,
            on_navigate=self.on_navigate,
        )

    def _top_layout(self, region, show_toolbar=False):
        """Place raw top icon inline after ⋅⋅⋅, or as a block for toolbar/fallback."""
        is_sep, line_reg = self._first_line_is_separator(region)
        if not is_sep:
            if line_reg.begin() > 0:
                prev_end = line_reg.begin() - 1
                return sublime.Region(prev_end, prev_end), sublime.LAYOUT_BLOCK
            end_pt = line_reg.end()
            return sublime.Region(end_pt, end_pt), sublime.LAYOUT_INLINE
        top_pt = line_reg.end()
        layout = sublime.LAYOUT_BLOCK if show_toolbar else sublime.LAYOUT_INLINE
        return sublime.Region(top_pt, top_pt), layout

    def _bottom_anchor(self, region):
        """Return zero-width region at the end of the last non-blank line."""
        end = region.end()
        if hasattr(self.view, "substr"):
            if end > region.begin() and hasattr(self.view, "line"):
                # Keep bottom toggle on the blank/NBSP line below a trailing HTML table
                line_reg = self.view.line(end)
                if (
                    line_reg.begin() > region.begin()
                    and not self.view.substr(line_reg).strip()
                ):
                    pt = line_reg.begin()
                    return sublime.Region(pt, pt)
            pt = end
            probe = self.view.substr(pt - 1) if pt > region.begin() else ""
            if probe:
                while pt > region.begin():
                    ch = self.view.substr(pt - 1)
                    if not ch or not ch.isspace():
                        break
                    pt -= 1
                last_pt = (
                    max(region.begin(), pt - 1)
                    if pt > region.begin()
                    else region.begin()
                )
                line_end = (
                    self.view.line(last_pt).end()
                    if hasattr(self.view, "line")
                    else pt
                )
                return sublime.Region(line_end, line_end)
        return sublime.Region(end, end)


class PreviewPhantomView(_BasePhantomView):
    """Manage Markdown preview card and top inline toggle phantoms."""

    def __init__(self, view, on_navigate, max_width_getter=None):
        super().__init__(view, on_navigate)
        self.max_width_getter = max_width_getter
        self.phantom_sets = {}
        self.top_phantom_sets = {}
        self._card_css = (
            self._build_card_css() if view is not None else PREVIEW_CARD_CSS
        )

    def show(self, key, region, md_text, show_toolbar=False):
        """Render and mount preview card and optional top inline toggle."""
        # Sync inline top ‹› before mounting block card at region.end() so
        # Sublime lays out LAYOUT_INLINE ahead of LAYOUT_BLOCK at that point.
        include_top = self._sync_top_phantom(
            key, region, show_toolbar=show_toolbar
        )
        anchor = sublime.Region(region.end(), region.end())
        card_phantom = mdpopups.Phantom(
            anchor,
            self._build_card_html(
                key,
                md_text,
                show_toolbar=show_toolbar,
                include_top=include_top,
            ),
            sublime.LAYOUT_BLOCK,
            md=False,
            css=self._card_css,
            on_navigate=self.on_navigate,
            wrapper_class="termmate-response-card",
        )
        self._set_phantoms(
            self.phantom_sets,
            mdpopups.PhantomSet,
            "preview",
            key,
            [card_phantom],
        )

    def clear(self, key):
        """Clear preview card and top inline toggle phantoms for key."""
        self._clear_phantom_set(self.phantom_sets, key)
        self._clear_phantom_set(self.top_phantom_sets, key)

    _DEFAULT_MONO_FONTS = {"darwin": "Menlo", "win32": "Consolas"}

    def _editor_font_family(self):
        """Return CSS font-family stack matching editor font_face or OS default."""
        raw = self.view.settings().get("font_face", "")
        font_face = (
            "".join(c for c in raw.strip() if c not in '"\';{}')
            if isinstance(raw, str)
            else ""
        )
        if not font_face:
            font_face = self._DEFAULT_MONO_FONTS.get(sys.platform, "")
        prefix = f'"{font_face}", ' if font_face else ""
        return f"{prefix}var(--mdpopups-font-mono), monospace"

    def _build_card_css(self):
        """Append editor-matched font-family override to PREVIEW_CARD_CSS."""
        font_family = self._editor_font_family()
        return (
            f"{PREVIEW_CARD_CSS}\n"
            "div.mdpopups .termmate-response-preview,\n"
            "div.mdpopups .termmate-response-preview code,\n"
            "div.mdpopups .termmate-response-preview pre,\n"
            "div.mdpopups .termmate-response-preview .chat-tool-line,\n"
            ".termmate-response-preview,\n"
            ".termmate-response-preview code,\n"
            ".termmate-response-preview pre,\n"
            ".termmate-response-preview .chat-tool-line {\n"
            f"    font-family: {font_family};\n"
            "}"
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

    def _render_body_html(self, md_text):
        """Convert Markdown body HTML via public mdpopups.md2html."""
        processed_md = self._prepare_markdown(md_text)
        body_html = None
        if HAS_MDPOPUPS and mdpopups is not None and hasattr(mdpopups, "md2html"):
            try:
                candidate = mdpopups.md2html(self.view, processed_md)
                if isinstance(candidate, str):
                    body_html = render_ordered_lists_as_html(candidate)
            except Exception as e:
                LOG.error(f"Error rendering preview via mdpopups.md2html: {e}")
        if body_html is None:
            body_html = f"<pre>{html.escape(strip_list_separators(processed_md))}</pre>"
        return body_html

    def _build_card_html(self, key, md_text, show_toolbar=False, include_top=True):
        body_html = self._render_body_html(md_text)
        footer_html = _preview_footer_html(key)
        preview_block = (
            f'<div class="termmate-response-preview">{body_html}{footer_html}</div>'
        )
        if include_top:
            top_bar = _preview_toggle_html(key, show_toolbar=show_toolbar)
            return f"{top_bar}{preview_block}"
        return f'<div class="termmate-response-top-spacer">{preview_block}</div>'

    def _top_inline_anchor(self, region, show_toolbar=False):
        """Return region.end() for inline ‹› after ⋅⋅⋅⋯, or None for card bar."""
        if show_toolbar:
            return None
        is_sep, _ = self._first_line_is_separator(region)
        if not is_sep:
            return None
        end_pt = region.end()
        return sublime.Region(end_pt, end_pt)

    def _sync_top_phantom(self, key, region, show_toolbar):
        top_anchor = self._top_inline_anchor(
            region, show_toolbar=show_toolbar
        )
        if top_anchor is None:
            self._clear_phantom_set(self.top_phantom_sets, key)
            return True
        top_ph = sublime.Phantom(
            top_anchor,
            _preview_top_inline_html(key),
            sublime.LAYOUT_INLINE,
            on_navigate=self.on_navigate,
        )
        self._set_phantoms(
            self.top_phantom_sets,
            sublime.PhantomSet,
            "preview_top",
            key,
            [top_ph],
        )
        return False


class _ViewportScrollAnchor:
    """Capture and restore viewport scroll offsets across response fold/resize."""

    def __init__(self, view):
        self.view = view

    def can_defer_fold(self, region):
        """Return True when layout geometry supports deferred fold anchoring."""
        return (
            self._viewport_geometry() is not None
            and self._layout_y(region.end()) is not None
        )

    def capture(self, region, is_top):
        """Return viewport-relative Y of the top or bottom anchor before toggling."""
        pre_vp = self._viewport_geometry()
        if pre_vp is None:
            return None
        if is_top:
            anchor_y = self._layout_y(self.view.line(region.begin()).begin())
        else:
            pre_bounds = self._entry_layout_bounds(region)
            anchor_y = pre_bounds[1] if pre_bounds is not None else None
        if anchor_y is None:
            return None
        return anchor_y - pre_vp[1]

    def restore(self, region, is_top, pre_screen_y):
        """Restore viewport scroll position around the toggled response anchor."""
        if is_top:
            self._scroll_top(region, pre_screen_y)
        else:
            self._scroll_bottom(region, pre_screen_y)

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
        bot_line_end = (
            self.view.line(bot_pt).end() if hasattr(self.view, "line") else bot_pt
        )
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

    def _scroll_top(self, reg, pre_screen_y):
        top_pt = self.view.line(reg.begin()).begin()
        top_y = self._layout_y(top_pt)
        vp = self._viewport_geometry()
        set_vp = getattr(self.view, "set_viewport_position", None)
        if top_y is None or vp is None or not callable(set_vp):
            self.view.show(top_pt, False)
            return

        vx, _, _, vh = vp
        if vh <= 0:
            self.view.show(top_pt, False)
            return

        line_h = self._line_height()
        if pre_screen_y is not None:
            desired = max(0.0, min(max(0.0, vh - line_h), pre_screen_y))
        else:
            desired = min(line_h, vh * 0.1)

        target_vy = max(0.0, top_y - desired)
        target_vy = self._clamp_scroll_past_end(target_vy, vh)
        set_vp((vx, target_vy), False)

    def _scroll_bottom(self, reg, pre_screen_y):
        bounds = self._entry_layout_bounds(reg)
        vp = self._viewport_geometry()
        set_vp = getattr(self.view, "set_viewport_position", None)
        if bounds is None or vp is None or not callable(set_vp):
            self.view.show(reg.end(), False)
            return

        top_y, bot_y = bounds
        vx, _, _, vh = vp
        if vh <= 0:
            self.view.show(reg.end(), False)
            return

        line_h = self._line_height()
        msg_h = max(0.0, bot_y - top_y)
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


class ResponsePhantomManager:
    """Own agent response regions and coordinate raw/preview toggle transitions."""

    REGION_PREFIX = "term_chat_response_"
    PERSISTENT_REGIONS_KEY = "term_chat_response_regions"
    _PREVIEW_SETTLE_DELAY_MS = 30

    def __init__(
        self,
        view,
        max_width_getter=None,
        on_navigate_link=None,
        default_raw_mode_getter=None,
        show_toolbar_getter=None,
        table_manager=None,
    ):
        self.view = view
        self.max_width_getter = max_width_getter
        self.on_navigate_link = on_navigate_link
        self.default_raw_mode_getter = default_raw_mode_getter
        self.show_toolbar_getter = show_toolbar_getter
        self.table_manager = table_manager
        self._entries = {}
        self.next_id = 0
        self.raw_view = RawPhantomView(view, self._on_navigate)
        self.preview_view = PreviewPhantomView(
            view,
            self._on_navigate,
            max_width_getter=max_width_getter,
        )
        self._scroll_anchor = _ViewportScrollAnchor(view)

    @property
    def is_available(self):
        return bool(
            HAS_MDPOPUPS
            and mdpopups is not None
            and self.view is not None
            and hasattr(sublime, "PhantomSet")
            and hasattr(sublime, "Phantom")
            and hasattr(mdpopups, "PhantomSet")
            and hasattr(mdpopups, "Phantom")
        )

    def _sync_persistent_regions(self):
        """Persist all active response bounds on the view for restart recovery."""
        if not self._is_view_valid():
            return
        regions = []
        for key in self._entries:
            reg = self._entry_region(key)
            if reg is not None and not reg.empty():
                regions.append(reg)
        if regions:
            flags = getattr(sublime, "HIDDEN", 0) | getattr(sublime, "PERSISTENT", 0)
            self.view.add_regions(
                self.PERSISTENT_REGIONS_KEY, regions, flags=flags
            )
        else:
            self.view.erase_regions(self.PERSISTENT_REGIONS_KEY)

    def resolve_region(self, resp_start, limit):
        """Return normalized response region, keeping terminator after HTML tables."""
        if not self._is_view_valid():
            return None
        content_end = limit
        while (
            content_end > resp_start
            and self.view.substr(content_end - 1) in ("\n", FOLD_TERMINATOR)
        ):
            content_end -= 1
        if content_end <= resp_start:
            return None
        resp_end = content_end
        if (
            self.table_manager is not None
            and self.table_manager.has_table_ending_at(content_end)
            and limit - content_end >= 2
        ):
            # Include the blank/NBSP line for preview folding while leaving >= 1 trailing
            # newline before limit so later appends (e.g. artifact.show) don't shift phantoms
            resp_end = (
                content_end + 2
                if self.view.substr(content_end + 1) == FOLD_TERMINATOR
                and limit - content_end >= 3
                else content_end + 1
            )
        return sublime.Region(resp_start, resp_end)

    def add(self, region, md_text, raw_mode=None, _sync_persistent=True):
        """Register the agent response region and render its toggle/preview phantoms."""
        if (
            not self.is_available
            or not self._is_view_valid()
            or not md_text
            or not md_text.strip()
            or region.empty()
        ):
            return None

        use_raw = self._resolve_raw_mode(raw_mode)
        key = f"{self.REGION_PREFIX}{self.next_id}"
        self.next_id += 1
        self.view.add_regions(key, [region], flags=sublime.HIDDEN)
        entry = {
            "key": key,
            "md_text": md_text,
            "raw_mode": use_raw,
            "generation": 0,
            "pending_fold": False,
        }
        self._entries[key] = entry
        if _sync_persistent:
            self._sync_persistent_regions()
        self._apply_fold_state(region, use_raw)
        self._render_entry(entry, region)
        return key

    def restore(self):
        """Rebuild response entries in raw mode from persisted view regions."""
        if not self.is_available or not self._is_view_valid():
            return 0
        persisted = list(self.view.get_regions(self.PERSISTENT_REGIONS_KEY))
        if not persisted:
            return 0
        existing = {
            (reg.begin(), reg.end())
            for _, _, reg in self._active_entries()
        }
        restored = 0
        for reg in persisted:
            if reg.empty() or (reg.begin(), reg.end()) in existing:
                continue
            md_text = self.view.substr(reg)
            if not md_text or not md_text.strip():
                self.view.unfold(reg)
                continue
            if self.add(reg, md_text, raw_mode=True, _sync_persistent=False) is not None:
                existing.add((reg.begin(), reg.end()))
                restored += 1
        self._sync_persistent_regions()
        return restored

    def refresh(self):
        """Re-render active preview phantoms (e.g. after theme/width changes)."""
        if not self.is_available or not self._is_view_valid():
            return
        for entry in list(self._entries.values()):
            if entry["raw_mode"]:
                continue
            region = self._resolve_active_region(entry)
            if region is not None:
                self._render_entry(entry, region)

    def truncate(self, cut_point):
        """Unfold and drop response entries at or after cut_point (rewind)."""
        kept = {}
        for key, entry in self._entries.items():
            reg = self._entry_region(key)
            if reg is not None and reg.end() < cut_point:
                kept[key] = entry
            else:
                self._dispose_entry(entry, reg)
        self._entries = kept
        self._sync_persistent_regions()
        if self.table_manager is not None:
            self.table_manager.truncate(cut_point)

    def clear(self):
        """Unfold and remove all response regions and phantoms."""
        for entry in self._entries.values():
            self._dispose_entry(entry)
        self._entries = {}
        self._sync_persistent_regions()
        if self.table_manager is not None:
            self.table_manager.clear()

    def has_entries(self):
        """Return True when at least one response region is tracked."""
        return bool(self._entries)

    def _active_entries(self):
        """Return active (key, entry, region) tuples in insertion order."""
        active = []
        for key, entry in list(self._entries.items()):
            reg = self._resolve_active_region(entry)
            if reg is not None:
                active.append((key, entry, reg))
        return active

    def active_entries(self):
        """Return active (key, raw_mode, region) tuples in insertion order."""
        return [
            (key, entry["raw_mode"], reg)
            for key, entry, reg in self._active_entries()
        ]

    @staticmethod
    def _anchor_is_top(bounds, vy, vy_end):
        """Return True to anchor the top of a response, False to anchor the bottom."""
        if bounds is None:
            return True
        top_y, bot_y = bounds
        return top_y >= vy or (bot_y > vy_end and (vy - top_y) <= (bot_y - vy_end))

    def is_point_in_viewport(self, point):
        """Return True if point is visible, False if off-screen, or None if unknown."""
        vp = self._scroll_anchor._viewport_geometry()
        if vp is None or vp[3] <= 0:
            return None
        if point is None:
            return False
        cursor_y = self._scroll_anchor._layout_y(point)
        if cursor_y is None:
            return False
        _, vy, _, vh = vp
        line_h = self._scroll_anchor._line_height()
        return (cursor_y + line_h > vy) and (cursor_y < vy + vh)

    def viewport_target(self):
        """Return (key, is_top) for the active entry with largest viewport overlap."""
        active = self._active_entries()
        if not active:
            return None
        vp = self._scroll_anchor._viewport_geometry()
        if vp is None or vp[3] <= 0:
            return None

        _, vy, _, vh = vp
        vy_end = vy + vh
        best_key = None
        best_is_top = True
        best_overlap = 0.0
        for key, _, reg in active:
            bounds = self._scroll_anchor._entry_layout_bounds(reg)
            if bounds is None:
                continue
            top_y, bot_y = bounds
            overlap = min(bot_y, vy_end) - max(top_y, vy)
            if overlap > 0 and overlap >= best_overlap:
                best_overlap = overlap
                best_key = key
                best_is_top = self._anchor_is_top(bounds, vy, vy_end)

        return (best_key, best_is_top) if best_key is not None else None

    def toggle_all(self):
        """Toggle all active response entries between raw and preview mode."""
        if not self.is_available or not self._is_view_valid():
            return 0
        active = self._active_entries()
        if not active:
            return 0
        target_raw = not any(entry["raw_mode"] for _, entry, _ in active)
        toggled = 0
        for _, entry, reg in active:
            if entry["raw_mode"] == target_raw:
                continue
            entry["generation"] += 1
            entry["pending_fold"] = False
            entry["raw_mode"] = target_raw
            self._apply_fold_state(reg, target_raw)
            self._render_entry(entry, reg)
            toggled += 1
        return toggled

    def toggle_entry(self, key, is_top=True):
        """Toggle a single response entry between rendered preview and raw text."""
        if not self.is_available or not self._is_view_valid():
            return
        entry = self._entries.get(key)
        if entry is None:
            return
        reg = self._resolve_active_region(entry)
        if reg is None:
            return
        entry["generation"] += 1
        gen = entry["generation"]
        pre_screen_y = self._scroll_anchor.capture(reg, is_top)

        entry["raw_mode"] = not entry["raw_mode"]
        defer_fold = not entry["raw_mode"] and self._scroll_anchor.can_defer_fold(reg)
        if defer_fold:
            entry["pending_fold"] = True
            delay = self._PREVIEW_SETTLE_DELAY_MS
        else:
            entry["pending_fold"] = False
            self._apply_fold_state(reg, entry["raw_mode"])
            delay = 0 if entry["raw_mode"] else self._PREVIEW_SETTLE_DELAY_MS

        self._render_entry(entry, reg)
        sublime.set_timeout(
            lambda: self._finish_toggle(key, gen, is_top, pre_screen_y),
            delay,
        )

    def _finish_toggle(self, key, gen, is_top, pre_screen_y):
        """Complete deferred fold (if pending) and lock viewport anchor."""
        if not self._is_view_valid():
            return
        entry = self._entries.get(key)
        if entry is None or entry["generation"] != gen:
            return
        reg = self._entry_region(key)
        if reg is None:
            return

        if entry["pending_fold"]:
            entry["pending_fold"] = False
            self.raw_view.clear(key)
            self._apply_fold_state(reg, False)

        self._scroll_anchor.restore(reg, is_top, pre_screen_y)

    def _on_navigate(self, href):
        if href.startswith("toggle:"):
            parts = href.split(":", 2)
            key = parts[1]
            is_top = (parts[2] == "top") if len(parts) > 2 else True
            self.toggle_entry(key, is_top=is_top)
        elif self.on_navigate_link:
            self.on_navigate_link(href)

    def _is_view_valid(self):
        return bool(
            self.view
            and (not hasattr(self.view, "is_valid") or self.view.is_valid())
        )

    @staticmethod
    def _resolve_bool_getter(getter, default):
        if getter is not None:
            try:
                value = getter() if callable(getter) else getter
                return bool(value)
            except Exception:
                pass
        return default

    def _resolve_raw_mode(self, raw_mode=None):
        if raw_mode is not None:
            return bool(raw_mode)
        return self._resolve_bool_getter(self.default_raw_mode_getter, True)

    def _resolve_show_toolbar(self):
        return self._resolve_bool_getter(self.show_toolbar_getter, False)

    def _entry_region(self, key):
        regions = self.view.get_regions(key)
        return regions[0] if regions else None

    def _apply_fold_state(self, region, raw_mode):
        self.view.unfold(region)
        if not raw_mode:
            self.view.fold(region)
        if self.table_manager is not None:
            self.table_manager.sync_response_region(region, raw_mode)

    def _dispose_entry(self, entry, region=None):
        key = entry["key"]
        entry["pending_fold"] = False
        entry["generation"] += 1
        self.raw_view.clear(key)
        self.preview_view.clear(key)
        if region is None:
            region = self._entry_region(key)
        if region is not None:
            self.view.unfold(region)
        self.view.erase_regions(key)

    def _resolve_active_region(self, entry):
        """Return the active buffer region for entry, disposing stale entries."""
        reg = self._entry_region(entry["key"])
        if reg is None:
            self._dispose_entry(entry)
            self._entries.pop(entry["key"], None)
            self._sync_persistent_regions()
            return None
        return reg

    def _render_entry(self, entry, region):
        """Sync raw or preview phantoms for entry at its resolved region."""
        key = entry["key"]
        show_toolbar = self._resolve_show_toolbar()
        if entry["raw_mode"]:
            self.preview_view.clear(key)
            self.raw_view.show(key, region, show_toolbar=show_toolbar)
            return
        self.preview_view.show(
            key, region, entry["md_text"], show_toolbar=show_toolbar
        )
        if entry["pending_fold"]:
            self.raw_view.show_top_only(key, region, show_toolbar=show_toolbar)
        else:
            self.raw_view.clear(key)
