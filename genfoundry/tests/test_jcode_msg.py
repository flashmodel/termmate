"""
Test JCodeAgent's event parsing and spawn-per-turn argv construction.

Fixtures match jcode's own mock_gateway.py test data (surfaced via
Deepwiki) -- real fixture shapes, not fabricated; docs/WRAPPERS.md only
listed event type names, not fields, so this was checked before writing
the parser.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.jcode_agent import JCodeAgent
from genfoundry.base_agent import AgentOptions, MessageType

TEXT_DELTA_EVENT = {"type": "text_delta", "text": "You said: Hello\n\nRunning a quick tool to demonstrate.\n\n"}
TOOL_START_EVENT = {"type": "tool_start", "id": "tool-123", "name": "bash"}
TOOL_INPUT_EVENT = {"type": "tool_input", "delta": '{"command":'}
TOOL_EXEC_EVENT = {"type": "tool_exec", "id": "tool-123", "name": "bash"}
TOOL_DONE_EVENT = {"type": "tool_done", "id": "tool-123", "name": "bash", "output": "hello\n", "error": None}
TOKENS_EVENT = {"type": "tokens", "input": 120, "output": 240}
DONE_EVENT = {
    "type": "done", "session_id": "session_abc123", "provider": "OpenAI",
    "model": "gpt-5.4", "text": "OK", "usage": {"input_tokens": 123, "output_tokens": 7},
}
ERROR_EVENT = {
    "type": "error", "session_id": "session_abc123", "provider": "OpenAI",
    "model": "gpt-5.4", "message": "An error occurred",
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


class TestJCodeAgent(unittest.IsolatedAsyncioTestCase):

    async def _collect_turn(self, agent: JCodeAgent) -> list:
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == MessageType.STOP.value:
                break
        return messages

    async def test_full_event_sequence(self):
        fake_proc = FakeProcess()
        for event in (TEXT_DELTA_EVENT, TOOL_START_EVENT, TOOL_INPUT_EVENT,
                      TOOL_EXEC_EVENT, TOOL_DONE_EVENT, TOKENS_EVENT, DONE_EVENT):
            fake_proc.feed(event)

        opts = AgentOptions(cli_path="jcode")
        with patch("shutil.which", return_value="/usr/bin/jcode"):
            agent = JCodeAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("Hello")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        # text_delta + tool_exec (tool_start/tool_input/tool_done/tokens are not rendered)
        self.assertEqual(len(assistant_msgs), 2)
        self.assertIn("You said: Hello", assistant_msgs[0].content[0].text)
        tool_block = assistant_msgs[1].content[0]
        self.assertEqual(tool_block["name"], "bash")
        self.assertEqual(tool_block["id"], "tool-123")

        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(result_msgs[0].content, "OK")
        self.assertEqual(agent._session_id, "session_abc123")

        await agent.disconnect()

    async def test_error_event(self):
        fake_proc = FakeProcess()
        fake_proc.feed(ERROR_EVENT)

        opts = AgentOptions(cli_path="jcode")
        with patch("shutil.which", return_value="/usr/bin/jcode"):
            agent = JCodeAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("do something bad")
            messages = await self._collect_turn(agent)

        error_msgs = [m for m in messages if getattr(m, "type", None) == "error"]
        self.assertEqual(len(error_msgs), 1)
        self.assertEqual(error_msgs[0].content, "An error occurred")

        await agent.disconnect()

    async def test_resume_argv_uses_resume_flag(self):
        fake_proc_1 = FakeProcess()
        fake_proc_1.feed(DONE_EVENT)

        opts = AgentOptions(cli_path="jcode")
        with patch("shutil.which", return_value="/usr/bin/jcode"):
            agent = JCodeAgent(options=opts)

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
        self.assertEqual(captured[1][idx + 1], "session_abc123")

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
