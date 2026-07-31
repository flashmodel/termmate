import logging
import difflib
import re
import unicodedata
import sublime

LOG = logging.getLogger("TermMate")

def get_log_level(level_name):
    """Maps log level names to logging constants."""
    return getattr(logging, level_name.upper(), logging.ERROR)


def update_log_level(settings):
    """
    Reads the log_level from settings and reconfigures the logger.
    """
    level_name = settings.get("log_level", "ERROR")
    level = get_log_level(level_name)
    LOG.setLevel(level)
    LOG.propagate = False
    if not LOG.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def show_diff(window, old_text, new_text, name):
    """
    Generate and show a git-style unified diff between old and new text.

    Args:
        window: Sublime Text window to create the view in
        old_text: Original text content
        new_text: Modified text content
        name: Name for the diff view tab
    """
    a = old_text.splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)

    # Generate unified diff with context
    diff_lines = list(difflib.unified_diff(
        a, b,
        fromfile="a/" + name,
        tofile="b/" + name,
        lineterm='',
        n=5  # lines of context
    ))

    if not diff_lines:
        sublime.status_message("No changes")
        return

    # Build git-style diff output
    output_parts = []

    # Add git diff header
    output_parts.append(f"diff a/{name} b/{name}\n")
    output_parts.append(f"--- a/{name}\n")
    output_parts.append(f"+++ b/{name}\n")

    # Add the actual diff content (skip the default --- +++ lines from unified_diff)
    for line in diff_lines[2:]:
        if not line.endswith('\n'):
            line += '\n'
        output_parts.append(line)

    difftxt = "".join(output_parts)

    # Create and configure the diff view
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    v.assign_syntax('Packages/Diff/Diff.sublime-syntax')
    v.run_command('append', {'characters': difftxt, 'disable_tab_translation': True})
    v.set_read_only(True)


class MarkdownFormatter:
    """
    Helper class to format markdown text, specifically aligning tables
    with CJK character support. Supports stateful streaming.
    """

    def __init__(self):
        self.table_buffer = []
        self.in_code_block = False
        self.remaining_text = ""

    def char_width(self, char):
        if unicodedata.east_asian_width(char) in ('W', 'F', 'A'):
            return 2
        return 1

    def str_width(self, text):
        return sum(self.char_width(c) for c in text)

    # Cap on a single table column's display width. Without this, a cell
    # containing one long unbroken run of text (a full path, a stack trace
    # fragment, ...) makes the separator row's "-" * width fill balloon to
    # match -- rendered as one very long horizontal-rule-looking line.
    MAX_COL_WIDTH = 60

    def _truncate_to_width(self, text, max_width):
        """Truncate text to at most max_width display columns, adding an
        ellipsis if truncated. Assumes max_width >= 1."""
        if self.str_width(text) <= max_width:
            return text
        budget = max_width - 1  # leave room for the ellipsis char (width 1)
        out = []
        used = 0
        for ch in text:
            w = self.char_width(ch)
            if used + w > budget:
                break
            out.append(ch)
            used += w
        return "".join(out) + "…"

    def format_table(self, lines):
        if not lines:
            return []

        rows = []
        for line in lines:
            cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
            rows.append(cells)

        if not rows:
            return lines

        max_cols = max(len(row) for row in rows)
        for row in rows:
            while len(row) < max_cols:
                row.append("")

        separator_idx = -1
        alignments = [] # None, 'left', 'center', 'right'

        for i, row in enumerate(rows):
            is_sep = True
            row_aligns = []
            for cell in row:
                if not re.match(r'^:?-+:?$', cell):
                    is_sep = False
                    break
                # Determine alignment
                if cell.startswith(':') and cell.endswith(':'):
                    row_aligns.append('center')
                elif cell.endswith(':'):
                    row_aligns.append('right')
                elif cell.startswith(':'):
                    row_aligns.append('left')
                else:
                    row_aligns.append(None)

            if is_sep and i > 0: # Usually usually row 1
                separator_idx = i
                alignments = row_aligns
                break

        if separator_idx == -1:
            return lines

        # Pad alignments if needed
        while len(alignments) < max_cols:
            alignments.append(None)

        col_widths = [0] * max_cols
        for i, row in enumerate(rows):
            if i == separator_idx:
                continue
            for j, cell in enumerate(row):
                w = min(self.str_width(cell), self.MAX_COL_WIDTH)
                if w > col_widths[j]:
                    col_widths[j] = w

        col_widths = [max(w, 3) for w in col_widths]

        formatted_lines = []
        for i, row in enumerate(rows):
            new_row = "|"
            for j, cell in enumerate(row):
                width = col_widths[j]
                if i == separator_idx:
                    align = alignments[j]
                    if align == 'center':
                        fill = ":" + "-" * max(1, width - 2) + ":"
                    elif align == 'right':
                        fill = "-" * max(1, width - 1) + ":"
                    elif align == 'left':
                        fill = ":" + "-" * max(1, width - 1)
                    else:
                        fill = "-" * max(3, width)
                    new_row += f" {fill} |"
                else:
                    cell = self._truncate_to_width(cell, width)
                    padding = width - self.str_width(cell)
                    new_row += f" {cell}{' ' * padding} |"
            formatted_lines.append(new_row)

        return formatted_lines

    def format(self, text, flush=False):
        """
        Process the incoming text chunk.
        If flush is True, it returns all buffered content formatted.
        """
        # Combine with leftover from previous chunk
        combined_text = (self.remaining_text + text).expandtabs(4)

        if not flush and combined_text and not combined_text.endswith('\n'):
            last_newline = combined_text.rfind('\n')
            if last_newline != -1:
                self.remaining_text = combined_text[last_newline+1:]
                process_text = combined_text[:last_newline+1]
            else:
                self.remaining_text = combined_text
                return ""
        else:
            process_text = combined_text
            self.remaining_text = ""

        lines = process_text.split('\n')
        if process_text.endswith('\n'):
            lines = lines[:-1]

        output_lines = []

        def flush_buffer():
            if self.table_buffer:
                output_lines.extend(self.format_table(self.table_buffer))
                self.table_buffer.clear()

        for line in lines:
            stripped = line.strip()

            if stripped.startswith('```'):
                flush_buffer()
                self.in_code_block = not self.in_code_block
                output_lines.append(line)
                continue

            if self.in_code_block:
                output_lines.append(line)
                continue

            # Detect table rows - must start with | and have at least one more |
            if stripped.startswith('|') and '|' in stripped[1:]:
                self.table_buffer.append(line)
            else:
                flush_buffer()
                output_lines.append(line)

        if flush:
            if self.remaining_text:
                if self.remaining_text.strip().startswith('|'):
                    self.table_buffer.append(self.remaining_text)
                else:
                    flush_buffer()
                    output_lines.append(self.remaining_text)
                self.remaining_text = ""
            flush_buffer()

        if output_lines:
            res = '\n'.join(output_lines)
            if not flush:
                res += '\n'
            return res
        return ""
