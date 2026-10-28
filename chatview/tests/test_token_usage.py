import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Sublime Text supplies these modules at plugin runtime. Mocks keep this
# test runnable with ordinary Python.
sys.modules.setdefault("sublime", MagicMock())
sys.modules.setdefault("sublime_plugin", MagicMock())
import sublime

from chatview.utils import format_token_count
from chatview.chatpanel import TokenSummaryPhantom
from chatview.chatprocessor import (
    BaseChatMessageProcessor,
    ClaudeMessageProcessor,
    CodexMessageProcessor,
)
from genfoundry.base_agent import Message, MessageType


class TestFormatTokenCount(unittest.TestCase):
    """Test format_token_count formatting across scales and edge cases."""

    def test_zero_and_none(self):
        self.assertEqual(format_token_count(None), "0")
        self.assertEqual(format_token_count(0), "0")
        self.assertEqual(format_token_count("0"), "0")

    def test_sub_thousand(self):
        self.assertEqual(format_token_count(5), "5")
        self.assertEqual(format_token_count(217), "217")
        self.assertEqual(format_token_count(999), "999")

    def test_thousands_compact(self):
        self.assertEqual(format_token_count(1000), "1k")
        self.assertEqual(format_token_count(1500), "1.5k")
        self.assertEqual(format_token_count(9900), "9.9k")
        self.assertEqual(format_token_count(10000), "10k")
        self.assertEqual(format_token_count(34597), "34.6k")
        self.assertEqual(format_token_count(34814), "34.8k")
        self.assertEqual(format_token_count(258400), "258k")

    def test_millions(self):
        self.assertEqual(format_token_count(999500), "1M")
        self.assertEqual(format_token_count(1000000), "1M")
        self.assertEqual(format_token_count(1200000), "1.2M")

    def test_invalid_fallback(self):
        self.assertEqual(format_token_count("invalid"), "invalid")


class TestTokenSummaryPhantomRendering(unittest.TestCase):
    """Test HTML rendering of TokenSummaryPhantom."""

    def setUp(self):
        self.view = MagicMock()
        self.phantom = TokenSummaryPhantom(self.view, lambda: 100)

    def test_render_codex_usage(self):
        usage = {
            "last": {
                "totalTokens": 34814,
                "inputTokens": 34597,
                "cachedInputTokens": 31488,
                "outputTokens": 217,
                "reasoningOutputTokens": 0,
            },
            "total": {
                "totalTokens": 338644,
                "inputTokens": 334209,
                "cachedInputTokens": 276992,
                "outputTokens": 4435,
            },
            "modelContextWindow": 258400,
        }
        html = self.phantom._render_html(usage)

        self.assertIn("34.8k", html)
        self.assertIn(
            "token</span><span class=\"dim\">(in:3.1k, out:217, 91% cached)</span>",
            html,
        )
        self.assertIn("in:3.1k", html)
        self.assertIn("out:217", html)
        self.assertIn("91% cached", html)
        self.assertIn("(in:3.1k, out:217, 91% cached)", html)
        self.assertIn("cached)</span> <span class=\"item\">Context:", html)
        self.assertIn("Context:", html)
        self.assertIn("13.4%", html)
        self.assertIn("(258k)", html)
        self.assertIn("<span class=\"sep\">·</span><span class=\"item\">Total:339k</span>", html)
        self.assertIn("padding: 0 0.15em;", html)

    def test_render_codex_cold_start_with_cache_write(self):
        """Verify cold start with cacheWriteInputTokens does not falsely report cached %."""
        usage = {
            "last": {
                "totalTokens": 10172,
                "inputTokens": 10030,
                "cachedInputTokens": 0,
                "cacheWriteInputTokens": 10027,
                "outputTokens": 142,
                "reasoningOutputTokens": 35,
            },
            "total": {
                "totalTokens": 10172,
                "inputTokens": 10030,
                "cachedInputTokens": 0,
                "cacheWriteInputTokens": 10027,
                "outputTokens": 142,
                "reasoningOutputTokens": 35,
            },
            "modelContextWindow": 258400,
        }
        html = self.phantom._render_html(usage)

        self.assertIn("10.2k", html)
        self.assertIn("(in:10k, out:142, thinking:35)", html)
        self.assertNotIn("cached", html)
        self.assertIn("Context:<span class=\"highlight\">3.9%</span>", html)

    def test_render_antigravity_usage(self):
        # Normalized Antigravity metrics: inputTokens is total prompt tokens (uncached + cached)
        usage = {
            "last": {
                "inputTokens": 221209,
                "outputTokens": 3692,
                "cachedInputTokens": 218110,
                "totalTokens": 224901,
                "reasoningOutputTokens": 1918,
            },
            "total": {
                "inputTokens": 136520208,
                "outputTokens": 550875,
                "cachedInputTokens": 130547261,
                "totalTokens": 137071083,
                "reasoningOutputTokens": 364558,
            },
        }
        html = self.phantom._render_html(usage)

        self.assertIn("225k", html)
        self.assertIn(
            "token</span><span class=\"dim\">(in:3.1k, out:3.7k, thinking:1.9k, 99% cached)</span>",
            html,
        )
        self.assertIn("in:3.1k", html)
        self.assertIn("out:3.7k", html)
        self.assertIn("thinking:1.9k", html)
        self.assertIn("99% cached", html)
        self.assertIn("(in:3.1k, out:3.7k, thinking:1.9k, 99% cached)", html)
        self.assertIn("Total:137.1M", html)

    def test_render_usage_without_cache(self):
        usage = {
            "last": {
                "inputTokens": 1500,
                "outputTokens": 200,
                "cachedInputTokens": 0,
            },
        }
        html = self.phantom._render_html(usage)
        self.assertIn("(in:1.5k, out:200)", html)
        self.assertNotIn("cached", html)

    def test_update_empty_usage_clears_phantom(self):
        self.phantom.show({})
        self.view.erase_phantoms.assert_not_called()
        self.assertEqual(self.phantom.token_usage, {})

    def test_clear_resets_state(self):
        self.phantom.token_usage = {"last": {"totalTokens": 100}}
        self.phantom.clear()
        self.assertIsNone(self.phantom.token_usage)


class TestChatProcessorTokenUsageDispatch(unittest.TestCase):
    """Test message processor handling for token_usage and stop/result."""

    def test_base_processor_dispatches_token_usage(self):
        session = MagicMock()
        processor = BaseChatMessageProcessor(session)

        msg = Message("token_usage", content={"last": {"inputTokens": 500}})
        processor.handle_message(msg)

        session.set_token_usage.assert_called_once_with({"last": {"inputTokens": 500}})

    def test_codex_processor_calls_stop_loading_on_stop(self):
        session = MagicMock()
        processor = CodexMessageProcessor(session)
        processor.append_content = MagicMock()

        processor.handle_message(Message("stop"))

        session.stop_loading.assert_called_once()

    def test_claude_processor_calls_stop_loading_on_result(self):
        session = MagicMock()
        processor = ClaudeMessageProcessor(session)
        processor.append_content = MagicMock()

        processor.handle_message(Message("result"))

        session.stop_loading.assert_called_once()

    def test_claude_processor_does_not_append_extra_newline_on_result(self):
        session = MagicMock()
        processor = ClaudeMessageProcessor(session)
        processor.append_content = MagicMock()

        processor.handle_message(Message("result"))

        processor.append_content.assert_called_once_with("", flush=True)

    def test_codex_processor_does_not_append_extra_newline_on_stop(self):
        session = MagicMock()
        processor = CodexMessageProcessor(session)
        processor.append_content = MagicMock()

        processor.handle_message(Message("stop"))

        processor.append_content.assert_called_once_with("", flush=True)

    def test_antigravity_processor_does_not_append_extra_newline_on_stop(self):
        session = MagicMock()
        from chatview.chatprocessor import AntigravityMessageProcessor
        processor = AntigravityMessageProcessor(session)
        processor.append_content = MagicMock()

        processor.handle_message(Message("stop"))

        processor.append_content.assert_called_once_with("", flush=True)

    def test_ensure_trailing_newline_when_missing(self):
        session = MagicMock()
        processor = BaseChatMessageProcessor(session)
        processor.append_content("Hello world")
        self.assertFalse(processor._ends_with_newline)

        processor.ensure_trailing_newline()
        self.assertTrue(processor._ends_with_newline)
        session.append_markdown.assert_called_with("\n", flush=False)

    def test_ensure_trailing_newline_when_already_present(self):
        session = MagicMock()
        processor = BaseChatMessageProcessor(session)
        processor.append_content("Hello world\n")
        self.assertTrue(processor._ends_with_newline)

        session.append_markdown.reset_mock()
        processor.ensure_trailing_newline()
        session.append_markdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
