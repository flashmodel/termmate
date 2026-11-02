import re
import sys
import types
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("sublime", MagicMock())

from chatview import toolfold
from chatview.md_render import MarkdownFormatter


class Region:
    def __init__(self, a, b=None):
        self.a = a
        self.b = a if b is None else b

    def begin(self):
        return min(self.a, self.b)

    def end(self):
        return max(self.a, self.b)

    def empty(self):
        return self.a == self.b


fake_sublime = types.SimpleNamespace(
    Region=Region, DRAW_NO_FILL=1, DRAW_NO_OUTLINE=2, DRAW_SOLID_UNDERLINE=4, HIDDEN=8, PERSISTENT=16,
    load_settings=lambda name: types.SimpleNamespace(get=lambda key, default=None: default),
)


class FakeView:
    """A text buffer with regions and folds; regions move with inserts like Sublime's."""

    def __init__(self, text=""):
        self.text = text
        self.regions = {}
        self.folds = []

    def size(self):
        return len(self.text)

    def substr(self, region):
        return self.text[region.begin():region.end()]

    def line(self, point):
        start = self.text.rfind("\n", 0, point) + 1
        end = self.text.find("\n", point)
        return Region(start, len(self.text) if end == -1 else end)

    def insert(self, edit, pos, text):
        self.text = self.text[:pos] + text + self.text[pos:]
        n = len(text)
        shift = lambda p: p + n if p >= pos else p  # noqa: E731
        for key, regions in self.regions.items():
            self.regions[key] = [Region(shift(r.a), shift(r.b)) for r in regions]
        self.folds = [(shift(a), shift(b)) for a, b in self.folds]
        return n

    def erase(self, edit, region):
        a, b = region.begin(), region.end()
        self.text = self.text[:a] + self.text[b:]
        shift = lambda p: p - (b - a) if p >= b else min(p, a)  # noqa: E731
        for key, regions in self.regions.items():
            self.regions[key] = [Region(shift(r.a), shift(r.b)) for r in regions]
        self.folds = [(shift(a), shift(b)) for a, b in self.folds]

    def append(self, text):
        """What term_chat_output_append does: insert before the input line, then fold."""
        end = len(self.text)
        self.insert(None, end, text)
        return toolfold.fold_runs(self, None, end + len(text))

    def append_formatted(self, formatter, chunk):
        """The whole path of a streamed chunk: Markdown formatting, text-start marks, folding."""
        text = formatter.format(chunk)
        end = len(self.text)
        self.insert(None, end, text)
        toolfold.mark_text_starts(self, [end + start for start in formatter.take_text_starts()])
        return toolfold.fold_runs(self, None, end + len(text))

    def get_regions(self, key):
        return list(self.regions.get(key, []))

    def add_regions(self, key, regions, *args):
        self.regions[key] = list(regions)

    def find_all(self, pattern):
        return [Region(m.start(), m.end()) for m in re.finditer(pattern, self.text, re.M)]

    def fold(self, region):
        # Like Sublime, a fold swallows the folds inside it
        a, b = region.begin(), region.end()
        self.folds = [f for f in self.folds if not (a <= f[0] and f[1] <= b)] + [(a, b)]

    def unfold(self, region):
        hit = [f for f in self.folds if f[0] <= region.begin() <= f[1]]
        self.folds = [f for f in self.folds if f not in hit]
        return hit

    def folded_text(self):
        return [self.text[a:b] for a, b in self.folds]


class TestFindRuns(unittest.TestCase):

    def test_runs_and_counts(self):
        text = "Text\n⏺ Bash ls\n⏺ Bash x\n\n    second line\n⏺ Read a.py\n\nDone\n⏺ Grep y\n"
        runs = toolfold.find_runs(text)
        self.assertEqual(len(runs), 2)
        start, end, count, headed, complete, calls = runs[0]
        self.assertEqual(text[start:end], "⏺ Bash ls\n⏺ Bash x\n\n    second line\n⏺ Read a.py")
        self.assertEqual((count, headed, complete), (3, False, True))
        calls = runs[0][5]
        self.assertEqual([text[s:e] for s, _, e, _ in calls],
                         ["⏺ Bash ls", "⏺ Bash x\n\n    second line", "⏺ Read a.py"])
        self.assertEqual(text[calls[1][0]:calls[1][1]], "⏺ Bash x")
        # The last run is still open: more calls may follow
        self.assertEqual(runs[1][2:5], (1, False, False))
        self.assertFalse(runs[1][5][0][3])
        self.assertTrue(toolfold.find_runs(text, final=True)[1][4])

    def test_headed_run(self):
        runs = toolfold.find_runs("⏺ 2 tool calls\n⏺ Bash ls\n⏺ Bash x\nDone\n")
        self.assertEqual(runs[0][2:5], (2, True, True))

    def test_unfinished_last_line_ends_nothing(self):
        self.assertFalse(toolfold.find_runs("⏺ Bash ls\n⏺ Bash x\n⏺")[0][4])
        self.assertFalse(toolfold.find_runs("⏺ Bash ls\n⏺ Bash x\n  ")[0][4])
        self.assertTrue(toolfold.find_runs("⏺ Bash ls\n⏺ Bash x\nDo")[0][4])

    def test_text_start_ends_run(self):
        # An indented list right after a call is the agent's text when it is marked as such
        text = "⏺ Bash ls\n⏺ Read a.py\n\n    - Found X\n    - Found Y\n"
        start = text.index("    - Found X")
        self.assertIn("Found", text[slice(*toolfold.find_runs(text, final=True)[0][:2])])  # unmarked: read as body
        runs = toolfold.find_runs(text, final=True, text_starts={start})
        self.assertEqual(text[runs[0][0]:runs[0][1]], "⏺ Bash ls\n⏺ Read a.py")
        self.assertTrue(runs[0][4])


class TestFoldRuns(unittest.TestCase):

    def setUp(self):
        self._sublime = toolfold.sublime
        toolfold.sublime = fake_sublime

    def tearDown(self):
        toolfold.sublime = self._sublime

    def test_streamed_turn(self):
        view = FakeView("❯ hi\n\n")
        view.append("Let me look.\n")
        view.append("⏺ Bash ls\n")
        view.append("⏺ Read a.py\n")
        # Still open: no header yet
        self.assertNotIn("tool calls", view.text)
        view.append("Done")
        view.append(", all good.\n")
        self.assertIn("Let me look.\n⏺ 2 tool calls\n⏺ Bash ls\n⏺ Read a.py\nDone, all good.\n", view.text)
        self.assertEqual(view.folded_text(), ["\n⏺ Bash ls\n⏺ Read a.py"])
        header = view.get_regions(toolfold.HEADER_KEY)
        self.assertEqual([view.substr(r) for r in header], ["⏺ 2 tool calls"])
        # More text later does not add a second header
        view.append("More text.\n")
        self.assertEqual(view.text.count("tool calls"), 1)

    def test_last_run_folded_when_turn_ends(self):
        view = FakeView("Text\n")
        view.append("⏺ Bash ls\n⏺ Bash x\n\n    second\n")
        self.assertEqual(view.folds, [])
        toolfold.fold_runs(view, None, view.size(), final=True)
        self.assertEqual(view.folded_text(), ["\n⏺ Bash ls\n⏺ Bash x\n\n    second"])
        # The next turn rescans the last line of the folded run: no new header
        view.append("❯ next\n\n⏺ Bash a\n⏺ Bash b\nok\n")
        self.assertEqual(view.text.count("⏺ 2 tool calls"), 2)
        self.assertTrue(view.text.startswith("Text\n⏺ 2 tool calls\n⏺ Bash ls"))

    def test_single_call_left_alone(self):
        view = FakeView()
        view.append("Text\n⏺ Bash ls\nDone\n")
        self.assertNotIn("tool call", view.text)
        self.assertEqual(view.folds, [])

    def test_single_multiline_call_folded(self):
        view = FakeView()
        view.append("Text\n⏺ Bash python3 -c '\n\n    import os\n    '\n\nDone\n")
        self.assertNotIn("tool call", view.text)
        self.assertEqual(view.folded_text(), ["\n\n    import os\n    '"])

    def test_call_folded_before_run_ends(self):
        view = FakeView()
        view.append("Text\n⏺ Bash a\n\n    more\n")
        # Still open: more lines of the command may follow
        self.assertEqual(view.folds, [])
        view.append("⏺ Read a.py\n")
        self.assertEqual(view.folded_text(), ["\n\n    more"])
        view.append("⏺ Bash b\n")
        self.assertEqual(view.folded_text(), ["\n\n    more"])
        view.append("Done\n")
        self.assertEqual(view.folded_text(), ["\n⏺ Bash a\n\n    more\n⏺ Read a.py\n⏺ Bash b"])

    def test_blank_lines_between_calls_dropped(self):
        view = FakeView("Text\n")
        view.append("⏺ Read a.py#L25-L27\n")
        view.append("\n")
        view.append("⏺ Edit a.py#L23\n\n    @@ -23 +23 @@\n    -x\n    +y\n")
        self.assertIn("⏺ Read a.py#L25-L27\n⏺ Edit a.py#L23\n\n    @@", view.text)
        self.assertEqual(view.folded_text(), [])
        view.append("\n⏺ Bash ls\n\nDone\n")
        # The blank before the text after the run stays
        self.assertIn("    +y\n⏺ Bash ls\n\nDone\n", view.text)
        self.assertEqual(view.folded_text(), ["\n⏺ Read a.py#L25-L27\n⏺ Edit a.py#L23\n\n    @@ -23 +23 @@\n    -x\n"
                                              "    +y\n⏺ Bash ls"])
        header = view.text.index("⏺ 3")
        self.assertTrue(toolfold.toggle_at(view, header))
        self.assertEqual(view.folded_text(), ["\n\n    @@ -23 +23 @@\n    -x\n    +y"])

    def test_click_toggles_call(self):
        view = FakeView()
        view.append("Text\n⏺ Bash a\n\n    more\n⏺ Read a.py\nDone\n")
        header = view.text.index("⏺ 2")
        # Opening the run shows its calls folded one by one
        self.assertTrue(toolfold.toggle_at(view, header))
        self.assertEqual(view.folded_text(), ["\n\n    more"])
        call = view.text.index("⏺ Bash a")
        self.assertTrue(toolfold.toggle_at(view, call))
        self.assertEqual(view.folds, [])
        self.assertTrue(toolfold.toggle_at(view, call))
        self.assertEqual(view.folded_text(), ["\n\n    more"])
        # The ⏺ of a one-line call folds the run
        self.assertTrue(toolfold.toggle_at(view, view.text.index("⏺ Read")))
        self.assertEqual(view.folded_text(), ["\n⏺ Bash a\n\n    more\n⏺ Read a.py"])

    def test_click_toggles(self):
        view = FakeView()
        view.append("Text\n⏺ Bash ls\n⏺ Read a.py\nDone\n")
        header = view.text.index("⏺ 2")
        self.assertTrue(toolfold.toggle_at(view, header + 3))
        self.assertEqual(view.folds, [])
        # Open: a click on the ⏺ of a call folds the run again
        self.assertTrue(toolfold.toggle_at(view, view.text.index("⏺ Read")))
        self.assertEqual(view.folded_text(), ["\n⏺ Bash ls\n⏺ Read a.py"])
        self.assertTrue(toolfold.toggle_at(view, header))
        self.assertEqual(view.folds, [])
        # Elsewhere on a call's line, or on plain text: not handled
        self.assertFalse(toolfold.toggle_at(view, view.text.index("a.py")))
        self.assertFalse(toolfold.toggle_at(view, 1))
        self.assertFalse(toolfold.toggle_at(view, None))

    def test_reply_list_after_calls_not_folded(self):
        # The list is indented by the formatter like a call's body; the text-start mark keeps it out
        view, formatter = FakeView("❯ hi\n\n"), MarkdownFormatter()
        for chunk in ("Let me look.\n", "⏺ Bash git commit -m 'x\n\n    - fix x\n    '\n", "⏺ Read a.py\n",
                      "\n", "- Found X\n", "- Found Y\n"):
            view.append_formatted(formatter, chunk)
        toolfold.fold_runs(view, None, view.size(), final=True)
        # The command's own "- fix x" is not re-indented, and it stays folded with its call
        self.assertIn("⏺ Bash git commit -m 'x\n\n    - fix x\n    '\n⏺ Read a.py\n\n    - Found X\n", view.text)
        self.assertEqual(view.folded_text(), ["\n⏺ Bash git commit -m 'x\n\n    - fix x\n    '\n⏺ Read a.py"])
        # Clicks and a restart find the same end
        header = view.text.index("⏺ 2")
        self.assertTrue(toolfold.toggle_at(view, header))
        self.assertTrue(toolfold.toggle_at(view, header))
        self.assertEqual(view.folded_text(), ["\n⏺ Bash git commit -m 'x\n\n    - fix x\n    '\n⏺ Read a.py"])
        view.folds = []
        toolfold.restore(view)
        self.assertEqual(view.folded_text(), ["\n⏺ Bash git commit -m 'x\n\n    - fix x\n    '\n⏺ Read a.py"])

    def test_restore_after_restart(self):
        view = FakeView("Text\n⏺ 2 tool calls\n⏺ Bash ls\n⏺ Bash x\nDone\n")
        toolfold.restore(view)
        self.assertEqual(view.folded_text(), ["\n⏺ Bash ls\n⏺ Bash x"])
        self.assertEqual(len(view.get_regions(toolfold.HEADER_KEY)), 1)

    def test_restore_folds_calls(self):
        view = FakeView("Text\n⏺ Bash a\n\n    more\nDone\n")
        toolfold.restore(view)
        self.assertEqual(view.folded_text(), ["\n\n    more"])
        view = FakeView("Text\n⏺ Bash a\n\n    more\nDone\n")
        toolfold.restore(view, fold_calls=False)
        self.assertEqual(view.folds, [])


if __name__ == "__main__":
    unittest.main()
