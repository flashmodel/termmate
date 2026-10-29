import sys
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("sublime", MagicMock())

from chatview import inputpane
from chatview.inputpane import add_row_below, remove_row_below, resize_row_below


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

    def __eq__(self, other):
        return (self.a, self.b) == (other.a, other.b)

    def __repr__(self):
        return f"Region({self.a}, {self.b})"


class Selection(list):
    def add(self, region):
        self.append(region)

    def add_all(self, regions):
        self.extend(regions)


class FakePane:
    """Just enough of sublime.View over a string for the header-line helpers."""

    def __init__(self, text, sel=()):
        self.text = text
        self._sel = Selection(Region(*s) for s in sel)

    def size(self):
        return len(self.text)

    def substr(self, x):
        if isinstance(x, int):
            return self.text[x:x + 1]
        return self.text[x.begin():x.end()]

    def sel(self):
        return self._sel

    def replace(self, edit, region, text):
        self.text = self.text[:region.begin()] + text + self.text[region.end():]

    def show(self, point):
        pass


class TestHeaderLine(unittest.TestCase):

    def setUp(self):
        # Whatever sublime stub another test module installed, use a real-enough Region here
        self.real_sublime = inputpane.sublime
        inputpane.sublime = MagicMock(Region=Region)

    def tearDown(self):
        inputpane.sublime = self.real_sublime

    def test_text_skips_header(self):
        self.assertEqual(inputpane.get_text(FakePane("\nhello")), "hello")
        self.assertEqual(inputpane.get_text(FakePane("\n")), "")
        # A pane from before the header line existed
        self.assertEqual(inputpane.get_text(FakePane("hello")), "hello")

    def test_replace_keeps_header(self):
        pane = FakePane("\nold text", [(9, 9)])
        inputpane.replace_text(pane, None, "new")
        self.assertEqual(pane.text, "\nnew")
        self.assertEqual(list(pane.sel()), [Region(4)])

    def test_clamp_selection(self):
        pane = FakePane("\nabc", [(0, 0), (0, 4), (2, 3)])
        inputpane.clamp_selection(pane)
        self.assertEqual(list(pane.sel()), [Region(1), Region(1, 4), Region(2, 3)])

    def test_backspace_at_text_start_blocked(self):
        self.assertTrue(inputpane.blocks_header_delete(FakePane("\nabc", [(1, 1)]), "left_delete"))
        self.assertTrue(inputpane.blocks_header_delete(FakePane("\nabc", [(1, 1)]), "delete_word_backward"))
        self.assertFalse(inputpane.blocks_header_delete(FakePane("\nabc", [(2, 2)]), "left_delete"))
        # Deleting a selection that starts at the text start is fine
        self.assertFalse(inputpane.blocks_header_delete(FakePane("\nabc", [(1, 4)]), "left_delete"))
        self.assertFalse(inputpane.blocks_header_delete(FakePane("\nabc", [(1, 1)]), "right_delete"))

SINGLE = {"cols": [0.0, 1.0], "rows": [0.0, 1.0], "cells": [[0, 0, 1, 1]]}
# Split Chat Window: chat on the left, files on the right
TWO_COLUMNS = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 1.0], "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]}


class TestAddRowBelow(unittest.TestCase):

    def test_single_group(self):
        layout = add_row_below(SINGLE, 0, 0.25)
        self.assertEqual(layout["rows"], [0.0, 0.75, 1.0])
        self.assertEqual(layout["cells"], [[0, 0, 1, 1], [0, 1, 1, 2]])

    def test_chat_column_only(self):
        # The files column keeps its full height; the pane is appended as the last group
        layout = add_row_below(TWO_COLUMNS, 0, 0.25)
        self.assertEqual(layout["cols"], [0.0, 0.5, 1.0])
        self.assertEqual(layout["rows"], [0.0, 0.75, 1.0])
        self.assertEqual(layout["cells"], [[0, 0, 1, 1], [1, 0, 2, 2], [0, 1, 1, 2]])

    def test_chat_on_the_right(self):
        layout = add_row_below(TWO_COLUMNS, 1, 0.5)
        self.assertEqual(layout["rows"], [0.0, 0.5, 1.0])
        self.assertEqual(layout["cells"], [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]])

    def test_split_between_existing_rows(self):
        # Chat spans two rows of the other column; the new boundary lands between them
        layout = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 0.5, 1.0],
                  "cells": [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]]}
        new = add_row_below(layout, 0, 0.2)
        self.assertEqual(new["rows"], [0.0, 0.5, 0.8, 1.0])
        self.assertEqual(new["cells"], [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 3], [0, 2, 1, 3]])

    def test_reuses_an_existing_boundary(self):
        layout = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 0.5, 1.0],
                  "cells": [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]]}
        new = add_row_below(layout, 0, 0.5)
        self.assertEqual(new["rows"], [0.0, 0.5, 1.0])
        self.assertEqual(new["cells"], [[0, 0, 1, 1], [1, 0, 2, 1], [1, 1, 2, 2], [0, 1, 1, 2]])

    def test_input_not_modified(self):
        before = repr(TWO_COLUMNS)
        add_row_below(TWO_COLUMNS, 0, 0.25)
        self.assertEqual(repr(TWO_COLUMNS), before)


class TestRemoveRowBelow(unittest.TestCase):

    def test_round_trip(self):
        for layout, group in ((SINGLE, 0), (TWO_COLUMNS, 0), (TWO_COLUMNS, 1)):
            with self.subTest(group=group, layout=layout):
                added = add_row_below(layout, group, 0.25)
                self.assertEqual(remove_row_below(added, len(added["cells"]) - 1), layout)

    def test_round_trip_keeps_shared_boundary(self):
        layout = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 0.5, 1.0],
                  "cells": [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]]}
        added = add_row_below(layout, 0, 0.5)
        self.assertEqual(remove_row_below(added, 3), layout)

    def test_nothing_above(self):
        self.assertIsNone(remove_row_below(TWO_COLUMNS, 1))


class TestResizeRowBelow(unittest.TestCase):

    def test_window_grew(self):
        # The pane was 100 px and is 200 px after the window doubled: its share halves
        layout = add_row_below(SINGLE, 0, 0.2)
        new = resize_row_below(layout, 1, 200, 100)
        self.assertAlmostEqual(new["rows"][1], 0.9)
        self.assertEqual(new["cells"], layout["cells"])
        self.assertEqual(layout["rows"], [0.0, 0.8, 1.0])  # input not modified

    def test_exact_with_chat_height(self):
        # 1000 px window, 30 px tab bars: chat 800 - 30, pane 200 - 30; for 100 px the pane's cell is 130 px
        layout = add_row_below(SINGLE, 0, 0.2)
        new = resize_row_below(layout, 1, 170, 100, 770)
        self.assertAlmostEqual(new["rows"][1], 0.87)
        # Equal cells give no px per row: falls back to scaling the share
        even = add_row_below(SINGLE, 0, 0.5)
        self.assertAlmostEqual(resize_row_below(even, 1, 200, 100, 200)["rows"][1], 0.75)

    def test_window_shrank_caps_at_max_fraction(self):
        layout = add_row_below(SINGLE, 0, 0.2)
        new = resize_row_below(layout, 1, 50, 200)
        self.assertAlmostEqual(new["rows"][1], 1.0 - inputpane.INPUT_PANE_MAX_FRACTION)

    def test_chat_column_only(self):
        layout = add_row_below(TWO_COLUMNS, 0, 0.2)
        new = resize_row_below(layout, 2, 200, 100)
        self.assertAlmostEqual(new["rows"][1], 0.9)
        self.assertEqual(new["cells"], layout["cells"])

    def test_stays_between_neighbouring_rows(self):
        layout = {"cols": [0.0, 0.5, 1.0], "rows": [0.0, 0.5, 1.0],
                  "cells": [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]]}
        added = add_row_below(layout, 0, 0.2)  # rows 0, 0.5, 0.8, 1
        new = resize_row_below(added, 3, 100, 300)
        self.assertAlmostEqual(new["rows"][2], 1.0 - inputpane.INPUT_PANE_MAX_FRACTION)  # capped, below the 0.5 row

    def test_left_alone(self):
        layout = add_row_below(SINGLE, 0, 0.2)
        self.assertIsNone(resize_row_below(layout, 1, 100, 100))
        self.assertIsNone(resize_row_below(layout, 1, 0, 100))
        self.assertIsNone(resize_row_below(TWO_COLUMNS, 1, 100, 50))  # nothing above
        # The pane's top edge is also the other column's boundary
        shared = add_row_below({"cols": [0.0, 0.5, 1.0], "rows": [0.0, 0.5, 1.0],
                                "cells": [[0, 0, 1, 2], [1, 0, 2, 1], [1, 1, 2, 2]]}, 0, 0.5)
        self.assertIsNone(resize_row_below(shared, 3, 200, 100))


if __name__ == "__main__":
    unittest.main()
