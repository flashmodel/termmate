import html
import re
import unicodedata

import sublime


class MarkdownFormatter:
    """
    Helper class to format markdown text, specifically aligning tables
    with CJK character support. Supports stateful streaming.
    """

    def __init__(self, max_width_getter=None):
        self.table_buffer = []
        self.in_code_block = False
        self.remaining_text = ""
        self.max_width_getter = max_width_getter
        self._html_tables = []

    def char_width(self, char):
        if unicodedata.combining(char):
            return 0
        if unicodedata.east_asian_width(char) in ('W', 'F'):
            return 2
        return 1

    def str_width(self, text):
        return sum(self.char_width(c) for c in text)

    def take_html_tables(self):
        """Return and clear HTML table metadata from the last format call."""
        tables = self._html_tables
        self._html_tables = []
        return tables

    def _wrap_cell(self, text, width):
        """Split cell text into lines no wider than width display columns."""
        text = text.strip()
        if not text:
            return [""]

        lines = []
        while text:
            display_width = 0
            end = 0
            for end, char in enumerate(text, 1):
                char_width = self.char_width(char)
                if display_width + char_width > width:
                    end -= 1
                    break
                display_width += char_width
            else:
                lines.append(text)
                break

            if end == 0:
                end = 1

            candidate = text[:end]
            whitespace_breaks = [
                i for i, char in enumerate(candidate) if char.isspace()
            ]
            if whitespace_breaks and whitespace_breaks[-1] > 0:
                end = whitespace_breaks[-1]
                candidate = text[:end]

            lines.append(candidate.rstrip())
            text = text[end:].lstrip()

        return lines

    def _fit_column_widths(self, natural_widths, max_table_width, style):
        """Shrink wide columns so the rendered table fits max_table_width."""
        column_count = len(natural_widths)
        fixed_width = (2 * column_count if style in ("bordered", "html")
                       else 1 + 3 * column_count)
        available = max_table_width - fixed_width
        minimum_width = 3

        if available < minimum_width * column_count:
            return [minimum_width] * column_count
        if sum(natural_widths) <= available:
            return natural_widths

        low, high = minimum_width, max(natural_widths)
        while low < high:
            cap = (low + high + 1) // 2
            required = sum(max(minimum_width, min(width, cap))
                           for width in natural_widths)
            if required <= available:
                low = cap
            else:
                high = cap - 1

        widths = [max(minimum_width, min(width, low))
                  for width in natural_widths]
        remaining = available - sum(widths)
        for i, natural_width in enumerate(natural_widths):
            if remaining == 0:
                break
            extra = min(natural_width - widths[i], remaining)
            widths[i] += extra
            remaining -= extra
        return widths

    def _split_table_row(self, line):
        """Split pipes outside code spans, preserving escaped pipes."""
        text = line.strip()
        cells = []
        current = []
        code_fence = 0
        i = 0
        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text):
                current.append(char)
                current.append(text[i + 1])
                i += 2
                continue
            if char == '`':
                run_end = i + 1
                while run_end < len(text) and text[run_end] == '`':
                    run_end += 1
                run_length = run_end - i
                if code_fence == 0:
                    code_fence = run_length
                elif code_fence == run_length:
                    code_fence = 0
                current.extend(text[i:run_end])
                i = run_end
                continue
            if char == '|' and code_fence == 0:
                cells.append(''.join(current).strip())
                current = []
            else:
                current.append(char)
            i += 1
        cells.append(''.join(current).strip())

        if text.startswith('|') and cells and cells[0] == "":
            cells.pop(0)
        if text.endswith('|') and cells and cells[-1] == "":
            cells.pop()
        return cells

    def _parse_table(self, lines):
        if not lines:
            return None
        rows = [self._split_table_row(line) for line in lines]
        if not rows:
            return None

        max_cols = max(len(row) for row in rows)
        for row in rows:
            while len(row) < max_cols:
                row.append("")

        separator_idx = -1
        alignments = []
        for i, row in enumerate(rows):
            row_aligns = []
            for cell in row:
                if not re.match(r'^:?-+:?$', cell):
                    break
                if cell.startswith(':') and cell.endswith(':'):
                    row_aligns.append('center')
                elif cell.endswith(':'):
                    row_aligns.append('right')
                elif cell.startswith(':'):
                    row_aligns.append('left')
                else:
                    row_aligns.append(None)
            else:
                if i > 0:
                    separator_idx = i
                    alignments = row_aligns
                    break

        if separator_idx == -1:
            return None
        while len(alignments) < max_cols:
            alignments.append(None)
        return rows, separator_idx, alignments

    def format_table(self, lines):
        parsed = self._parse_table(lines)
        if parsed is None:
            return lines
        rows, separator_idx, alignments = parsed
        max_cols = len(rows[0])

        col_widths = [0] * max_cols
        for i, row in enumerate(rows):
            if i == separator_idx:
                continue
            for j, cell in enumerate(row):
                w = self.str_width(cell)
                if w > col_widths[j]:
                    col_widths[j] = w

        natural_widths = [max(w, 3) for w in col_widths]
        style = self._get_table_style()
        col_widths = self._fit_column_widths(
            natural_widths, self._get_table_max_width(), style)

        if style == "bordered":
            return self._render_bordered_table(rows, separator_idx, col_widths)

        formatted_lines = []
        for i, row in enumerate(rows):
            if i == separator_idx:
                new_row = "|"
                for j, width in enumerate(col_widths):
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
                formatted_lines.append(new_row)
                continue

            wrapped_cells = [self._wrap_cell(cell, col_widths[j])
                             for j, cell in enumerate(row)]
            row_height = max(len(cell_lines) for cell_lines in wrapped_cells)
            for line_idx in range(row_height):
                new_row = "|"
                for j, cell_lines in enumerate(wrapped_cells):
                    cell = (cell_lines[line_idx]
                            if line_idx < len(cell_lines) else "")
                    width = col_widths[j]
                    padding = width - self.str_width(cell)
                    new_row += f" {cell}{' ' * padding} |"
                formatted_lines.append(new_row)

        return formatted_lines

    def _get_table_style(self):
        """Return the configured table style."""
        try:
            settings = sublime.load_settings("TermMate.sublime-settings")
            return settings.get("table_style", "bordered")
        except Exception:
            return "bordered"

    def _get_table_max_width(self):
        """Return the maximum rendered table width in display columns."""
        if self.max_width_getter is not None:
            try:
                value = (self.max_width_getter()
                         if callable(self.max_width_getter)
                         else self.max_width_getter)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
            except Exception:
                pass

        try:
            settings = sublime.load_settings("TermMate.sublime-settings")
            value = settings.get("table_max_width", 100)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        except Exception:
            pass
        return 100

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
            wrapped_cells = [self._wrap_cell(cell, col_widths[j])
                             for j, cell in enumerate(row)]
            row_height = max(len(cell_lines) for cell_lines in wrapped_cells)
            for line_idx in range(row_height):
                new_row = ""
                for j, cell_lines in enumerate(wrapped_cells):
                    cell = (cell_lines[line_idx]
                            if line_idx < len(cell_lines) else "")
                    padding = col_widths[j] - self.str_width(cell)
                    new_row += f" {cell}{' ' * padding} "
                formatted_lines.append(new_row.rstrip())
        formatted_lines.append(border)

        return formatted_lines

    def _find_closing_marker(self, text, marker, start):
        """Find an unescaped closing inline marker."""
        pos = start
        while True:
            pos = text.find(marker, pos)
            if pos == -1:
                return -1
            backslashes = 0
            check = pos - 1
            while check >= 0 and text[check] == '\\':
                backslashes += 1
                check -= 1
            if backslashes % 2 == 0:
                return pos
            pos += len(marker)

    def _parse_inline_chars(self, text, styles=frozenset()):
        """Parse strong and code spans into styled display characters."""
        chars = []
        i = 0
        while i < len(text):
            if text[i] == '\\' and i + 1 < len(text):
                chars.append((text[i + 1], styles))
                i += 2
                continue

            if text[i] == '`':
                run_end = i + 1
                while run_end < len(text) and text[run_end] == '`':
                    run_end += 1
                marker = text[i:run_end]
                close = self._find_closing_marker(text, marker, run_end)
                if close != -1:
                    code_styles = styles | {'code'}
                    chars.extend((char, code_styles)
                                 for char in text[run_end:close])
                    i = close + len(marker)
                    continue

            marker = None
            if text.startswith('**', i):
                marker = '**'
            elif text.startswith('__', i):
                marker = '__'
            if marker:
                close = self._find_closing_marker(
                    text, marker, i + len(marker))
                if close != -1:
                    strong_styles = styles | {'strong'}
                    chars.extend(self._parse_inline_chars(
                        text[i + len(marker):close], strong_styles))
                    i = close + len(marker)
                    continue

            chars.append((text[i], styles))
            i += 1

        start = 0
        end = len(chars)
        while start < end and chars[start][0].isspace():
            start += 1
        while end > start and chars[end - 1][0].isspace():
            end -= 1
        return chars[start:end]

    def _styled_width(self, chars):
        return sum(self.char_width(char) for char, _ in chars)

    def _wrap_styled_chars(self, chars, width):
        """Wrap styled characters without losing their inline formatting."""
        if not chars:
            return [[]]
        remaining = list(chars)
        lines = []
        while remaining:
            display_width = 0
            end = 0
            for end, (char, _) in enumerate(remaining, 1):
                char_width = self.char_width(char)
                if display_width + char_width > width:
                    end -= 1
                    break
                display_width += char_width
            else:
                lines.append(remaining)
                break

            if end == 0:
                end = 1
            candidate = remaining[:end]
            whitespace_breaks = [
                i for i, (char, _) in enumerate(candidate) if char.isspace()
            ]
            if whitespace_breaks and whitespace_breaks[-1] > 0:
                end = whitespace_breaks[-1]
                candidate = remaining[:end]

            while candidate and candidate[-1][0].isspace():
                candidate.pop()
            lines.append(candidate)
            remaining = remaining[end:]
            while remaining and remaining[0][0].isspace():
                remaining.pop(0)
        return lines

    def _render_inline_html(self, chars):
        if not chars:
            return ""
        parts = []
        run = []
        current_styles = chars[0][1]

        def flush_run():
            if not run:
                return
            content = html.escape(''.join(run), quote=True)
            if 'code' in current_styles:
                content = f"<code>{content}</code>"
            if 'strong' in current_styles:
                content = f"<strong>{content}</strong>"
            parts.append(content)
            run.clear()

        for char, styles in chars:
            if styles != current_styles:
                flush_run()
                current_styles = styles
            run.append(char)
        flush_run()
        return ''.join(parts)

    def _pad_styled_line(self, chars, width, alignment):
        padding = max(0, width - self._styled_width(chars))
        if alignment == 'right':
            left = padding
        elif alignment == 'center':
            left = padding // 2
        else:
            left = 0
        right = padding - left
        plain = frozenset()
        return ([(' ', plain)] * left + chars + [(' ', plain)] * right)

    def _render_html_table(self, parsed):
        """Render a parsed table using minihtml-compatible block elements."""
        rows, separator_idx, alignments = parsed
        display_rows = []
        natural_widths = [3] * len(rows[0])
        for i, row in enumerate(rows):
            if i == separator_idx:
                continue
            styled_row = [self._parse_inline_chars(cell) for cell in row]
            display_rows.append(styled_row)
            for j, chars in enumerate(styled_row):
                natural_widths[j] = max(
                    natural_widths[j], self._styled_width(chars))

        col_widths = self._fit_column_widths(
            natural_widths, self._get_table_max_width(), "html")
        rendered = []
        for row_idx, styled_row in enumerate(display_rows):
            wrapped_cells = [
                self._wrap_styled_chars(chars, col_widths[j])
                for j, chars in enumerate(styled_row)
            ]
            row_height = max(len(lines) for lines in wrapped_cells)
            visual_lines = []
            for line_idx in range(row_height):
                pieces = []
                for j, lines in enumerate(wrapped_cells):
                    chars = lines[line_idx] if line_idx < len(lines) else []
                    padded = self._pad_styled_line(
                        chars, col_widths[j], alignments[j])
                    pieces.append(
                        ' ' + self._render_inline_html(padded) + ' ')
                visual_lines.append(
                    '<div class="visual-row">' + ''.join(pieces) + '</div>')

            row_classes = ["logical-row"]
            if row_idx == 0:
                row_classes.append("header-row")
            if row_idx == len(display_rows) - 1:
                row_classes.append("last-row")
            rendered.append(
                f'<div class="{" ".join(row_classes)}">' +
                ''.join(visual_lines) + '</div>')

        return (
            '<body id="term-chat-table" style="margin:0;padding:0">'
            '<style>'
            '.table{color:var(--foreground);font-family:var(--font-mono);'
            'font-size:1rem;line-height:1.25rem;white-space:pre;'
            'display:block;margin:0.25rem 0 0.5rem 0;'
            'border:1px solid color(var(--foreground) alpha(0.35));'
            'border-radius:2px}'
            '.logical-row{margin:0;padding-top:0.15rem;'
            'padding-bottom:0.15rem;'
            'border-bottom:1px solid color(var(--foreground) alpha(0.2))}'
            '.header-row{padding-top:0.2rem;padding-bottom:0.2rem;'
            'border-bottom-color:'
            'color(var(--foreground) alpha(0.35))}'
            '.last-row{border-bottom-width:0}'
            '.visual-row{margin:0;padding:0}'
            'code{font-family:var(--font-mono);color:var(--cyanish);'
            'background-color:color(var(--foreground) alpha(0.08))}'
            'strong{font-weight:bold}'
            '</style><div class="table">' + ''.join(rendered) +
            '</div></body>'
        )

    def format(self, text, flush=False):
        """
        Process the incoming text chunk.
        If flush is True, it returns all buffered content formatted.
        """
        self._html_tables = []

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
        html_table_lines = []

        def flush_buffer():
            if self.table_buffer:
                parsed = self._parse_table(self.table_buffer)
                if self._get_table_style() == "html" and parsed is not None:
                    source_header = "≡ source"
                    source_lines = [source_header] + [
                        "  " + line for line in self.table_buffer
                    ]
                    html_table_lines.append({
                        "start_line": len(output_lines),
                        "line_count": len(source_lines),
                        "fold_start_column": len(source_header),
                        "html": self._render_html_table(parsed),
                    })
                    output_lines.extend(source_lines)
                else:
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
            for table in html_table_lines:
                start_line = table.pop("start_line")
                line_count = table.pop("line_count")
                fold_start_column = table.pop("fold_start_column")
                block_start = sum(len(line) + 1
                                  for line in output_lines[:start_line])
                source = '\n'.join(
                    output_lines[start_line:start_line + line_count])
                table["start"] = block_start + fold_start_column
                table["end"] = block_start + len(source)
                self._html_tables.append(table)
            if not flush:
                res += '\n'
            return res
        return ""
