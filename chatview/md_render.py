import html
from html.parser import HTMLParser
import re
import unicodedata

import sublime

from .inline_math import render_inline_math

# Zero-indent NBSP line that stops Sublime's native gutter fold from swallowing trailing text.
FOLD_TERMINATOR = "\u00a0"


class MarkdownFormatter:
    """
    Helper class to format markdown text, specifically aligning tables
    with CJK character support. Supports stateful streaming.
    """

    _HTML_LINE_BREAK_RE = re.compile(r'<br\s*/?\s*>', re.IGNORECASE)
    _EXPLICIT_LINE_BREAK = '\n'

    def __init__(self, max_width_getter=None, table_style_getter=None):
        self.table_buffer = []
        self.in_code_block = False
        self.code_fence_char = None
        self.code_fence_len = 0
        self.remaining_text = ""
        self.max_width_getter = max_width_getter
        self.table_style_getter = table_style_getter
        self._html_tables = []

    def reset(self):
        """Reset internal buffers and code block fence states."""
        self.table_buffer.clear()
        self.in_code_block = False
        self.code_fence_char = None
        self.code_fence_len = 0
        self.remaining_text = ""
        self._html_tables.clear()

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
        for i, row in enumerate(rows):
            if i == separator_idx:
                continue
            rows[i] = [render_inline_math(cell) for cell in row]
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
        if self.table_style_getter is not None:
            try:
                value = (
                    self.table_style_getter()
                    if callable(self.table_style_getter)
                    else self.table_style_getter
                )
                if isinstance(value, str) and value:
                    return value
            except Exception:
                pass
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

            if text[i] == '<':
                line_break = self._HTML_LINE_BREAK_RE.match(text, i)
                if line_break:
                    chars.append((self._EXPLICIT_LINE_BREAK, styles))
                    i = line_break.end()
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

    def _split_explicit_lines(self, chars):
        """Split styled characters at line breaks created from <br>."""
        lines = [[]]
        for char, styles in chars:
            if char == self._EXPLICIT_LINE_BREAK:
                lines.append([])
            else:
                lines[-1].append((char, styles))
        return lines

    def _styled_line_width(self, chars):
        """Return the display width of one styled line."""
        return sum(self.char_width(char) for char, _ in chars)

    def _widest_styled_line_width(self, chars):
        """Return the widest explicit line in styled cell content."""
        return max(self._styled_line_width(line)
                   for line in self._split_explicit_lines(chars))

    def _wrap_styled_chars(self, chars, width):
        """Honor explicit breaks, then wrap without losing formatting."""
        lines = []
        for explicit_line in self._split_explicit_lines(chars):
            lines.extend(self._wrap_styled_segment(explicit_line, width))
        return lines

    def _wrap_styled_segment(self, chars, width):
        """Wrap one segment that contains no explicit line breaks."""
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
        padding = max(0, width - self._styled_line_width(chars))
        if alignment == 'right':
            left = padding
        elif alignment == 'center':
            left = padding // 2
        else:
            left = 0
        right = padding - left
        plain = frozenset()
        return ([(' ', plain)] * left + chars + [(' ', plain)] * right)

    def _render_table_inner_html(self, parsed):
        """Render a parsed table as minihtml <style> + <div class='table'> markup."""
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
                    natural_widths[j],
                    self._widest_styled_line_width(chars),
                )

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
            '<style>'
            '.table{color:var(--foreground);font-family:monospace;'
            'font-size:1rem;line-height:1.25rem;white-space:pre;'
            'display:block;margin:0.25rem 0 0.5rem 0;'
            'border:1px solid color(var(--foreground) alpha(0.35));'
            'border-radius:2px}'
            '.logical-row{margin:0;padding-top:0.4rem;'
            'padding-bottom:0.4rem;'
            'border-bottom:1px solid color(var(--foreground) alpha(0.2))}'
            '.header-row{padding-top:0.45rem;padding-bottom:0.45rem;'
            'border-bottom-color:'
            'color(var(--foreground) alpha(0.35))}'
            '.last-row{border-bottom-width:0}'
            '.visual-row{margin:0;padding:0}'
            '.table code{font-family:monospace;color:var(--cyanish);'
            'background-color:color(var(--foreground) alpha(0.08))}'
            '.table strong{font-weight:bold}'
            '</style><div class="table">' + ''.join(rendered) +
            '</div>'
        )

    def _render_html_table(self, parsed):
        """Render a parsed table using minihtml-compatible block elements."""
        return (
            '<body id="term-chat-table" style="margin:0;padding:0">'
            + self._render_table_inner_html(parsed)
            + '</body>'
        )

    def format(self, text, flush=False, format_tables=True):
        """
        Process the incoming text chunk.
        If flush is True, it returns all buffered content formatted.
        When format_tables is False, inline math is converted while keeping
        raw Markdown table syntax intact for downstream preview rendering.
        """
        self._html_tables = []

        # Combine with leftover from previous chunk
        combined_text = (
            (self.remaining_text + text).replace("\r\n", "\n").expandtabs(4)
        )

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
            emitted_html_table = False
            if self.table_buffer:
                parsed = self._parse_table(self.table_buffer)
                if self._get_table_style() == "html" and parsed is not None:
                    source_header = "≡"
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
                    emitted_html_table = True
                else:
                    output_lines.extend(self.format_table(self.table_buffer))
                self.table_buffer.clear()
            return emitted_html_table

        for line in lines:
            stripped = line.strip()

            if not self.in_code_block:
                if stripped.startswith(('```', '~~~')):
                    char = stripped[0]
                    count = len(stripped) - len(stripped.lstrip(char))
                    info = stripped[count:].strip()
                    if count >= 3 and not (char == '`' and '`' in info):
                        flush_buffer()
                        self.code_fence_char = char
                        self.code_fence_len = count
                        self.in_code_block = True
                        output_lines.append(line)
                        continue
            else:
                if stripped.startswith(self.code_fence_char * self.code_fence_len):
                    char = self.code_fence_char
                    count = len(stripped) - len(stripped.lstrip(char))
                    if stripped[count:].strip() == "":
                        self.code_fence_char = None
                        self.code_fence_len = 0
                        self.in_code_block = False
                        output_lines.append(line)
                        continue
                output_lines.append(line)
                continue

            if not stripped.startswith("⏺"):
                line = render_inline_math(line)
                stripped = line.strip()

            # Detect table rows - must start with | and have at least one more |
            if format_tables and stripped.startswith('|') and '|' in stripped[1:]:
                self.table_buffer.append(line)
            else:
                if flush_buffer() and not stripped:
                    output_lines.append(FOLD_TERMINATOR)
                else:
                    output_lines.append(line)

        if flush:
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
            elif self._html_tables:
                # End with \n<NBSP>\n so gutter fold stops and later appends stay separate
                tail = res[self._html_tables[-1]["end"]:]
                if tail == "":
                    res += f"\n{FOLD_TERMINATOR}\n"
                elif tail == f"\n{FOLD_TERMINATOR}":
                    res += "\n"
            return res
        return ""


_LIST_ITEM_RE = re.compile(r'^(\s*)([-*+]|\d+[.)])(\s+.*)$')
_LIST_SEP_HTML = '<div class="chat-list-sep"></div>'
_LIST_SEP_RE = re.compile(
    r'(?:<p>\s*)?<div class="chat-list-sep"></div>(?:\s*</p>)?\n?'
)
_RAW_LIST_SEP_RE = re.compile(r'[ \t]*<div class="chat-list-sep"></div>\n?')
_TOOL_FILE_LINE_RE = re.compile(
    r'^⏺\s+(Read|Edit|Write|read|edit|write|fileChange|'
    r'ViewFile|ReadFile|EditFile|WriteFile)\s+(\S+)(.*)$'
)


def strip_list_separators(text):
    """Remove internal list-separation markers when falling back to raw text."""
    if text and _LIST_SEP_HTML in text:
        return _RAW_LIST_SEP_RE.sub('', text)
    return text


def _parse_fence_open(stripped):
    """Return (char, count, info) if stripped opens a fenced code block, else None."""
    if not stripped.startswith(('```', '~~~')):
        return None
    char = stripped[0]
    count = len(stripped) - len(stripped.lstrip(char))
    info = stripped[count:].strip()
    if count < 3 or (char == '`' and '`' in info):
        return None
    return char, count, info


def _is_fence_close(stripped, fence_char, fence_len):
    """Return True if stripped closes an active fenced code block."""
    if not fence_char or not stripped.startswith(fence_char * fence_len):
        return False
    count = len(stripped) - len(stripped.lstrip(fence_char))
    return count >= fence_len and stripped[count:].strip() == ""


def normalize_markdown_list_indentation(text):
    """Normalize 2-space or irregular list indentation to 4-space indentation.

    Ensures Python-Markdown (used by mdpopups) recognizes nested sub-lists
    written with 2-space or 3-space indentation and separates adjacent lists of
    different types, leaving fenced code blocks, horizontal rules, and
    non-list content untouched.
    """
    if not text:
        return text

    lines = text.split('\n')
    new_lines = []
    in_fence = False
    fence_char = None
    fence_len = 0
    indent_stack = []
    last_root_kind = None
    consecutive_blanks = 0

    for line in lines:
        stripped = line.strip()

        if in_fence:
            if _is_fence_close(stripped, fence_char, fence_len):
                in_fence = False
                fence_char = None
                fence_len = 0
            new_lines.append(line)
            consecutive_blanks = 0
            continue

        fence = _parse_fence_open(stripped)
        if fence is not None:
            in_fence = True
            fence_char, fence_len, _ = fence
            if line.startswith(fence_char * 3) or not indent_stack:
                indent_stack.clear()
                last_root_kind = None
            new_lines.append(line)
            consecutive_blanks = 0
            continue

        if not stripped:
            consecutive_blanks += 1
            if consecutive_blanks >= 2:
                indent_stack.clear()
            new_lines.append(line)
            continue
        consecutive_blanks = 0

        clean = stripped.replace(' ', '')
        if len(clean) >= 3 and set(clean) in ({'-'}, {'*'}, {'_'}):
            indent_stack.clear()
            last_root_kind = None
            new_lines.append(line)
            continue

        m = _LIST_ITEM_RE.match(line)
        if m:
            raw_spaces, marker, rest = m.groups()
            raw_len = len(raw_spaces.expandtabs(4))
            list_kind = 'ol' if marker[0].isdigit() else 'ul'
            prev_kind = None

            # Python-Markdown requires a blank line before a root list following text.
            if (
                not indent_stack
                and raw_len < 4
                and rest.strip()
                and (list_kind == 'ul' or marker == '1.')
                and new_lines
                and new_lines[-1].strip()
            ):
                new_lines.append('')

            if indent_stack and raw_len - indent_stack[-1][0] >= 2:
                # Deeper by >= 2 spaces: start a nested sub-list.
                norm_len = len(indent_stack) * 4
                indent_stack.append((raw_len, norm_len, list_kind))
            else:
                while indent_stack and indent_stack[-1][0] > raw_len:
                    indent_stack.pop()
                if indent_stack:
                    # Same level (or 1-space typo snapped to parent): track kind switch.
                    top_raw, norm_len, prev_kind = indent_stack[-1]
                    indent_stack[-1] = (top_raw, norm_len, list_kind)
                else:
                    # Root level (or resumed after blank lines): check last root kind.
                    norm_len, prev_kind = 0, last_root_kind
                    indent_stack.append((raw_len, 0, list_kind))

            if prev_kind and prev_kind != list_kind:
                if new_lines and new_lines[-1].strip():
                    new_lines.append('')
                new_lines.append((' ' * norm_len) + _LIST_SEP_HTML)
                new_lines.append('')

            if norm_len == 0:
                last_root_kind = list_kind

            new_lines.append((' ' * norm_len) + marker + rest)
        else:
            raw_indent = len(line) - len(line.lstrip())
            if indent_stack and raw_indent > 0:
                while indent_stack and indent_stack[-1][0] > raw_indent:
                    indent_stack.pop()
                if indent_stack:
                    diff = indent_stack[-1][1] - indent_stack[-1][0]
                    new_indent = max(0, raw_indent + diff)
                    new_lines.append((' ' * new_indent) + line.lstrip())
                else:
                    last_root_kind = None
                    new_lines.append(line)
            else:
                indent_stack.clear()
                last_root_kind = None
                new_lines.append(line)

    return '\n'.join(new_lines)


def _render_tool_line_html(line):
    """Render a ⏺ tool-call line as an isolated minihtml block with uniform styling."""
    bullet = '<span class="chat-tool-bullet">⏺</span>'
    match = _TOOL_FILE_LINE_RE.match(line)
    if match:
        tool_name, target, rest = match.group(1), match.group(2), match.group(3)
        esc_attr = html.escape(target, quote=True)
        esc_target = html.escape(target, quote=False)
        tail = (
            f'<a class="chat-tool-link" href="toolfile:{esc_attr}">'
            f'{esc_target}</a>'
        )
        if rest:
            tail += html.escape(rest, quote=False)
    else:
        after_bullet = line[1:].lstrip()
        parts = after_bullet.split(None, 1)
        if not parts:
            return f'<div class="chat-tool-line">{bullet}</div>'
        tool_name = parts[0]
        tail = html.escape(parts[1], quote=False) if len(parts) > 1 else ''
    esc_name = html.escape(tool_name, quote=False)
    sep = ' ' if tail else ''
    body = f'{bullet} <span class="chat-tool-name">{esc_name}</span>{sep}{tail}'
    return f'<div class="chat-tool-line">{body}</div>'


def _flush_tool_group(output, tool_group):
    """Append accumulated tool-call lines as one HTML group and clear buffer."""
    if not tool_group:
        return
    output.append('')
    output.append('<div class="chat-tool-group">\n' + '\n'.join(tool_group) + '\n</div>')
    output.append('')
    tool_group.clear()


def _flush_table_buffer(output, table_buffer, formatter):
    """Append buffered Markdown table as HTML (or inline-math text fallback) and clear."""
    if not table_buffer:
        return
    parsed = formatter._parse_table(table_buffer)
    if parsed is not None:
        output.append('')
        output.append(formatter._render_table_inner_html(parsed))
        output.append('')
    else:
        output.extend(render_inline_math(t) for t in table_buffer)
    table_buffer.clear()


def _insert_tool_group_separators(output):
    """Insert a spacer div at every non-empty boundary touching a tool group."""
    tool_prefix = '<div class="chat-tool-group">\n'
    sep_html = '<div class="chat-tool-sep">&nbsp;</div>'
    non_empty = [i for i, item in enumerate(output) if item.strip()]
    sep_before = {
        curr
        for prev, curr in zip(non_empty, non_empty[1:])
        if output[prev].startswith(tool_prefix)
        or output[curr].startswith(tool_prefix)
    }
    if not sep_before:
        return output
    spaced = []
    for i, item in enumerate(output):
        if i in sep_before:
            spaced.extend(['', sep_html, ''])
        spaced.append(item)
    return spaced


def preprocess_response_markdown(text, max_width=100):
    """Preprocess an agent response Markdown string for mdpopups rendering."""
    if not text:
        return ""

    width = (
        max_width
        if isinstance(max_width, int) and not isinstance(max_width, bool) and max_width > 0
        else 100
    )
    formatter = MarkdownFormatter(max_width_getter=width)

    lines = text.expandtabs(4).split('\n')
    output = []
    table_buffer = []
    tool_group = []
    in_code_block = False
    fence_char = None
    fence_len = 0
    in_diff_block = False
    after_tool_line = False
    saw_blank_after_tool = False

    for idx, line in enumerate(lines):
        stripped = line.strip()

        if not in_code_block:
            if after_tool_line:
                if stripped == "":
                    saw_blank_after_tool = True
                    continue
                if line.startswith("    "):
                    saw_blank_after_tool = False
                    continue

            fence = _parse_fence_open(stripped)
            if fence is not None:
                _flush_table_buffer(output, table_buffer, formatter)
                in_code_block = True
                fence_char, fence_len, info = fence
                lang = info.split(None, 1)[0].lower() if info else ""
                if lang == "diff":
                    in_diff_block = True
                    # An omitted diff belongs to the preceding tool call; only
                    # blanks after the diff closes separate the next tool call.
                    saw_blank_after_tool = False
                    continue
                after_tool_line = False
                saw_blank_after_tool = False
                _flush_tool_group(output, tool_group)
                output.append(line)
                continue
        else:
            if _is_fence_close(stripped, fence_char, fence_len):
                in_code_block = False
                fence_char = None
                fence_len = 0
                if in_diff_block:
                    in_diff_block = False
                    continue
            if in_diff_block:
                continue
            output.append(line)
            continue

        if stripped == "≡" and idx + 1 < len(lines):
            next_stripped = lines[idx + 1].strip()
            if next_stripped.startswith('|') and '|' in next_stripped[1:]:
                continue

        if stripped.startswith('|') and '|' in stripped[1:]:
            after_tool_line = False
            saw_blank_after_tool = False
            _flush_tool_group(output, tool_group)
            table_buffer.append(line)
        elif stripped.startswith("⏺"):
            _flush_table_buffer(output, table_buffer, formatter)
            if tool_group and saw_blank_after_tool:
                _flush_tool_group(output, tool_group)
            tool_group.append(_render_tool_line_html(stripped))
            after_tool_line = True
            saw_blank_after_tool = False
        else:
            after_tool_line = False
            saw_blank_after_tool = False
            _flush_tool_group(output, tool_group)
            _flush_table_buffer(output, table_buffer, formatter)
            output.append(render_inline_math(line))

    _flush_tool_group(output, tool_group)
    _flush_table_buffer(output, table_buffer, formatter)
    output = _insert_tool_group_separators(output)

    return normalize_markdown_list_indentation('\n'.join(output))


class _OrderedListHtmlTransformer(HTMLParser):
    """Rewrite <ol>/<li> HTML into numbered block divs for minihtml rendering."""

    _MARKER_WRAPPER_TAGS = frozenset({'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})
    _OL_TYPES = frozenset({'1', 'a', 'A', 'i', 'I'})
    _OL_DROP_ATTRS = frozenset({'start', 'reversed', 'type'})
    _LI_DROP_ATTRS = frozenset({'value'})
    _ROMAN_NUMERALS = (
        (1000, 'm'), (900, 'cm'), (500, 'd'), (400, 'cd'),
        (100, 'c'), (90, 'xc'), (50, 'l'), (40, 'xl'),
        (10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i'),
    )

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._out = []
        self._list_stack = []
        self._li_stack = []
        self._pre_depth = 0

    def get_html(self):
        return ''.join(self._out)

    def _raw_start_tag(self, tag, attrs, self_closing=False):
        raw = self.get_starttag_text()
        if raw:
            return raw
        parts = [f'<{tag}']
        for key, value in attrs:
            if value is None:
                parts.append(f' {key}')
            else:
                parts.append(f' {key}="{html.escape(value, quote=True)}"')
        parts.append(' />' if self_closing else '>')
        return ''.join(parts)

    def _flush_pending_num(self, trailing_space=True):
        if not self._li_stack:
            return False
        top_li = self._li_stack[-1]
        pending = top_li['pending_num']
        if pending is None:
            return False
        top_li['pending_num'] = None
        kind = pending[0]
        if kind == 'fixed':
            suffix = ' ' if trailing_space else ''
            self._out.append(f'<span class="chat-ol-num">{pending[1]}</span>{suffix}')
        else:
            _, frame, offset = pending
            out_idx = len(self._out)
            self._out.append('')
            frame['deferred'].append((out_idx, offset, trailing_space))
        return True

    def _close_li(self):
        self._flush_pending_num(trailing_space=False)
        top_li = self._li_stack.pop()
        self._out.append('</div>' if top_li['owner'] == 'ol' else '</li>')

    def _close_list(self, frame, closing_tag=None):
        if frame['tag'] == 'ol':
            start = frame['count']
            for out_idx, offset, trailing_space in frame['deferred']:
                label = self._format_ol_marker(start - offset, frame['list_type'])
                suffix = ' ' if trailing_space else ''
                self._out[out_idx] = (
                    f'<span class="chat-ol-num">{label}</span>{suffix}'
                )
            self._out.append('</div>')
        else:
            self._out.append(f'</{closing_tag}>' if closing_tag else '</ul>')

    @staticmethod
    def _parse_int_attr(attrs, name):
        for key, value in attrs:
            if key.lower() == name and value is not None:
                try:
                    return int(value.strip())
                except (ValueError, TypeError):
                    return None
        return None

    @classmethod
    def _parse_type_attr(cls, attrs):
        for key, value in attrs:
            if key.lower() == 'type' and value is not None:
                candidate = value.strip()
                if candidate in cls._OL_TYPES:
                    return candidate
                return '1'
        return '1'

    @staticmethod
    def _has_attr(attrs, name):
        return any(key.lower() == name for key, _ in attrs)

    @staticmethod
    def _to_alpha(n):
        chars = []
        while n > 0:
            n, rem = divmod(n - 1, 26)
            chars.append(chr(ord('a') + rem))
        return ''.join(reversed(chars))

    @classmethod
    def _to_roman(cls, n):
        parts = []
        for value, symbol in cls._ROMAN_NUMERALS:
            while n >= value:
                parts.append(symbol)
                n -= value
        return ''.join(parts)

    @classmethod
    def _format_ol_marker(cls, num, list_type):
        if list_type == 'a' and num > 0:
            return f'{cls._to_alpha(num)}.'
        if list_type == 'A' and num > 0:
            return f'{cls._to_alpha(num).upper()}.'
        if list_type == 'i' and 1 <= num <= 3999:
            return f'{cls._to_roman(num)}.'
        if list_type == 'I' and 1 <= num <= 3999:
            return f'{cls._to_roman(num).upper()}.'
        return f'{num}.'

    @staticmethod
    def _build_div_start_tag(base_class, attrs, drop_attrs):
        classes = [base_class]
        seen_classes = {base_class}
        extra_parts = []
        for key, value in attrs:
            lower = key.lower()
            if lower in drop_attrs:
                continue
            if lower == 'class':
                if value:
                    for token in value.split():
                        if token not in seen_classes:
                            seen_classes.add(token)
                            classes.append(token)
                continue
            if value is None:
                extra_parts.append(f' {key}')
            else:
                extra_parts.append(f' {key}="{html.escape(value, quote=True)}"')
        class_str = html.escape(' '.join(classes), quote=True)
        return f'<div class="{class_str}"{"".join(extra_parts)}>'

    def handle_starttag(self, tag, attrs):
        lower = tag.lower()
        if lower == 'pre':
            if self._pre_depth == 0:
                self._flush_pending_num()
            self._pre_depth += 1
            self._out.append(self._raw_start_tag(tag, attrs))
            return

        if self._pre_depth > 0:
            self._out.append(self._raw_start_tag(tag, attrs))
            return

        if lower == 'ol':
            self._flush_pending_num()
            start = self._parse_int_attr(attrs, 'start')
            is_reversed = self._has_attr(attrs, 'reversed')
            step = -1 if is_reversed else 1
            list_type = self._parse_type_attr(attrs)
            if start is not None:
                next_num = start
            elif is_reversed:
                next_num = None
            else:
                next_num = 1
            self._list_stack.append({
                'tag': 'ol',
                'next_num': next_num,
                'step': step,
                'list_type': list_type,
                'count': 0,
                'deferred': [],
            })
            self._out.append(
                self._build_div_start_tag('chat-ol', attrs, self._OL_DROP_ATTRS)
            )
            return

        if lower == 'ul':
            self._flush_pending_num()
            self._list_stack.append({'tag': 'ul'})
            self._out.append(self._raw_start_tag(tag, attrs))
            return

        if lower == 'li':
            top_list = self._list_stack[-1] if self._list_stack else None
            if top_list is None:
                self._out.append(self._raw_start_tag(tag, attrs))
                return
            depth = len(self._list_stack)
            while self._li_stack and self._li_stack[-1]['depth'] >= depth:
                self._close_li()
            if top_list['tag'] == 'ol':
                top_list['count'] += 1
                explicit_val = self._parse_int_attr(attrs, 'value')
                if explicit_val is not None:
                    num = explicit_val
                    top_list['next_num'] = num + top_list['step']
                    marker = self._format_ol_marker(num, top_list['list_type'])
                    pending_num = ('fixed', marker)
                elif top_list['next_num'] is not None:
                    num = top_list['next_num']
                    top_list['next_num'] += top_list['step']
                    marker = self._format_ol_marker(num, top_list['list_type'])
                    pending_num = ('fixed', marker)
                else:
                    offset = top_list['count'] - 1
                    pending_num = ('deferred', top_list, offset)
                self._li_stack.append({
                    'owner': 'ol',
                    'depth': depth,
                    'pending_num': pending_num,
                })
                self._out.append(
                    self._build_div_start_tag('chat-li', attrs, self._LI_DROP_ATTRS)
                )
            else:
                self._li_stack.append({
                    'owner': 'ul',
                    'depth': depth,
                    'pending_num': None,
                })
                self._out.append(self._raw_start_tag(tag, attrs))
            return

        if self._li_stack and self._li_stack[-1]['pending_num'] is not None:
            if lower in self._MARKER_WRAPPER_TAGS:
                self._out.append(self._raw_start_tag(tag, attrs))
                self._flush_pending_num()
                return
            self._flush_pending_num()

        self._out.append(self._raw_start_tag(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if self._pre_depth == 0:
            self._flush_pending_num()
        self._out.append(self._raw_start_tag(tag, attrs, self_closing=True))

    def handle_endtag(self, tag):
        lower = tag.lower()
        if lower == 'pre':
            if self._pre_depth > 0:
                self._pre_depth -= 1
            self._out.append(f'</{tag}>')
            return

        if self._pre_depth > 0:
            self._out.append(f'</{tag}>')
            return

        if lower == 'li':
            if self._li_stack and self._li_stack[-1]['depth'] == len(self._list_stack):
                self._close_li()
            elif not self._list_stack:
                self._out.append(f'</{tag}>')
            return

        if lower in ('ol', 'ul'):
            match_idx = None
            for idx in range(len(self._list_stack) - 1, -1, -1):
                if self._list_stack[idx]['tag'] == lower:
                    match_idx = idx
                    break
            if match_idx is None:
                self._out.append(f'</{tag}>')
                return
            while len(self._list_stack) > match_idx:
                depth = len(self._list_stack)
                while self._li_stack and self._li_stack[-1]['depth'] >= depth:
                    self._close_li()
                frame = self._list_stack.pop()
                closing_tag = tag if len(self._list_stack) == match_idx else None
                self._close_list(frame, closing_tag=closing_tag)
            return

        self._out.append(f'</{tag}>')

    def handle_data(self, data):
        if (self._pre_depth == 0
                and self._li_stack
                and self._li_stack[-1]['pending_num'] is not None):
            if data.strip():
                self._flush_pending_num()
                self._out.append(data.lstrip())
                return
        self._out.append(data)

    def handle_entityref(self, name):
        if self._pre_depth == 0:
            self._flush_pending_num()
        self._out.append(f'&{name};')

    def handle_charref(self, name):
        if self._pre_depth == 0:
            self._flush_pending_num()
        self._out.append(f'&#{name};')

    def handle_comment(self, data):
        self._out.append(f'<!--{data}-->')

    def handle_decl(self, decl):
        self._out.append(f'<!{decl}>')

    def handle_pi(self, data):
        self._out.append(f'<?{data}>')

    def unknown_decl(self, data):
        self._out.append(f'<![{data}]>')

    def close(self):
        super().close()
        while self._list_stack:
            depth = len(self._list_stack)
            while self._li_stack and self._li_stack[-1]['depth'] >= depth:
                self._close_li()
            frame = self._list_stack.pop()
            self._close_list(frame)
        while self._li_stack:
            self._close_li()


def render_ordered_lists_as_html(html_text):
    """Rewrite <ol>/<li> blocks to explicit numbered divs for minihtml."""
    if not html_text:
        return html_text
    if _LIST_SEP_HTML in html_text:
        html_text = _LIST_SEP_RE.sub('', html_text)
    if '<ol' not in html_text.lower():
        return html_text
    parser = _OrderedListHtmlTransformer()
    parser.feed(html_text)
    parser.close()
    return parser.get_html()
