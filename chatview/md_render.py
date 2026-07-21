import re
import unicodedata

import sublime


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
                w = self.str_width(cell)
                if w > col_widths[j]:
                    col_widths[j] = w

        col_widths = [max(w, 3) for w in col_widths]

        if self._get_table_style() == "bordered":
            return self._render_bordered_table(rows, separator_idx, col_widths)

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
                    padding = width - self.str_width(cell)
                    new_row += f" {cell}{' ' * padding} |"
            formatted_lines.append(new_row)

        return formatted_lines

    def _get_table_style(self):
        """Return the configured table style: "bordered" or "markdown"."""
        try:
            settings = sublime.load_settings("TermMate.sublime-settings")
            return settings.get("table_style", "bordered")
        except Exception:
            return "bordered"

    def _render_bordered_table(self, rows, separator_idx, col_widths):
        """
        Render the table with plain horizontal border lines between rows
        (no corner or junction characters), and no vertical lines at all.
        Columns are aligned by padding.
        """
        inner_width = sum(w + 2 for w in col_widths)
        border = "─" * inner_width

        formatted_lines = [border]
        first_row = True
        for i, row in enumerate(rows):
            if i == separator_idx:
                continue
            if not first_row:
                formatted_lines.append(border)
            first_row = False
            new_row = ""
            for j, cell in enumerate(row):
                padding = col_widths[j] - self.str_width(cell)
                new_row += f" {cell}{' ' * padding} "
            formatted_lines.append(new_row.rstrip())
        formatted_lines.append(border)

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
