"""
Input pane: a small view under the Chat View for typing prompts.

The chat's own input line is the end of the chat buffer, so it scrolls out of
sight while you read back through the conversation. The input pane lives in its
own layout cell below the chat and stays put. Sending copies its text into the
chat's input line and runs the normal send, so history, context tags and the
agent see exactly what they would if it had been typed in the chat.
"""
import bisect

import sublime

INPUT_PANE_FLAG = "chatview_input_pane"
INPUT_PANE_NAME = "Chat Input"
INPUT_PANE_LINES = 8  # lines the pane is sized for: the Agent / Model / PlanMode panel plus ~5 of text
INPUT_PANE_MAX_FRACTION = 0.3 # never take more than this share of the chat's cell
HINT_KEY = "chatview_input_pane_hint"
SKIP_COLLAPSE = "chatview_input_pane_skip_collapse"  # set on a pane whose closer handles the layout
# The pane's first line is an empty line reserved for the Agent / Model / PlanMode bar (an inline
# phantom; a block phantom would be drawn *below* its line, under the first line of text). The typed
# text starts after it, and the caret is kept out of it.
INPUT_START = 1


def add_row_below(layout, group, fraction):
    """Split cell `group` of a window layout horizontally.

    The new bottom cell takes `fraction` of the cell's height and is appended
    as the last cell, so existing group numbers don't change. Cells beside the
    split one keep their extent.
    """
    rows = list(layout["rows"])
    cells = [list(c) for c in layout["cells"]]
    x1, y1, x2, y2 = cells[group]
    split = rows[y2] - (rows[y2] - rows[y1]) * fraction
    if split in rows:
        r = rows.index(split)
    else:
        r = bisect.bisect_left(rows, split)
        rows.insert(r, split)
        for cell in cells:
            if cell[1] >= r:
                cell[1] += 1
            if cell[3] >= r:
                cell[3] += 1
    bottom = cells[group][3]
    cells[group][3] = r
    cells.append([x1, r, x2, bottom])
    return {"cols": list(layout["cols"]), "rows": rows, "cells": cells}


def remove_row_below(layout, group):
    """Undo add_row_below: drop cell `group` and give its height back to the cell above it.

    Returns None when no cell directly above spans the same columns (the layout
    was rearranged since), so the caller can leave the layout alone.
    """
    rows = list(layout["rows"])
    cells = [list(c) for c in layout["cells"]]
    x1, y1, x2, y2 = cells[group]
    above = next((c for i, c in enumerate(cells)
                  if i != group and c[0] == x1 and c[2] == x2 and c[3] == y1), None)
    if above is None:
        return None
    above[3] = y2
    del cells[group]
    if not any(c[1] == y1 or c[3] == y1 for c in cells):
        del rows[y1]
        for cell in cells:
            if cell[1] > y1:
                cell[1] -= 1
            if cell[3] > y1:
                cell[3] -= 1
    return {"cols": list(layout["cols"]), "rows": rows, "cells": cells}


def resize_row_below(layout, group, height, target, above_height=None):
    """Move the top edge of cell `group` (the input pane) so its `height` px becomes about `target` px.

    Layout rows are fractions of the window, so the pane grows and shrinks with it. With the
    cell above's px height (`above_height`) the px per row unit and the tab bar height follow
    from the two cells, and the edge lands on `target` in one step; without it the pane's share
    is scaled by target / height (tab bars make that a few px off, the next check corrects it).
    The pane never takes more than INPUT_PANE_MAX_FRACTION of its cell and the cell above, and
    the edge stays between its neighbouring rows. Returns None when there is nothing to move."""
    rows = list(layout["rows"])
    cells = layout["cells"]
    x1, y1, x2, y2 = cells[group]
    above = next((c for i, c in enumerate(cells)
                  if i != group and c[0] == x1 and c[2] == x2 and c[3] == y1), None)
    if above is None or height <= 0 or y1 == 0 or y2 >= len(rows):
        return None
    if any(c is not cells[group] and c is not above and y1 in (c[1], c[3]) for c in cells):
        return None  # the edge is shared with a cell in another column: leave it
    top, mid, bottom = rows[above[1]], rows[y1], rows[y2]
    share = (bottom - mid) * target / height
    if above_height is not None and abs((mid - top) - (bottom - mid)) > 0.02:
        # cell px = rows * px_per_row - tab bar, for both cells: two equations, two unknowns
        px_per_row = (above_height - height) / ((mid - top) - (bottom - mid))
        tab_bar = (bottom - mid) * px_per_row - height
        if px_per_row > 0 and 0 <= tab_bar < height + target:
            share = (target + tab_bar) / px_per_row
    share = min(share, (bottom - top) * INPUT_PANE_MAX_FRACTION)
    lowest, highest = rows[y1 - 1], rows[y1 + 1]
    new_mid = min(max(bottom - share, lowest + 0.01), highest - 0.01)
    if abs(new_mid - mid) < 1e-4:
        return None
    rows[y1] = new_mid
    return {"cols": list(layout["cols"]), "rows": rows, "cells": [list(c) for c in cells]}


_KEEP_HEIGHT_MS = 100  # short enough that the pane doesn't visibly stretch before it snaps back
_KEEP_HEIGHT_IDLE_MS = 1000  # while another window is active (resizing a window activates it)
# window id -> [chat + pane height px, pane height px to keep, correction pending], while watched
_kept_heights = {}


def keep_height(window):
    """Keep the input pane's height in px while the window is resized.

    Sublime has no resize event, so this checks every _KEEP_HEIGHT_MS while the pane is open (less often
    while its window is not the active one). If the
    chat and pane together changed height, the window was resized: the pane gets its height
    back. If only the split moved, the user dragged it: the new height is kept from then on."""
    if window is None:
        return
    watched = window.id() in _kept_heights
    _kept_heights[window.id()] = None  # (re)opened: measure afresh
    if watched:
        return
    sublime.set_timeout(lambda: _check_height(window), _KEEP_HEIGHT_MS)


def _check_height(window):
    wid = window.id()
    pane = find_input_pane(window) if window.is_valid() else None
    if pane is None:
        _kept_heights.pop(wid, None)
        return
    active = sublime.active_window()
    delay = _KEEP_HEIGHT_MS if active is not None and active.id() == wid else _KEEP_HEIGHT_IDLE_MS
    sublime.set_timeout(lambda: _check_height(window), delay)
    group, _ = window.get_view_index(pane)
    layout = window.get_layout()
    if group < 0 or group >= len(layout["cells"]):
        return
    x1, y1, x2, _ = layout["cells"][group]
    above = next((i for i, c in enumerate(layout["cells"])
                  if i != group and c[0] == x1 and c[2] == x2 and c[3] == y1), None)
    chat = window.active_view_in_group(above) if above is not None else None
    if chat is None:
        return
    pane_h = pane.viewport_extent()[1]
    chat_h = chat.viewport_extent()[1]
    total = chat_h + pane_h
    if pane_h <= 0 or total <= pane_h:
        return  # minimized or not drawn yet
    kept = _kept_heights.get(wid)
    if kept is None:
        _kept_heights[wid] = [total, pane_h, False]
        return
    total_before, target, correcting = kept
    kept[0] = total
    if abs(total - total_before) <= 2 and not correcting:
        kept[1] = pane_h  # unchanged, or the user moved the split
        return
    new_layout = None
    if abs(pane_h - target) > pane.line_height() / 2:
        new_layout = resize_row_below(layout, group, pane_h, target, chat_h)
    kept[2] = new_layout is not None  # check again until it is within half a line
    if new_layout is not None:
        window.set_layout(new_layout)


def find_input_pane(window):
    """The window's input pane view, or None."""
    if window is None:
        return None
    for view in window.views():
        if view.settings().get(INPUT_PANE_FLAG, False):
            return view
    return None


def is_input_pane(view):
    return view is not None and view.settings().get(INPUT_PANE_FLAG, False)


def has_header(view):
    """Whether the pane starts with its reserved header line (see INPUT_START)."""
    return view.size() > 0 and view.substr(0) == "\n"


def text_region(view):
    """The typed text: everything after the reserved header line."""
    start = INPUT_START if has_header(view) else 0
    return sublime.Region(start, view.size())


def get_text(view):
    return view.substr(text_region(view))


def _pane_fraction(chat_view):
    """Share of the chat's cell height that fits INPUT_PANE_LINES lines of text."""
    _, height = chat_view.viewport_extent()
    if height <= 0:
        return 0.15
    wanted = chat_view.line_height() * (INPUT_PANE_LINES + 1)
    return min(INPUT_PANE_MAX_FRACTION, wanted / height)


def open_input_pane(window, chat_view, shortcut="Ctrl+Enter"):
    """Open the input pane under chat_view (or focus it if it is already open)."""
    pane = find_input_pane(window)
    if pane is not None:
        window.focus_view(pane)
        return pane
    group, _ = window.get_view_index(chat_view)
    if group < 0:
        return None
    window.set_layout(add_row_below(window.get_layout(), group, _pane_fraction(chat_view)))
    pane_group = window.num_groups() - 1
    window.focus_group(pane_group)
    pane = window.new_file()
    pane.set_name(INPUT_PANE_NAME)
    pane.set_scratch(True)
    pane.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
    settings = pane.settings()
    settings.set(INPUT_PANE_FLAG, True)
    for key, value in (
        ("word_wrap", True),
        ("line_numbers", False),
        ("gutter", False),
        ("draw_minimap", False),
        ("scroll_past_end", False),
        ("highlight_line", False),
        ("draw_indent_guides", False),
        # spell_check is left to the user's Preferences: the pane holds their own prose
        ("rulers", []),
    ):
        settings.set(key, value)
    window.set_view_index(pane, pane_group, 0)
    pane.run_command("term_chat_input_pane_header")
    window.focus_view(pane)
    update_hint(pane, shortcut)
    return pane


def close_input_pane(window):
    """Close the input pane; its layout row goes away once the view has closed (collapse_group)."""
    pane = find_input_pane(window)
    if pane is not None:
        pane.close()


def collapse_group(window, group):
    """Remove the input pane's now-empty layout cell, giving its height back to the chat."""
    if window is None or not window.is_valid() or group < 0 or group >= window.num_groups():
        return
    if window.views_in_group(group):
        return
    layout = remove_row_below(window.get_layout(), group)
    if layout is not None:
        window.set_layout(layout)


def update_hint(pane, shortcut="Ctrl+Enter"):
    """Show a faint "how to send" hint while the pane is empty."""
    pane.erase_phantoms(HINT_KEY)
    region = text_region(pane)
    if not region.empty():
        return
    content = (
        "<body style='margin:0'><span style='color:color(var(--foreground) alpha(0.4))'>"
        f"Message the agent · {shortcut} to send · ↑ ↓ history · @ for files"
        "</span></body>"
    )
    pane.add_phantom(HINT_KEY, sublime.Region(region.begin()), content, sublime.LAYOUT_INLINE)


def replace_text(view, edit, text):
    """Replace the typed text and put the caret at the end."""
    view.replace(edit, text_region(view), text)
    view.sel().clear()
    view.sel().add(sublime.Region(view.size()))
    view.show(view.size())


def clamp_selection(view):
    """Keep carets and selections out of the reserved header line."""
    if not has_header(view):
        return
    changed = False
    regions = []
    for region in view.sel():
        a, b = max(region.a, INPUT_START), max(region.b, INPUT_START)
        changed = changed or (a, b) != (region.a, region.b)
        regions.append(sublime.Region(a, b))
    if changed:
        view.sel().clear()
        view.sel().add_all(regions)


def blocks_header_delete(view, command_name):
    """Whether a delete command would remove the header line's newline (backspace at the text start)."""
    if not has_header(view) or command_name not in ("left_delete", "delete_word_backward"):
        return False
    return any(r.empty() and r.begin() <= INPUT_START for r in view.sel())
