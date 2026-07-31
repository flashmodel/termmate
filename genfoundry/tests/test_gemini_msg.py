"""
Test GeminiAgent's event parsing and spawn-per-turn argv construction.

Fixtures are the REAL sequence captured earlier in this project's
development from `gemini -p "say hi" -o stream-json --skip-trust`
(a genuinely successful run, not billing-blocked like Grok/Qwen's
captures were) -- init, a real tool_use/tool_result round trip
(the tool call errored on a workspace-boundary check, which is itself
real behavior worth covering), a delta assistant reply, and result.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.gemini_agent import GeminiAgent
from genfoundry.base_agent import AgentOptions, MessageType

REAL_EVENTS = [
    {"type": "init", "timestamp": "2026-07-31T08:20:02.976Z",
     "session_id": "7d45251c-b3f2-4870-8ce6-ef05b0ddf41d", "model": "auto"},
    {"type": "message", "timestamp": "2026-07-31T08:20:02.981Z",
     "role": "user", "content": "say hi"},
    {"type": "tool_use", "timestamp": "2026-07-31T08:20:24.960Z",
     "tool_name": "read_file", "tool_id": "read_file__wa1ne6rn",
     "parameters": {"file_path": "C:\\Users\\donal\\agents.md"}},
    {"type": "tool_result", "timestamp": "2026-07-31T08:20:25.328Z",
     "tool_id": "read_file__wa1ne6rn", "status": "error",
     "output": "Path not in workspace: ...",
     "error": {"type": "invalid_tool_params", "message": "Path not in workspace: ..."}},
    {"type": "message", "timestamp": "2026-07-31T08:20:54.148Z",
     "role": "assistant", "content": "Hello! How can I assist you today?", "delta": True},
    {"type": "result", "timestamp": "2026-07-31T08:20:54.779Z", "status": "success",
     "stats": {"total_tokens": 37080, "input_tokens": 36033, "output_tokens": 58}},
]


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self._stdout_lines: list = []
        self._stdout_index = 0
        self.stdout = self
        self.stderr = self

    def feed(self, data: dict) -> None:
        self._stdout_lines.append(json.dumps(data).encode() + b"\n")

    async def read(self, n: int) -> bytes:
        if self._stdout_index >= len(self._stdout_lines):
            self.returncode = 0
            return b""
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


class TestGeminiAgent(unittest.IsolatedAsyncioTestCase):

    async def _collect_turn(self, agent: GeminiAgent) -> list:
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == MessageType.STOP.value:
                break
        return messages

    async def test_real_captured_sequence_parses(self):
        fake_proc = FakeProcess()
        for event in REAL_EVENTS:
            fake_proc.feed(event)

        opts = AgentOptions(cli_path="gemini")
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            agent = GeminiAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("say hi")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        # tool_use block + final assistant text = 2 AssistantMessages
        self.assertEqual(len(assistant_msgs), 2)
        tool_blocks = [b for b in assistant_msgs[0].content if isinstance(b, dict) and b.get("type") == "tool_use"]
        self.assertEqual(len(tool_blocks), 1)
        self.assertEqual(tool_blocks[0]["name"], "read_file")
        self.assertEqual(assistant_msgs[1].content[0].text, "Hello! How can I assist you today?")

        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(agent._session_id, "7d45251c-b3f2-4870-8ce6-ef05b0ddf41d")

        await agent.disconnect()

    async def test_first_turn_argv_has_no_resume_flag(self):
        fake_proc = FakeProcess()
        fake_proc.feed(REAL_EVENTS[0])
        fake_proc.feed(REAL_EVENTS[-1])

        opts = AgentOptions(cli_path="gemini")
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            agent = GeminiAgent(options=opts)

        captured = []

        async def fake_exec(*argv, **kwargs):
            captured.append(argv)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_exec)):
            await agent.connect()
            await agent.send_message("say hi")

        self.assertIn("--skip-trust", captured[0])
        self.assertNotIn("-r", captured[0])

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
