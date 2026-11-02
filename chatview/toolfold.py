"""
Tool-call folding, so the chat reads as the agent's text. Each call that takes several lines (a
multi-line command, an edit with its diff) is folded to its first line as soon as it is finished;
each run of calls in a reply is then folded under one "⏺ N tool calls" header once the run is over.
A click on the header expands or collapses the run, a click on the ⏺ of a call expands or collapses
the call (on a one-line call: the run).
"""
import re

import sublime

from .chatrender import extract_diff_fold_ranges

TOOL_PREFIX = "⏺ "
# Runs with fewer calls get no header: their calls are folded one by one already
MIN_CALLS = 2
HEADER_PATTERN = r"^⏺ \d+ tool calls?$"
_HEADER_RE = re.compile(HEADER_PATTERN)
HEADER_KEY = "chatview_tool_call_headers"
HEADER_SCOPE = "markup.underline.link"
HEADER_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.DRAW_SOLID_UNDERLINE
# Where the next scan starts: an empty region, so it moves with text inserted before it
SCAN_KEY = "chatview_tool_fold_from"
# Calls starting before this point are folded already
CALL_SCAN_KEY = "chatview_tool_call_fold_from"
# The first character of the agent's text after a tool call (marked when it is drawn, see
# MarkdownFormatter.take_text_starts): an indented list there is text, not the call's body
TEXT_KEY = "chatview_text_after_tool_call"


def header_text(count):
    return f"⏺ {count} tool call{'s' if count != 1 else ''}"


def is_header(line):
    return bool(_HEADER_RE.match(line))


def is_tool_line(line):
    return line.startswith(TOOL_PREFIX) and not is_header(line)


def _continues_run(line):
    """Blank lines and indented lines (a multi-line command, a diff) belong to the call above."""
    return not line.strip() or line[0] in " \t"


def mark_text_starts(view, points):
    """Remember where the agent's text starts after a tool call (one character each, kept in the session)."""
    regions = [r for r in view.get_regions(TEXT_KEY) if not r.empty()]
    regions += [sublime.Region(p, p + 1) for p in points]
    view.add_regions(TEXT_KEY, regions, "", "", sublime.HIDDEN | sublime.PERSISTENT)


def _text_starts(view):
    return {r.begin() for r in view.get_regions(TEXT_KEY) if not r.empty()}


def _undecided(line):
    """A last line still streaming in that could turn out to be part of a run."""
    return _continues_run(line) or TOOL_PREFIX.startswith(line) or line.startswith(TOOL_PREFIX)


def find_runs(text, final=False, headed=False, text_starts=()):
    """Return the runs of tool calls in text as (start, end, count, headed, complete, calls) tuples.

    `end` is the end of the run's last non-blank line, `headed` whether a header line is right above
    it (pass `headed` when text starts inside a run that has one). A run is complete once a line
    that is not part of it follows; with `final` (the turn is over) the end of the text completes it
    too. Without `final`, an unfinished last line ends nothing. `calls` lists the run's calls as
    (start, first_line_end, end, complete); a call is complete once the next call starts. A line starting
    at one of `text_starts` (offsets in text) is the agent's text, never part of a run.
    """
    runs = []
    run = None  # [start, end, count, headed, calls]
    offset = 0
    lines = text.splitlines(keepends=True)
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if not final and i == len(lines) - 1 and line == raw and _undecided(line):
            break
        if is_tool_line(line):
            if run is None:
                run = [offset, offset, 0, headed, []]
            elif run[4]:
                run[4][-1][3] = True
            run[1] = offset + len(line)
            run[2] += 1
            run[4].append([offset, offset + len(line), offset + len(line), False])
            headed = False
        elif run is not None and _continues_run(line) and offset not in text_starts:
            if line.strip():
                run[1] = run[4][-1][2] = offset + len(line)
        else:
            if run is not None:
                runs.append(_run_tuple(run, True))
                run = None
            if is_header(line):
                headed = True
            elif line.strip():
                headed = False
        offset += len(raw)
    if run is not None:
        runs.append(_run_tuple(run, final))
    return runs


def _run_tuple(run, complete):
    start, end, count, headed, calls = run
    if complete:
        calls[-1][3] = True
    return start, end, count, headed, complete, [tuple(call) for call in calls]


def _diff_fold_settings():
    settings = sublime.load_settings("TermMate.sublime-settings")
    return settings.get("diff_fold_limit", 15), settings.get("diff_preview_lines", 5)


def fold_runs(view, edit, end, final=False):
    """Fold each finished multi-line call and each finished run of tool calls since the last scan.

    Blank lines between the calls of a run are erased, so the folded calls sit on consecutive lines.
    A finished run of 2+ calls gets a header line above it and is folded under it. `end` is where
    the agent's output ends (the chat's input line follows). Called after every append, and with
    `final` when the turn is over. Returns how many characters were inserted minus erased.
    """
    if end <= 0:
        return 0
    regions = view.get_regions(SCAN_KEY)
    start = view.line(min(regions[0].begin(), end)).begin() if regions else 0
    call_regions = view.get_regions(CALL_SCAN_KEY)
    call_from = min(call_regions[0].begin(), end) if call_regions else start
    text = view.substr(sublime.Region(start, end))
    # The scan may start inside a run that already has its header (its last line is rescanned)
    first = view.substr(view.line(start))
    in_headed_run = start > 0 and is_tool_line(first) and _header_above(view, view.line(start)) is not None
    delta = 0  # characters inserted (headers) minus erased (blank lines between calls) so far
    next_from = next_call_from = None
    headers = []
    text_starts = {p - start for p in _text_starts(view) if start <= p < end}
    for run_start, run_end, count, headed, complete, calls in find_runs(text, final, headed=in_headed_run,
                                                                         text_starts=text_starts):
        group_start = None
        if complete and not headed and count >= MIN_CALLS:
            pos = start + delta + run_start
            header = header_text(count)
            view.insert(edit, pos, header + "\n")
            headers.append(sublime.Region(pos, pos + len(header)))
            delta += len(header) + 1
            group_start = pos + len(header)
        run_delta = delta
        for i, (call_start, head_end, call_end, call_complete) in enumerate(calls):
            if not call_complete:
                next_call_from = start + delta + call_start
                break
            if start + call_start < call_from:
                continue
            if call_end > head_end:
                view.fold(sublime.Region(start + delta + head_end, start + delta + call_end))
            if i + 1 < len(calls):
                # The calls fold to one line each: drop the blank lines between them
                gap_start, gap_end = call_end + 1, calls[i + 1][0]
                if gap_end > gap_start:
                    view.erase(edit, sublime.Region(start + delta + gap_start, start + delta + gap_end))
                    delta -= gap_end - gap_start
        if group_start is not None:
            view.fold(sublime.Region(group_start, start + delta + run_end))
        if not complete:
            next_from = start + run_delta + run_start
            break
    end += delta
    if next_from is None:
        # Rescan the last line: it may still be streaming in
        next_from = view.line(max(start, end - 1)).begin()
    if next_call_from is None:
        next_call_from = end
    flags = sublime.HIDDEN | sublime.PERSISTENT
    view.add_regions(SCAN_KEY, [sublime.Region(next_from)], "", "", flags)
    view.add_regions(CALL_SCAN_KEY, [sublime.Region(next_call_from)], "", "", flags)
    if headers:
        # Headers cut off by a rewind are left as empty regions
        kept = [r for r in view.get_regions(HEADER_KEY) if not r.empty()]
        view.add_regions(HEADER_KEY, kept + headers, HEADER_SCOPE, "", HEADER_FLAGS)
    return delta


def _run_end(view, header, starts=None):
    """End of the last non-blank line of the run below a header line."""
    starts = _text_starts(view) if starts is None else starts
    run_end = header.end()
    line = header
    while line.end() < view.size():
        line = view.line(line.end() + 1)
        text = view.substr(line)
        if is_tool_line(text) or (run_end > header.end() and _continues_run(text) and line.begin() not in starts):
            if text.strip():
                run_end = line.end()
        else:
            break
    return run_end


def _call_end(view, line, starts=None):
    """End of the last non-blank line of the call on a tool line (the line's own end for a one-line call)."""
    starts = _text_starts(view) if starts is None else starts
    call_end = line.end()
    while line.end() < view.size():
        line = view.line(line.end() + 1)
        text = view.substr(line)
        if is_tool_line(text) or not _continues_run(text) or line.begin() in starts:
            break
        if text.strip():
            call_end = line.end()
    return call_end


def _header_above(view, line):
    """The header line of the run a tool line belongs to, or None."""
    starts = _text_starts(view)
    while line.begin() > 0:
        line = view.line(line.begin() - 1)
        text = view.substr(line)
        if is_header(text):
            return line
        if not (is_tool_line(text) or _continues_run(text)) or line.begin() in starts:
            return None
    return None


def _fold_calls(view, start, end):
    """Fold every multi-line call whose tool line starts between start and end."""
    starts = _text_starts(view)
    pos = start
    while pos < end:
        line = view.line(pos)
        call_end = _call_end(view, line, starts) if is_tool_line(view.substr(line)) else line.end()
        if call_end > line.end():
            view.fold(sublime.Region(line.end(), call_end))
        pos = call_end + 1


def _refold_diffs(view, start, end):
    """Unfolding a call unfolds its long diff too: fold it again."""
    limit, preview = _diff_fold_settings()
    for diff_start, diff_end in extract_diff_fold_ranges(view.substr(sublime.Region(start, end)), limit,
                                                         preview_lines=preview):
        if diff_end > diff_start:
            view.fold(sublime.Region(start + diff_start, start + diff_end))


def toggle_at(view, point):
    """Expand or collapse at a click: the run under a header, or the call (or one-line call's run) at its ⏺.

    Returns True if handled.
    """
    if point is None:
        return False
    line = view.line(point)
    text = view.substr(line)
    if is_header(text):
        header = line
    elif is_tool_line(text) and point - line.begin() < len(TOOL_PREFIX):
        call_end = _call_end(view, line)
        if call_end > line.end():
            if view.unfold(sublime.Region(line.end(), line.end() + 1)):
                _refold_diffs(view, line.end(), call_end)
            else:
                view.fold(sublime.Region(line.end(), call_end))
            return True
        # A one-line call has nothing of its own to fold: fold its run
        header = _header_above(view, line)
        if header is None:
            return False
        view.fold(sublime.Region(header.end(), _run_end(view, header)))
        return True
    else:
        return False
    fold_start = header.end()
    run_end = _run_end(view, header)
    if run_end <= fold_start:
        return False
    if view.unfold(sublime.Region(fold_start, fold_start + 1)):
        # Unfolding the run unfolds its calls too: show them folded one by one again
        _fold_calls(view, fold_start + 1, run_end)
        return True
    view.fold(sublime.Region(fold_start, run_end))
    return True


def restore(view, fold_calls=True):
    """After a restart: underline the headers again and fold their runs and calls (folds and regions are
    not kept). Without `fold_calls` (tool-call folding is off) only the existing headers are restored."""
    starts = _text_starts(view)
    if fold_calls:
        for line in view.find_all(r"^⏺ "):
            line = view.line(line.begin())
            if is_tool_line(view.substr(line)):
                call_end = _call_end(view, line, starts)
                if call_end > line.end():
                    view.fold(sublime.Region(line.end(), call_end))
    headers = view.find_all(HEADER_PATTERN)
    if not headers:
        return
    view.add_regions(HEADER_KEY, headers, HEADER_SCOPE, "", HEADER_FLAGS)
    for header in headers:
        run_end = _run_end(view, header, starts)
        if run_end > header.end():
            view.fold(sublime.Region(header.end(), run_end))
