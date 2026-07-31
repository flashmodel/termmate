"""
Test MarkdownFormatter.format_table's column-width capping.

Regression test: a table cell containing one long unbroken run of text (a
full path, a stack-trace fragment, ...) used to make the separator row's
"-" * width fill grow to match that cell's width -- rendered in the chat
view as one very long horizontal-rule-looking line (observed: 485 chars).
"""

import sys
import types
import unittest
import os

# chatview/utils.py imports `sublime`, which only exists inside the Sublime
# Text runtime. Stub it so this file is importable under plain pytest --
# MarkdownFormatter itself never calls into the sublime API.
if "sublime" not in sys.modules:
    sys.modules["sublime"] = types.ModuleType("sublime")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from chatview.utils import MarkdownFormatter


class TestMarkdownTableWidthCap(unittest.TestCase):

    def setUp(self):
        self.mf = MarkdownFormatter()

    def test_long_cell_does_not_blow_out_separator_width(self):
        long_val = "x" * 485
        table = [
            "| name | value |",
            "| --- | --- |",
            f"| a | {long_val} |",
            "| b | short |",
        ]
        out = self.mf.format_table(table)

        self.assertEqual(len(out), 4)
        for line in out:
            self.assertLessEqual(
                len(line), MarkdownFormatter.MAX_COL_WIDTH * 2 + 20,
                f"line exceeded expected cap: {len(line)} chars",
            )
        # The separator row must not contain a run of dashes anywhere near 485 long.
        separator_line = out[1]
        self.assertNotIn("-" * 100, separator_line)

    def test_truncated_cell_ends_with_ellipsis(self):
        long_val = "y" * 200
        table = [
            "| name | value |",
            "| --- | --- |",
            f"| a | {long_val} |",
        ]
        out = self.mf.format_table(table)
        value_row = out[2]
        self.assertIn("…", value_row)

    def test_short_table_unaffected(self):
        table = [
            "| name | value |",
            "| --- | --- |",
            "| a | 1 |",
            "| b | 2 |",
        ]
        out = self.mf.format_table(table)
        for line in out:
            self.assertNotIn("…", line)


if __name__ == "__main__":
    unittest.main()
