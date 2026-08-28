"""
Diff view module for TermMate.

Provides Find-Results-style line-numbered diff generation, view lifecycle management,
and double-click line navigation to source files.
"""
import logging
import os
import re
import difflib

import sublime

LOG = logging.getLogger("TermMate")

DIFF_VIEW_PATH_KEY = "chatview_artifact_diff_path"
DIFF_SYNTAX = "Packages/TermMate/chatview/ChatDiff.sublime-syntax"

_DIFF_HEADER_RE = re.compile(r'^diff [ab]/(.+?) [ab]/.+$|^[+-]{3} [ab]/(.+)$')
_HUNK_HEADER_RE = re.compile(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)$')
_NUMBERED_LINE_RE = re.compile(r'^([ \t]*\d+)?\s+([ \t]*\d+)?\s+([+-]|\s)(.*)$')


def format_raw_unified_diff(diff_lines_or_text, filename: str) -> str:
    """
    Transforms raw unified diff lines into a padded, line-numbered buffer.

    Format:
        old_no new_no [+- ]content
    """
    if isinstance(diff_lines_or_text, str):
        diff_lines = diff_lines_or_text.splitlines(keepends=True)
    else:
        diff_lines = list(diff_lines_or_text)

    if not diff_lines:
        return ""

    # First pass: find the maximum line number to calculate padding width
    max_line = 1
    for line in diff_lines:
        stripped = line.strip()
        m = _HUNK_HEADER_RE.search(stripped)
        if m:
            old_start, new_start = int(m.group(1)), int(m.group(2))
            max_line = max(max_line, old_start + 100, new_start + 100)

    # Pad columns to at least 3 digits
    w = max(len(str(max_line)), 3)

    output = []

    # Check if diff a/.. b/.. header is already present
    first_non_empty = next((l.strip() for l in diff_lines if l.strip()), "")
    if not first_non_empty.startswith("diff ") and filename:
        output.append(f"diff a/{filename} b/{filename}\n")

    has_file_headers = any(l.startswith("--- ") or l.startswith("+++ ") for l in diff_lines)
    if not has_file_headers and filename:
        output.append(f"--- a/{filename}\n")
        output.append(f"+++ b/{filename}\n")

    old_no = 0
    new_no = 0
    in_hunk = False

    for line in diff_lines:
        line_str = line.rstrip('\r\n')

        # Preserve file header lines
        if line_str.startswith("diff ") or line_str.startswith("--- ") or line_str.startswith("+++ "):
            output.append(line_str + "\n")
            continue

        # Check for hunk header @@ -old,len +new,len @@
        hunk_m = _HUNK_HEADER_RE.search(line_str)
        if hunk_m:
            old_no = int(hunk_m.group(1))
            new_no = int(hunk_m.group(2))
            in_hunk = True
            tail = hunk_m.group(3) or ""
            spacer = " " * w
            output.append(f"{spacer} {spacer}  {hunk_m.group(0).strip()}{tail}\n")
            continue

        if not in_hunk:
            if line_str.strip():
                output.append(line_str + "\n")
            continue

        if line_str.startswith('\\ No newline'):
            spacer = " " * w
            output.append(f"{spacer} {spacer}  {line_str}\n")
            continue

        if line_str.startswith('-'):
            col_old = f"{old_no:>{w}}"
            col_new = " " * w
            content = line_str[1:]
            output.append(f"{col_old} {col_new} -{content}\n")
            old_no += 1
        elif line_str.startswith('+'):
            col_old = " " * w
            col_new = f"{new_no:>{w}}"
            content = line_str[1:]
            output.append(f"{col_old} {col_new} +{content}\n")
            new_no += 1
        else:
            # Context line (may start with ' ' or be blank)
            content = line_str[1:] if line_str.startswith(' ') else line_str
            col_old = f"{old_no:>{w}}"
            col_new = f"{new_no:>{w}}"
            output.append(f"{col_old} {col_new}  {content}\n")
            old_no += 1
            new_no += 1

    return "".join(output)


def format_numbered_diff(old_text: str, new_text: str, name: str, context: int = 5) -> str:
    """Generate a line-numbered git-style unified diff between old and new text."""
    a = old_text.splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        a, b,
        fromfile="a/" + name,
        tofile="b/" + name,
        lineterm='',
        n=context
    ))

    if not diff_lines:
        return ""

    return format_raw_unified_diff(diff_lines, name)


def strip_numbered_diff(numbered_diff_text: str) -> str:
    """
    Restores a line-numbered diff back to standard Unified Diff format.
    Useful for copying clean diffs or passing to patch tools.
    """
    clean_lines = []
    for line in numbered_diff_text.splitlines(keepends=True):
        line_str = line.rstrip('\r\n')
        # Hunk banner: restore @@ line
        if "@@ " in line_str and not line_str.startswith(("diff ", "--- ", "+++ ")):
            m = re.search(r'(@@\s*-\d+(?:,\d+)?\s*\+\d+(?:,\d+)?\s*@@.*)', line_str)
            if m:
                clean_lines.append(m.group(1).strip() + "\n")
            continue
        if "\\ No newline" in line_str:
            m = re.search(r'(\\ No newline.*)', line_str)
            if m:
                clean_lines.append(m.group(1).strip() + "\n")
            continue
        parsed = parse_numbered_line(line_str)
        if parsed:
            _, _, sign, content = parsed
            clean_lines.append(f"{sign}{content}\n")
            continue
        clean_lines.append(line_str + "\n")

    return "".join(clean_lines)



def configure_diff_view(v: sublime.View, name: str, abs_path: str = None) -> sublime.View:
    """Applies buffer settings and syntax for line-numbered diff view."""
    v.set_name(name)
    v.set_scratch(True)
    v.settings().set("line_numbers", False)
    v.settings().set("gutter", False)
    v.settings().set("fold_buttons", False)
    v.settings().set("draw_indent_guides", False)
    v.settings().set("margin", 16)
    v.settings().set("word_wrap", False)
    v.settings().set("highlight_line", True)
    v.assign_syntax(DIFF_SYNTAX)
    if abs_path:
        v.settings().set(DIFF_VIEW_PATH_KEY, abs_path)
    return v


def show_diff(window: sublime.Window, old_text: str, new_text: str, name: str, abs_path: str = None) -> sublime.View:
    """
    Generate and show a line-numbered git-style unified diff in a new scratch view.
    """
    difftxt = format_numbered_diff(old_text, new_text, name)
    if not difftxt:
        sublime.status_message("No changes")
        return None

    v = window.new_file()
    configure_diff_view(v, name, abs_path)
    v.run_command('append', {'characters': difftxt, 'disable_tab_translation': True})
    v.set_read_only(True)
    return v


def show_diff_text(window: sublime.Window, diff_text: str, name: str, abs_path: str = None) -> sublime.View:
    """
    Show pre-built diff text in a new read-only scratch view.
    Automatically formats raw unified diff if not yet numbered.
    """
    if not diff_text:
        sublime.status_message("No changes")
        return None

    # If it looks like a raw diff (starts with @@ or --- a/), format it
    lines = [l for l in diff_text.splitlines() if l.strip()]
    is_raw = any(l.startswith(("@@ -", "--- a/", "diff a/")) for l in lines) and not any(re.match(r'^\s*\d+\s+\d+\s+', l) for l in lines)
    if is_raw:
        formatted = format_raw_unified_diff(diff_text, name)
    else:
        formatted = diff_text

    v = window.new_file()
    configure_diff_view(v, name, abs_path)
    v.run_command('append', {'characters': formatted, 'disable_tab_translation': True})
    v.set_read_only(True)
    return v


def parse_numbered_line(line_str: str):
    """
    Parses a numbered diff line and returns (old_lineno, new_lineno, sign, content).
    Returns None if line does not match the numbered line structure.
    """
    if line_str.startswith(("diff ", "--- ", "+++ ", "@@ ")) or "@@ " in line_str:
        return None

    # Deletion: old_no followed by spaces and '-'
    m_del = re.match(r'^[ \t]*(\d+)\s+-(.*)$', line_str)
    if m_del:
        return int(m_del.group(1)), None, "-", m_del.group(2)

    # Addition: spaces followed by new_no and '+'
    m_add = re.match(r'^[ \t]*(\d+)\s+\+(.*)$', line_str)
    if m_add:
        return None, int(m_add.group(1)), "+", m_add.group(2)

    # Context: two numbers followed by 2 spaces and content
    m_ctx = re.match(r'^[ \t]*(\d+)\s+(\d+)\s{2}(.*)$', line_str)
    if m_ctx:
        return int(m_ctx.group(1)), int(m_ctx.group(2)), " ", m_ctx.group(3)

    return None



def handle_diff_view_click(view: sublime.View, abs_path: str, click_point: int) -> bool:
    """
    Handle double-click in a line-numbered diff view.

    Jumps to:
    1. Header line -> Opens source file.
    2. Hunk banner -> Jumps to start line of the hunk.
    3. Numbered code line -> Jumps directly to the exact source line number.

    Returns True if handled, False otherwise.
    """
    if click_point is None:
        return False

    line_str = view.substr(view.line(click_point)).strip()
    if not line_str:
        return False

    # 1. Diff header check
    m = _DIFF_HEADER_RE.match(line_str)
    if m:
        rel = m.group(1) or m.group(2)
        target = _resolve_file_path(view.window(), rel, abs_path)
        if target:
            view.window().open_file(target)
            return True
        sublime.status_message("File not found: " + rel)
        return True

    # 2. Hunk banner check
    hunk_m = _HUNK_HEADER_RE.search(line_str)
    if hunk_m:
        new_start = hunk_m.group(2) if hunk_m.lastindex and hunk_m.lastindex >= 2 and hunk_m.group(2) else hunk_m.group(1)
        if new_start:
            line_no = int(new_start)
            _jump_to_source_line(view.window(), abs_path, line_no)
            return True

    # 3. Numbered code line check
    parsed = parse_numbered_line(line_str)
    if parsed:
        old_no, new_no, sign, _ = parsed
        # Prefer new line number for additions/context; fallback to old line number for deletions
        target_line = new_no if new_no is not None else old_no
        if target_line is not None:
            _jump_to_source_line(view.window(), abs_path, target_line)
            return True

    return False




def _resolve_file_path(window: sublime.Window, rel_path: str, fallback_abs: str = None) -> str:
    """Resolves relative diff path against window folders or fallback abs path."""
    if fallback_abs and os.path.isfile(fallback_abs):
        return fallback_abs
    if window:
        for folder in (window.folders() or []):
            p = os.path.normpath(os.path.join(folder, rel_path))
            if os.path.isfile(p):
                return p
    return None


def _jump_to_source_line(window: sublime.Window, abs_path: str, line_no: int):
    """Opens the source file at the specified line number."""
    if abs_path and os.path.isfile(abs_path):
        window.open_file(f"{abs_path}:{line_no}", sublime.ENCODED_POSITION)
    else:
        sublime.status_message(f"Source file not found: {abs_path}")
