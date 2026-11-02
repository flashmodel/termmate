"""
Clickable file paths and web links in agent replies.

Paths written in plain text (src/main.py, src/main.py:42, src/main.py#L10-25,
/abs/path.py) that exist on disk are underlined, and so are http(s) URLs.
Paths and URLs in inline code or a fenced code block are code, not links.
Hovering one shows an "Open" popup; double-clicking opens the file at the line,
with a `#L10-25` range selected, or the URL in the browser.
"""
import bisect
import html
import os
import re
from dataclasses import dataclass

import sublime

FILE_LINK_REGION_KEY = "chatview_file_links"
FILE_LINK_SCOPE = "markup.underline.link"
FILE_LINK_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.DRAW_SOLID_UNDERLINE
SCAN_DELAY_MS = 300

_PATH_RE = re.compile(
    r"(?<![\w/.~@-])@?"
    r"(?P<path>(?:~|\.{1,2})?/?(?:[\w.+-]+/)*[\w+-][\w.+-]*\.[A-Za-z0-9](?:[\w-]*[A-Za-z0-9])?)"
    r"(?:(?::(?P<line>\d+)(?::(?P<col>\d+)|-(?P<dend>\d+))?)"
    r"|(?:#L(?P<lstart>\d+)(?:C(?P<lcol>\d+)|-L?(?P<lend>\d+))?))?"
)

# A URL runs to whitespace or a Markdown / HTML delimiter; trailing punctuation is dropped below
_URL_RE = re.compile(r"(?<![^\W_])(?<!/)https?://[^\s<>()\[\]'\"`]+")
_URL_TRAIL = ".,;:!?*_~"

# view id -> scan generation (debounces rescans while the agent streams output)
_pending = {}
# view id -> [FileLink | UrlLink], sorted by start
_links = {}
# view id -> what the last scan read (text, skip spans, base dirs): an unchanged chat is not scanned again
_scanned = {}


@dataclass
class FileLink:
    """A file path found in the chat: its span, absolute path, and target line/column/range end (0 = unset)."""

    start: int
    end: int
    path: str
    line: int = 0
    col: int = 0
    end_line: int = 0

    def contains(self, point):
        return self.start <= point < self.end

    def location(self):
        """Path with line or line range, for display."""
        text = self.path
        if self.line:
            text += f":{self.line}"
            if self.end_line > self.line:
                text += f"-{self.end_line}"
        return text


@dataclass
class UrlLink:
    """An http(s) URL found in the chat: its span and the address."""

    start: int
    end: int
    url: str

    def contains(self, point):
        return self.start <= point < self.end

    def location(self):
        return self.url


def _resolve(path, base_dirs, isfile):
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return path if isfile(path) else None
    for base in base_dirs:
        full = os.path.normpath(os.path.join(base, path))
        if isfile(full):
            return full
    return None


_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def code_spans(text):
    """Return sorted (begin, end) spans of Markdown code: fenced blocks and inline `code`.

    A fence (``` or ~~~, any indent, so tool-call diffs count) runs to the closing
    fence of the same character and at least the same length, or to the end of
    the text while it is still streaming. Inline code is matched within a line.
    """
    spans = []
    fence = None  # (char, length, start offset) of the open fence
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = (m.group(1)[0], len(m.group(1)), offset)
            else:
                spans.extend((offset + c.start(), offset + c.end())
                             for c in _INLINE_CODE_RE.finditer(line))
        elif (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]
                and stripped == m.group(1).strip()):
            spans.append((fence[2], offset + len(line)))
            fence = None
        offset += len(line)
    if fence is not None:
        spans.append((fence[2], len(text)))
    return spans


def _skipper(text, skip):
    """Return `inside(offset)`: True when offset lies in Markdown code or one of the skip (begin, end) spans."""
    spans = sorted(list(skip) + code_spans(text))
    starts = [begin for begin, _ in spans]
    # Running maximum of span ends, so one bisect answers "is this offset inside any span"
    reach = []
    furthest = -1
    for _, end in spans:
        furthest = max(furthest, end)
        reach.append(furthest)

    def inside(offset):
        i = bisect.bisect_right(starts, offset) - 1
        return i >= 0 and offset < reach[i]

    return inside


def find_url_links(text, skip=()):
    """Return a UrlLink for every http(s) URL in text outside Markdown code and the skip spans.

    Trailing punctuation (`.` `,` `;` `:` `!` `?`) belongs to the sentence, not the URL, and trailing
    `*` `_` `~` close Markdown emphasis (`**https://example.com**`).
    """
    inside = _skipper(text, skip)
    links = []
    for m in _URL_RE.finditer(text):
        if inside(m.start()):
            continue
        url = m.group().rstrip(_URL_TRAIL)
        if "." in url.split("//", 1)[1].split("/", 1)[0] or "localhost" in url:
            links.append(UrlLink(m.start(), m.start() + len(url), url))
    return links


def find_file_links(text, base_dirs, skip=(), isfile=os.path.isfile):
    """Return a FileLink for every path in text that names an existing file.

    Relative paths are tried against each of base_dirs in order. Paths inside
    Markdown code (inline `code` or a fenced block) are code, not links, and are
    left alone, as are matches that start inside one of the skip (begin, end) spans.
    """
    inside = _skipper(text, skip)
    cache = {}
    links = []
    for m in _PATH_RE.finditer(text):
        if inside(m.start()):
            continue
        path = m.group("path")
        if path not in cache:
            cache[path] = _resolve(path, base_dirs, isfile)
        full = cache[path]
        if not full:
            continue
        line = m.group("line") or m.group("lstart")
        col = m.group("col") or m.group("lcol")
        end_line = m.group("lend") or m.group("dend")  # #L10-25 or :10-20: a line range
        links.append(FileLink(
            m.start(), m.end(), full,
            int(line) if line else 0,
            int(col) if col else 0,
            int(end_line) if end_line else 0,
        ))
    return links


def schedule_scan(view, base_dirs_fn, skip_key=None, end_fn=None):
    """Rescan view for file links after SCAN_DELAY_MS without further calls. `end_fn(view)` is where the
    scan stops (the chat's input line, which is being edited, is not linked); default: the end."""
    generation = _pending.get(view.id(), 0) + 1
    _pending[view.id()] = generation
    sublime.set_timeout_async(lambda: _scan(view, generation, base_dirs_fn, skip_key, end_fn), SCAN_DELAY_MS)


def _scan(view, generation, base_dirs_fn, skip_key, end_fn):
    if _pending.get(view.id()) != generation or not view.is_valid():
        return
    _pending.pop(view.id(), None)
    end = min(end_fn(view), view.size()) if end_fn else view.size()
    text = view.substr(sublime.Region(0, end))
    skip = [(r.begin(), r.end()) for r in view.get_regions(skip_key)] if skip_key else []
    base_dirs = list(base_dirs_fn(view))
    # Typing in the input line or switching to the chat changes nothing above it: keep the links
    state = (hash(text), len(text), tuple(skip), tuple(base_dirs))
    if _scanned.get(view.id()) == state:
        return
    _scanned[view.id()] = state
    urls = find_url_links(text, skip)
    # A path inside a URL is part of the URL
    links = find_file_links(text, base_dirs, skip + [(u.start, u.end) for u in urls])
    links = sorted(links + urls, key=lambda link: link.start)
    _links[view.id()] = links
    view.add_regions(
        FILE_LINK_REGION_KEY,
        [sublime.Region(link.start, link.end) for link in links],
        FILE_LINK_SCOPE,
        "",
        FILE_LINK_FLAGS,
    )


def forget(view):
    """Drop state for a closed view."""
    _pending.pop(view.id(), None)
    _links.pop(view.id(), None)
    _scanned.pop(view.id(), None)


def link_at(view, point):
    for link in _links.get(view.id(), ()):
        if link.contains(point):
            return link
    return None


def open_link(window, link):
    """Open the linked file at its line (select the line range if there is one), or the URL in the browser."""
    if isinstance(link, UrlLink):
        sublime.run_command("open_url", {"url": link.url})
        return
    target = f"{link.path}:{link.line or 1}:{link.col or 1}"
    view = window.open_file(target, sublime.ENCODED_POSITION)
    if link.line and link.end_line > link.line:
        _select_lines(view, link.line, link.end_line)


def _select_lines(view, first, last):
    if view.is_loading():
        sublime.set_timeout(lambda: _select_lines(view, first, last), 50)
        return
    start = view.text_point(first - 1, 0)
    end = view.line(view.text_point(last - 1, 0)).end()
    view.sel().clear()
    view.sel().add(sublime.Region(start, end))
    view.show_at_center(start)


def open_link_at(view, point):
    """Open the file link under point. Returns True if there was one."""
    link = link_at(view, point)
    window = view.window()
    if not link or not window:
        return False
    open_link(window, link)
    return True


def show_link_popup(view, point):
    """Show an "Open" popup for the file link under point. Returns True if there was one."""
    link = link_at(view, point)
    if not link:
        return False
    content = (
        "<body style='margin:0.3em 0.5em'>"
        "<a href='open'>Open</a> "
        f"<span style='color:color(var(--foreground) alpha(0.6))'>{html.escape(link.location())}</span>"
        "</body>"
    )

    def on_navigate(_href):
        view.hide_popup()
        window = view.window()
        if window:
            open_link(window, link)

    view.show_popup(content, sublime.HIDE_ON_MOUSE_MOVE_AWAY, point, 1000, 200, on_navigate)
    return True
