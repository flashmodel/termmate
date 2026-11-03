import sys
import unittest
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatprocessor import ClaudeMessageProcessor
from chatview.usage import (format_reset, label, level_color, live_windows, model_name, parse_windows, popup_html,
                            tag_text)

NOW = 1_000_000.0
INFO = {"unifiedWindows": {
    "five_hour": {"utilization": 0.17, "resetsAt": NOW + 5100},
    "seven_day": {"utilization": 0.08, "resetsAt": NOW + 3 * 86400},
    "seven_day_fable": {"utilization": 0.5, "resetsAt": NOW + 3 * 86400},
}}


class TestUsage(unittest.TestCase):
    def test_parse_windows(self):
        w = parse_windows(INFO)
        self.assertEqual(w["five_hour"], (0.17, NOW + 5100))
        self.assertEqual(set(w), {"five_hour", "seven_day", "seven_day_fable"})

    def test_parse_skips_bad_entries(self):
        info = {"unifiedWindows": {"a": {"utilization": "x"}, "b": 3, "c": {"utilization": 2}, "d": {}}}
        self.assertEqual(parse_windows(info), {"c": (1.0, None)})
        self.assertEqual(parse_windows(None), {})
        self.assertEqual(parse_windows({}), {})

    def test_label(self):
        self.assertEqual(label("five_hour"), "Session limit")
        self.assertEqual(label("seven_day"), "Weekly · all models")
        self.assertEqual(label("seven_day_fable"), "Weekly · Fable")
        self.assertEqual(label("seven_day_claude_design"), "Weekly · Claude Design")
        self.assertEqual(label("overage"), "Overage")

    def test_format_reset(self):
        self.assertEqual(format_reset(NOW + 5100, NOW), "Resets in 1 hr 25 min")
        self.assertEqual(format_reset(NOW + 600, NOW), "Resets in 10 min")
        self.assertEqual(format_reset(NOW + 10, NOW), "Resets in 1 min")
        self.assertEqual(format_reset(NOW - 1, NOW), "Resets now")
        self.assertEqual(format_reset(None, NOW), "")
        self.assertTrue(format_reset(NOW + 3 * 86400, NOW).startswith("Resets "))
        self.assertNotIn(" in ", format_reset(NOW + 3 * 86400, NOW))

    def test_tag_text(self):
        w = parse_windows(INFO)
        self.assertEqual(tag_text(w, NOW), "17% · 8% · 50%")
        self.assertEqual(tag_text({}, NOW), "")
        only_session = {"five_hour": (0.5, NOW + 10)}
        self.assertEqual(tag_text(only_session, NOW), "50%")
        # Other weekly windows in key order, numbers only; non-weekly extras stay out of the tag
        w["seven_day_overage_included"] = (0.27, NOW + 10)
        w["overage"] = (0.1, NOW + 10)
        self.assertEqual(tag_text(w, NOW), "17% · 8% · 50% · 27%")

    def test_model_name(self):
        self.assertEqual(model_name("seven_day_fable"), "Fable")
        self.assertEqual(model_name("seven_day"), "")
        self.assertEqual(model_name("seven_day_"), "")
        self.assertEqual(model_name("five_hour"), "")

    def test_stale_windows_dropped(self):
        w = {"five_hour": (0.9, NOW - 1), "seven_day": (0.1, NOW + 10)}
        self.assertEqual(set(live_windows(w, NOW)), {"seven_day"})
        self.assertEqual(tag_text(w, NOW), "10%")

    def test_popup_html(self):
        out = popup_html(parse_windows(INFO), NOW)
        self.assertLess(out.index("Session limit"), out.index("Weekly · all models"))
        self.assertLess(out.index("Weekly · all models"), out.index("Weekly · Fable"))
        self.assertIn("17%", out)
        self.assertIn("Resets in 1 hr 25 min", out)
        self.assertEqual(popup_html({}, NOW), "")

    def test_level_colors(self):
        self.assertIsNone(level_color(0.594))
        self.assertEqual(level_color(0.6), "var(--yellowish)")
        self.assertEqual(level_color(0.69), "var(--yellowish)")
        self.assertEqual(level_color(0.70), "var(--orangish)")
        self.assertEqual(level_color(0.894), "var(--orangish)")
        self.assertEqual(level_color(0.9), "var(--redish)")
        w = {"five_hour": (0.95, None), "seven_day": (0.75, None), "seven_day_fable": (0.2, None)}
        self.assertEqual(tag_text(w), '<span style="color:var(--redish)">95%</span> · '
                                      '<span style="color:var(--orangish)">75%</span> · 20%')
        out = popup_html(w)
        self.assertIn('<span style="color:var(--redish)">95%</span>', out)
        self.assertIn('<span style="color:var(--orangish)">█', out)
        self.assertIn('<span style="color:var(--accent)">█', out)

    def test_popup_bars_match_widest_line(self):
        out = popup_html(parse_windows(INFO), NOW)
        widest = max(len(label(k)) + len(format_reset(r, NOW)) + len(f"{round(u * 100)}%") + 4
                     for k, (u, r) in parse_windows(INFO).items())
        bars = [part.count("█") for part in out.split("<br>")[3:]]
        self.assertEqual(bars, [widest] * 3)


class TestRateLimitEvent(unittest.TestCase):
    def test_claude_processor_passes_windows_to_the_bar(self):
        from genfoundry.base_agent import Message
        session = MagicMock()
        processor = ClaudeMessageProcessor(session)
        # The bar is updated on Sublime's main thread: run the scheduled call
        with patch("chatview.chatprocessor.sublime.set_timeout", lambda fn, delay=0: fn()):
            processor.handle_message(Message("rate_limit_event", None, None, rate_limit_info=INFO))
        session.model_phantom.set_usage.assert_called_once_with(parse_windows(INFO))

    def test_event_without_windows_is_ignored(self):
        from genfoundry.base_agent import Message
        session = MagicMock()
        ClaudeMessageProcessor(session).handle_message(
            Message("rate_limit_event", None, None, rate_limit_info={"status": "allowed"}))
        session.model_phantom.set_usage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
