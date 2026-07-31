"""
Test KimiAgent's persistent-process I/O and event parsing.

Uses the documented print-mode JSONL fixtures (plain OpenAI-chat-message
shape, per https://github.com/moonshotai/kimi-cli/blob/main/docs/en/
customization/print-mode.md) -- not live CLI output.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.kimi_agent import KimiAgent
from genfoundry.base_agent import AgentOptions, MessageType


class FakeProcess:
    """Simulates a persistent kimi subprocess: feed() queues NDJSON lines."""

    def __init__(self):
        self.returncode = None
        self._stdout_lines: list = []
        self._stdout_index = 0
        self.stdout = self
        self.stderr = self
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.written_lines = []
        self.stdin.write.side_effect = lambda b: self.written_lines.append(b.decode("utf-8"))

    def feed(self, data: dict) -> None:
        self._stdout_lines.append(json.dumps(data).encode() + b"\n")

    async def read(self, n: int) -> bytes:
        while self._stdout_index >= len(self._stdout_lines):
            await asyncio.sleep(0.01)
        line = self._stdout_lines[self._stdout_index]
        self._stdout_index += 1
        return line

    async def readline(self) -> bytes:
        await asyncio.sleep(10)
        return b""

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        pass


class TestKimiAgent(unittest.IsolatedAsyncioTestCase):

    async def _collect_turn(self, agent: KimiAgent) -> list:
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == "result":
                break
        return messages

    async def test_simple_reply_emits_result(self):
        fake_proc = FakeProcess()
        fake_proc.feed({"role": "assistant", "content": "Hello! How can I help you?"})

        opts = AgentOptions(cli_path="kimi")
        with patch("shutil.which", return_value="/usr/bin/kimi"):
            agent = KimiAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("Hello")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0].content[0].text, "Hello! How can I help you?")
        self.assertEqual(len(result_msgs), 1)

        # Sent as plain {"role":"user","content":...}, not Claude's {type, message} envelope
        sent = json.loads(fake_proc.written_lines[0])
        self.assertEqual(sent, {"role": "user", "content": "Hello"})

        await agent.disconnect()

    async def test_tool_call_sequence_defers_result_until_final_message(self):
        fake_proc = FakeProcess()
        fake_proc.feed({
            "role": "assistant",
            "content": "Let me check the current directory.",
            "tool_calls": [{
                "type": "function", "id": "tc_1",
                "function": {"name": "Shell", "arguments": '{"command":"ls"}'},
            }],
        })
        fake_proc.feed({"role": "tool", "tool_call_id": "tc_1", "content": "file1.py\nfile2.py"})
        fake_proc.feed({"role": "assistant", "content": "There are two Python files in the current directory."})

        opts = AgentOptions(cli_path="kimi")
        with patch("shutil.which", return_value="/usr/bin/kimi"):
            agent = KimiAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("List files")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        # Only ONE result, and it's the last message (after the tool-call round-trip)
        self.assertEqual(len(result_msgs), 1)
        self.assertIs(messages[-1], result_msgs[0])
        self.assertEqual(len(assistant_msgs), 2, "tool-calling message + final text message")

        # First assistant message carries the tool_use block
        tool_blocks = [b for b in assistant_msgs[0].content if isinstance(b, dict) and b.get("type") == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["name"], "Shell")
        self.assertEqual(tool_blocks[0]["input"], {"command": "ls"})

        await agent.disconnect()

    async def test_two_turns_reuse_same_persistent_process(self):
        fake_proc = FakeProcess()
        fake_proc.feed({"role": "assistant", "content": "first reply"})
        fake_proc.feed({"role": "assistant", "content": "second reply"})

        opts = AgentOptions(cli_path="kimi")
        with patch("shutil.which", return_value="/usr/bin/kimi"):
            agent = KimiAgent(options=opts)

        spawn_calls = []

        async def fake_create_subprocess_exec(*argv, **kwargs):
            spawn_calls.append(argv)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_create_subprocess_exec)):
            await agent.connect()
            await agent.send_message("first")
            await self._collect_turn(agent)
            await agent.send_message("second")
            await self._collect_turn(agent)

        self.assertEqual(len(spawn_calls), 1, "kimi should stay one persistent process across turns")
        self.assertEqual(len(fake_proc.written_lines), 2)

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
