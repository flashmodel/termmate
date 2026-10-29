import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.claude_agent import ClaudeCodeAgent
from genfoundry.base_agent import AgentOptions


class TestClaudeThinkLevel(unittest.IsolatedAsyncioTestCase):

    async def test_set_think_level_tokens(self):
        opts = AgentOptions(cli_path="/usr/bin/true")
        agent = ClaudeCodeAgent(options=opts)
        agent.is_connected = True
        agent._send_control_request = AsyncMock()

        # 1. Medium -> 8192
        await agent.set_think_level("medium")
        agent._send_control_request.assert_called_with({
            "subtype": "set_max_thinking_tokens",
            "max_thinking_tokens": 8192,
        })
        self.assertEqual(agent.options.think_level, "medium")

        # 2. None / Off -> 0
        await agent.set_think_level("none")
        agent._send_control_request.assert_called_with({
            "subtype": "set_max_thinking_tokens",
            "max_thinking_tokens": 0,
        })

        # 3. Adaptive -> None
        await agent.set_think_level("adaptive")
        agent._send_control_request.assert_called_with({
            "subtype": "set_max_thinking_tokens",
            "max_thinking_tokens": None,
        })

    async def test_set_think_level_disconnected(self):
        opts = AgentOptions(cli_path="/usr/bin/true")
        agent = ClaudeCodeAgent(options=opts)
        agent.is_connected = False

        # Should safely update options without raising RuntimeError
        await agent.set_think_level("medium")
        self.assertEqual(agent.options.think_level, "medium")

    def _create_fake_proc(self):
        fake_proc = AsyncMock()
        fake_proc.stdin = MagicMock()
        fake_proc.stdin.drain = AsyncMock()
        fake_proc.stdout = AsyncMock()
        fake_proc.stderr = AsyncMock()
        fake_proc.returncode = None
        return fake_proc

    async def _connect_with_mocks(self, agent):
        fake_proc = self._create_fake_proc()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec, \
             patch.object(agent, "_send_initialize_request", new_callable=AsyncMock), \
             patch.object(agent, "_read_messages", new_callable=AsyncMock), \
             patch.object(agent, "_read_stderr", new_callable=AsyncMock):
            await agent.connect()
            return mock_exec.call_args[0]

    async def test_connect_cli_effort_arg(self):
        opts = AgentOptions(cli_path="/usr/bin/true", think_level="high")
        agent = ClaudeCodeAgent(options=opts)
        agent._send_control_request = AsyncMock()

        args = await self._connect_with_mocks(agent)
        self.assertIn("--effort", args)
        idx = args.index("--effort")
        self.assertEqual(args[idx + 1], "high")
        agent._send_control_request.assert_called_with({
            "subtype": "set_max_thinking_tokens",
            "max_thinking_tokens": 16384,
        })

    async def test_connect_cli_no_effort_arg(self):
        # Verify cases where --effort CLI arg should NOT be passed
        cases = [
            ("auto", None),
            ("none", 0),
            ("off", 0),
            (None, False),
        ]
        for level, expected_tokens in cases:
            with self.subTest(level=level):
                opts = AgentOptions(cli_path="/usr/bin/true", think_level=level)
                agent = ClaudeCodeAgent(options=opts)
                agent._send_control_request = AsyncMock()

                args = await self._connect_with_mocks(agent)
                self.assertNotIn("--effort", args)
                if expected_tokens is False:
                    agent._send_control_request.assert_not_called()
                else:
                    agent._send_control_request.assert_called_with({
                        "subtype": "set_max_thinking_tokens",
                        "max_thinking_tokens": expected_tokens,
                    })


class TestClaudeAgentTokenUsageExtraction(unittest.TestCase):
    """Test ClaudeCodeAgent token usage extraction and parsing."""

    def setUp(self):
        with patch("genfoundry.claude_agent.find_claude_cli", return_value="/mock/claude"):
            self.agent = ClaudeCodeAgent(AgentOptions(cwd="/tmp"))

    def test_extract_token_usage_non_result(self):
        self.assertIsNone(self.agent._extract_token_usage({"type": "assistant"}))

    def test_extract_token_usage_empty_usage(self):
        self.assertIsNone(self.agent._extract_token_usage({"type": "result"}))

    def test_extract_token_usage_success(self):
        data = {
            "type": "result",
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 150,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 200,
            },
            "total_usage": {
                "input_tokens": 5000,
                "output_tokens": 600,
                "cache_read_input_tokens": 3000,
            },
            "cost_usd": 0.0123,
        }
        res = self.agent._extract_token_usage(data)
        self.assertIsNotNone(res)
        # 1200 uncached + 800 read + 200 create = 2200 total input tokens
        self.assertEqual(res["last"]["inputTokens"], 2200)
        self.assertEqual(res["last"]["outputTokens"], 150)
        self.assertEqual(res["last"]["cachedInputTokens"], 800)
        self.assertEqual(res["last"]["totalTokens"], 2350)
        # 5000 uncached + 3000 read = 8000 total input tokens
        self.assertEqual(res["total"]["inputTokens"], 8000)
        self.assertEqual(res["total"]["outputTokens"], 600)
        self.assertEqual(res["total"]["cachedInputTokens"], 3000)
        self.assertEqual(res["total"]["totalTokens"], 8600)
        self.assertEqual(res["costUsd"], 0.0123)

    def test_extract_token_usage_input_greater_than_cached(self):
        """Verify that uncached tokens > cached tokens are still correctly summed."""
        data = {
            "type": "result",
            "usage": {
                "input_tokens": 5000,
                "output_tokens": 200,
                "cache_read_input_tokens": 1000,
            },
        }
        res = self.agent._extract_token_usage(data)
        self.assertIsNotNone(res)
        # 5000 + 1000 = 6000
        self.assertEqual(res["last"]["inputTokens"], 6000)
        self.assertEqual(res["last"]["cachedInputTokens"], 1000)
        self.assertEqual(res["last"]["totalTokens"], 6200)

    def test_extract_token_usage_with_model_usage(self):
        data = {
            "type": "result",
            "usage": {
                "input_tokens": 135,
                "output_tokens": 6475,
                "cache_read_input_tokens": 91038,
                "cache_creation_input_tokens": 34509,
            },
            "modelUsage": {
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 627,
                    "outputTokens": 2028,
                    "cacheReadInputTokens": 127540,
                    "cacheCreationInputTokens": 17163,
                    "contextWindow": 200000,
                },
                "claude-fable-5": {
                    "inputTokens": 135,
                    "outputTokens": 6475,
                    "cacheReadInputTokens": 91038,
                    "cacheCreationInputTokens": 34509,
                    "contextWindow": 1000000,
                },
            },
            "total_cost_usd": 1.462,
        }
        res = self.agent._extract_token_usage(data)
        self.assertIsNotNone(res)
        # last: 135 + 91038 + 34509 = 125682 in, 6475 out
        self.assertEqual(res["last"]["inputTokens"], 125682)
        self.assertEqual(res["last"]["outputTokens"], 6475)
        self.assertEqual(res["last"]["cachedInputTokens"], 91038)
        # total across haiku (145330 in, 2028 out) + fable (125682 in, 6475 out)
        # in: 145330 + 125682 = 271012, out: 2028 + 6475 = 8503
        self.assertEqual(res["total"]["inputTokens"], 271012)
        self.assertEqual(res["total"]["outputTokens"], 8503)
        self.assertEqual(res["total"]["cachedInputTokens"], 218578)
        self.assertEqual(res["total"]["totalTokens"], 279515)
        self.assertEqual(res["modelContextWindow"], 1000000)

    def test_extract_token_usage_cold_start_cache_write_not_counted_as_cached(self):
        """Verify that cache_creation_input_tokens (write) is not counted as cached (hit)."""
        data = {
            "type": "result",
            "usage": {
                "input_tokens": 3,
                "output_tokens": 376,
                "cache_creation_input_tokens": 40756,
                "cache_read_input_tokens": 0,
            },
        }
        res = self.agent._extract_token_usage(data)
        self.assertIsNotNone(res)
        # Total prompt is 3 + 40756 = 40759, but cache hit is 0
        self.assertEqual(res["last"]["inputTokens"], 40759)
        self.assertEqual(res["last"]["cachedInputTokens"], 0)
        self.assertEqual(res["last"]["outputTokens"], 376)
        self.assertEqual(res["last"]["totalTokens"], 41135)

    def test_thinking_tokens_captured_on_result(self):
        sys_msg = {
            "type": "system",
            "subtype": "thinking_tokens",
            "estimated_tokens": 4550,
        }
        self.assertIsNone(self.agent._parse_message(sys_msg))
        self.assertEqual(self.agent._last_thinking_tokens, 4550)

        data = {
            "type": "result",
            "result": "Done",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        res = self.agent._extract_token_usage(data)
        self.assertEqual(res["last"]["reasoningOutputTokens"], 4550)
        self.assertEqual(self.agent._last_thinking_tokens, 0)

    def test_thinking_tokens_multi_block_accumulation(self):
        """Verify thinking tokens across multiple thinking blocks in one turn accumulate."""
        # Block 1 (streaming deltas up to 4550)
        self.agent._parse_message({
            "type": "system",
            "subtype": "thinking_tokens",
            "estimated_tokens": 50,
            "estimated_tokens_delta": 50,
        })
        self.agent._parse_message({
            "type": "system",
            "subtype": "thinking_tokens",
            "estimated_tokens": 4550,
            "estimated_tokens_delta": 4500,
        })
        self.assertEqual(self.agent._last_thinking_tokens, 4550)

        # Block 2 (e.g. after tool execution, stream restarts)
        self.agent._parse_message({
            "type": "system",
            "subtype": "thinking_tokens",
            "estimated_tokens": 100,
            "estimated_tokens_delta": 100,
        })
        self.agent._parse_message({
            "type": "system",
            "subtype": "thinking_tokens",
            "estimated_tokens": 256,
            "estimated_tokens_delta": 156,
        })
        self.assertEqual(self.agent._last_thinking_tokens, 4550 + 256)

        data = {
            "type": "result",
            "result": "Done",
            "usage": {"input_tokens": 100, "output_tokens": 5000},
        }
        res = self.agent._extract_token_usage(data)
        self.assertEqual(res["last"]["reasoningOutputTokens"], 4806)
        self.assertEqual(self.agent._last_thinking_tokens, 0)

    def test_parse_message_queues_token_usage_on_result(self):
        data = {
            "type": "result",
            "result": "Success",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        msg = self.agent._parse_message(data)
        self.assertEqual(msg.type, "result")
        self.assertEqual(msg.content, "Success")

        self.assertFalse(self.agent._message_queue.empty())
        queued = self.agent._message_queue.get_nowait()
        self.assertEqual(queued.type, "token_usage")
        self.assertEqual(queued.content["last"]["inputTokens"], 100)


class TestSessionCwd(unittest.TestCase):

    def test_first_cwd_is_the_session_folder(self):
        # Later entries follow the agent's cd; the session lives under the folder it started in
        from genfoundry.claude_agent import _extract_session_cwd
        head = ('{"type":"summary","summary":"x"}\n'
                '{"type":"user","cwd":"/proj","message":{}}\n'
                '{"type":"assistant","cwd":"/proj/sub","message":{}}\n'
                '{"type":"user","cwd":"/proj/sub","mess')
        self.assertEqual(_extract_session_cwd(head), "/proj")
        self.assertIsNone(_extract_session_cwd('{"type":"summary"}\n{"cwd":"/cut'))


if __name__ == "__main__":
    unittest.main()
