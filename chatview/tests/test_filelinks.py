import sys
import unittest
from unittest.mock import MagicMock

mock_sublime = MagicMock()
mock_sublime.DRAW_NO_FILL = 1
mock_sublime.DRAW_NO_OUTLINE = 2
mock_sublime.DRAW_SOLID_UNDERLINE = 4
sys.modules.setdefault("sublime", mock_sublime)

from chatview.filelinks import FileLink, UrlLink, find_file_links, find_url_links, open_link

FILES = {
    "/proj/src/main.py",
    "/proj/TermMate.sublime-settings",
    "/proj/README.md",
    "/other/lib/util.zig",
    "/abs/config.json",
}


def isfile(path):
    return path in FILES


def links(text, base_dirs=("/proj",), skip=()):
    return find_file_links(text, list(base_dirs), skip, isfile=isfile)


class TestFindFileLinks(unittest.TestCase):

    def test_relative_path(self):
        text = "see src/main.py for details"
        self.assertEqual(links(text), [FileLink(4, 15, "/proj/src/main.py")])

    def test_line_and_column(self):
        self.assertEqual(links("src/main.py:42")[0], FileLink(0, 14, "/proj/src/main.py", 42))
        self.assertEqual(links("src/main.py:42:7")[0], FileLink(0, 16, "/proj/src/main.py", 42, 7))

    def test_line_range_fragment(self):
        self.assertEqual(links("src/main.py#L10-25")[0], FileLink(0, 18, "/proj/src/main.py", 10, 0, 25))
        self.assertEqual(links("src/main.py#L10-L25")[0], FileLink(0, 19, "/proj/src/main.py", 10, 0, 25))
        self.assertEqual(links("src/main.py#L10C3")[0], FileLink(0, 17, "/proj/src/main.py", 10, 3))
        # :10-20 is a line range, like #L10-20 (not line 10, column 20)
        self.assertEqual(links("src/main.py:10-20")[0], FileLink(0, 17, "/proj/src/main.py", 10, 0, 20))

    def test_at_tag(self):
        self.assertEqual(links("@src/main.py")[0], FileLink(0, 12, "/proj/src/main.py"))

    def test_inline_code_is_not_a_link(self):
        self.assertEqual(links("`src/main.py:3`"), [])
        self.assertEqual(links("run ``cat src/main.py`` first"), [])
        # Text around the code span still links
        self.assertEqual(links("`x` then README.md"), [FileLink(9, 18, "/proj/README.md")])
        # A lone backtick doesn't start a code span
        self.assertEqual(links("it's ` and README.md"), [FileLink(11, 20, "/proj/README.md")])

    def test_fenced_code_is_not_a_link(self):
        text = "see README.md\n```python\nopen('src/main.py')\n```\nthen src/main.py\n"
        self.assertEqual(links(text), [FileLink(4, 13, "/proj/README.md"),
                                       FileLink(53, 64, "/proj/src/main.py")])
        self.assertEqual(links("~~~\nsrc/main.py\n~~~\n"), [])

    def test_tool_call_diff_is_code(self):
        # Tool-call diffs are fenced and indented 4 spaces; the tool line above is not code
        text = "⏺ Edit src/main.py#L3\n    ````diff\n    +README.md\n    ````\n"
        self.assertEqual(links(text), [FileLink(7, 21, "/proj/src/main.py", 3)])

    def test_fence_closes_only_on_matching_fence(self):
        # A shorter fence or one with an info string inside the block doesn't close it
        text = "````\n```\nsrc/main.py\n```python\nREADME.md\n````\nREADME.md"
        self.assertEqual(links(text), [FileLink(len(text) - 9, len(text), "/proj/README.md")])

    def test_unclosed_fence_runs_to_end(self):
        # While a reply is still streaming, an open fence covers the rest of the text
        self.assertEqual(links("README.md\n```\nsrc/main.py\n"), [FileLink(0, 9, "/proj/README.md")])

    def test_absolute_path(self):
        self.assertEqual(links("open /abs/config.json now")[0], FileLink(5, 21, "/abs/config.json"))

    def test_hyphenated_extension_and_parent_dirs(self):
        text = "⏺ Edit ../TermMate.sublime-settings#L56"
        self.assertEqual(links(text, base_dirs=("/proj/sub",)),
                         [FileLink(7, 39, "/proj/TermMate.sublime-settings", 56)])

    def test_trailing_period_not_included(self):
        self.assertEqual(links("Edit README.md.")[0], FileLink(5, 14, "/proj/README.md"))

    def test_later_base_dirs(self):
        self.assertEqual(links("lib/util.zig", base_dirs=("/proj", "/other"))[0],
                         FileLink(0, 12, "/other/lib/util.zig"))

    def test_missing_files_and_non_paths_ignored(self):
        self.assertEqual(links("e.g. liburing.so and src/missing.py, v1.2"), [])

    def test_urls_ignored(self):
        self.assertEqual(links("https://example.com/src/main.py"), [])

    def test_skip_spans(self):
        text = "src/main.py and README.md"
        self.assertEqual(links(text, skip=[(0, 11)]), [FileLink(16, 25, "/proj/README.md")])

    def test_location(self):
        self.assertEqual(FileLink(0, 1, "/a.py").location(), "/a.py")
        self.assertEqual(FileLink(0, 1, "/a.py", 4).location(), "/a.py:4")
        self.assertEqual(FileLink(0, 1, "/a.py", 4, 0, 9).location(), "/a.py:4-9")


class TestFindUrlLinks(unittest.TestCase):

    def test_url(self):
        text = "docs at https://example.com/docs/setup?x=1#top here"
        self.assertEqual(find_url_links(text), [UrlLink(8, 46, "https://example.com/docs/setup?x=1#top")])
        self.assertEqual(find_url_links("http://localhost:8080/"), [UrlLink(0, 22, "http://localhost:8080/")])

    def test_trailing_punctuation_and_brackets(self):
        self.assertEqual(find_url_links("See https://example.com/a."), [UrlLink(4, 25, "https://example.com/a")])
        self.assertEqual(find_url_links("(https://example.com/a), [x](https://example.com/b)"),
                         [UrlLink(1, 22, "https://example.com/a"), UrlLink(29, 50, "https://example.com/b")])
        self.assertEqual(find_url_links("<https://example.com>"), [UrlLink(1, 20, "https://example.com")])

    def test_markdown_emphasis(self):
        self.assertEqual(find_url_links("**http://127.0.0.1:8000**"), [UrlLink(2, 23, "http://127.0.0.1:8000")])
        self.assertEqual(find_url_links("_https://example.com/a_, ~~https://example.com~~"),
                         [UrlLink(1, 22, "https://example.com/a"), UrlLink(27, 46, "https://example.com")])

    def test_code_and_skip_spans(self):
        self.assertEqual(find_url_links("`https://example.com` and\n```\nhttps://example.com\n```\n"), [])
        self.assertEqual(find_url_links("https://example.com", skip=[(0, 5)]), [])

    def test_not_urls(self):
        self.assertEqual(find_url_links("https:// and http://nodots and xhttps://example.com"), [])

    def test_open_url(self):
        sublime = sys.modules["sublime"]  # the mock the module imported (another test file may have set it)
        sublime.run_command.reset_mock()
        open_link(MagicMock(), UrlLink(0, 1, "https://example.com"))
        sublime.run_command.assert_called_once_with("open_url", {"url": "https://example.com"})


if __name__ == "__main__":
    unittest.main()
