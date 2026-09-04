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


if __name__ == "__main__":
    unittest.main()
