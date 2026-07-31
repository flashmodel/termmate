"""
Test VibeAgent's event parsing and spawn-per-turn argv construction.

Fixtures match mistral-vibe's own test suite shapes (surfaced via
Deepwiki against test_streaming_output_uses_public_history_entries and
related tests) -- real test-derived data, not fabricated.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.vibe_agent import VibeAgent
from genfoundry.base_agent import AgentOptions, MessageType

USER_MESSAGE_ENTRY = {
    "type": "message", "session_id": "sess-vibe-1", "role": "user",
    "content": [{"text": "Explain decorators"}],
}
EFFECT_ENTRY = {
    "type": "effect", "session_id": "sess-vibe-1",
    "title": "read_file", "detail": {"path": "decorators.py"},
    "state": {"status": "completed"},
}
ASSISTANT_MESSAGE_ENTRY = {
    "type": "message", "session_id": "sess-vibe-1", "role": "assistant",
    "content": [{"text": "Decorators wrap functions."}],
}
NOTICE_ERROR_ENTRY = {
    "type": "notice", "session_id": "sess-vibe-1", "level": "error",
    "message": "Rate limit exceeded",
}


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


class TestVibeAgent(unittest.IsolatedAsyncioTestCase):

    async def _collect_turn(self, agent: VibeAgent) -> list:
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == MessageType.STOP.value:
                break
        return messages

    async def test_user_echo_is_skipped_and_assistant_reply_signals_completion(self):
        fake_proc = FakeProcess()
        fake_proc.feed(USER_MESSAGE_ENTRY)
        fake_proc.feed(EFFECT_ENTRY)
        fake_proc.feed(ASSISTANT_MESSAGE_ENTRY)

        opts = AgentOptions(cli_path="vibe")
        with patch("shutil.which", return_value="/usr/bin/vibe"):
            agent = VibeAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("Explain decorators")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        # user echo produces nothing; effect produces a tool_use block; final
        # assistant text produces a message + the heuristic "result"
        self.assertEqual(len(assistant_msgs), 2)
        tool_block = assistant_msgs[0].content[0]
        self.assertEqual(tool_block["name"], "read_file")
        self.assertEqual(assistant_msgs[1].content[0].text, "Decorators wrap functions.")
        self.assertGreaterEqual(len(result_msgs), 1)
        self.assertEqual(agent._session_id, "sess-vibe-1")

        await agent.disconnect()

    async def test_notice_error_surfaces_as_error_message(self):
        fake_proc = FakeProcess()
        fake_proc.feed(NOTICE_ERROR_ENTRY)

        opts = AgentOptions(cli_path="vibe")
        with patch("shutil.which", return_value="/usr/bin/vibe"):
            agent = VibeAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("do something rate-limited")
            messages = await self._collect_turn(agent)

        error_msgs = [m for m in messages if getattr(m, "type", None) == "error"]
        self.assertEqual(len(error_msgs), 1)
        self.assertEqual(error_msgs[0].content, "Rate limit exceeded")

        await agent.disconnect()

    async def test_resume_argv_uses_resume_flag(self):
        fake_proc_1 = FakeProcess()
        fake_proc_1.feed(ASSISTANT_MESSAGE_ENTRY)

        opts = AgentOptions(cli_path="vibe")
        with patch("shutil.which", return_value="/usr/bin/vibe"):
            agent = VibeAgent(options=opts)

        captured = []

        async def fake_exec(*argv, **kwargs):
            captured.append(argv)
            return fake_proc_1 if len(captured) == 1 else FakeProcess()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_exec)):
            await agent.connect()
            await agent.send_message("first turn")
            await self._collect_turn(agent)
            await agent.send_message("second turn")

        self.assertNotIn("--resume", captured[0])
        self.assertIn("--resume", captured[1])
        idx = captured[1].index("--resume")
        self.assertEqual(captured[1][idx + 1], "sess-vibe-1")

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
