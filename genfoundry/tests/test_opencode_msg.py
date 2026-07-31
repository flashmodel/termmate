"""
Test OpenCodeAgent's event parsing and spawn-per-turn argv construction.

Fixtures are lifted from OpenCode's own test suite (packages/opencode/
test/run-process.test.ts, surfaced via Deepwiki) -- real assertions from
their tests, not a live capture (opencode.ai/docs doesn't publish example
JSON lines) and not fabricated.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.opencode_agent import OpenCodeAgent
from genfoundry.base_agent import AgentOptions, MessageType

TEXT_EVENT = {
    "type": "text", "timestamp": 1722384000000, "sessionID": "sess-oc-1",
    "part": {"type": "text", "text": "structured output"},
}
TOOL_USE_EVENT = {
    "type": "tool_use", "timestamp": 1722384001000, "sessionID": "sess-oc-1",
    "part": {"type": "tool", "tool": "bash",
             "state": {"status": "completed", "input": {"command": "ls"}, "output": "file1.py"}},
}
ERROR_EVENT = {
    "type": "error", "timestamp": 1722384002000, "sessionID": "sess-oc-1",
    "error": {"message": "Unknown model: test/nonexistent-model", "code": "UNKNOWN_MODEL"},
}
STEP_FINISH_EVENT = {
    "type": "step_finish", "timestamp": 1722384003000, "sessionID": "sess-oc-1",
    "part": {"type": "step-finish", "reason": "stop"},
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


class TestOpenCodeAgent(unittest.IsolatedAsyncioTestCase):

    async def _collect_turn(self, agent: OpenCodeAgent) -> list:
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == MessageType.STOP.value:
                break
        return messages

    async def test_text_tool_use_step_finish_sequence(self):
        fake_proc = FakeProcess()
        fake_proc.feed(TEXT_EVENT)
        fake_proc.feed(TOOL_USE_EVENT)
        fake_proc.feed(STEP_FINISH_EVENT)

        opts = AgentOptions(cli_path="opencode")
        with patch("shutil.which", return_value="/usr/bin/opencode"):
            agent = OpenCodeAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("do something")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        self.assertEqual(len(assistant_msgs), 2)
        self.assertEqual(assistant_msgs[0].content[0].text, "structured output")
        tool_block = assistant_msgs[1].content[0]
        self.assertEqual(tool_block["name"], "bash")
        self.assertEqual(tool_block["input"], {"command": "ls"})

        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(result_msgs[0].content, "stop")
        self.assertEqual(agent._session_id, "sess-oc-1")

        await agent.disconnect()

    async def test_error_event(self):
        fake_proc = FakeProcess()
        fake_proc.feed(ERROR_EVENT)

        opts = AgentOptions(cli_path="opencode")
        with patch("shutil.which", return_value="/usr/bin/opencode"):
            agent = OpenCodeAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("use a bad model")
            messages = await self._collect_turn(agent)

        error_msgs = [m for m in messages if getattr(m, "type", None) == "error"]
        self.assertEqual(len(error_msgs), 1)
        self.assertIn("Unknown model", error_msgs[0].content)

        await agent.disconnect()

    async def test_resume_argv_uses_session_flag(self):
        fake_proc_1 = FakeProcess()
        fake_proc_1.feed(TEXT_EVENT)
        fake_proc_1.feed(STEP_FINISH_EVENT)

        opts = AgentOptions(cli_path="opencode")
        with patch("shutil.which", return_value="/usr/bin/opencode"):
            agent = OpenCodeAgent(options=opts)

        captured = []

        async def fake_exec(*argv, **kwargs):
            captured.append(argv)
            return fake_proc_1 if len(captured) == 1 else FakeProcess()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_exec)):
            await agent.connect()
            await agent.send_message("first turn")
            await self._collect_turn(agent)
            await agent.send_message("second turn")

        self.assertNotIn("--session", captured[0])
        self.assertIn("--session", captured[1])
        idx = captured[1].index("--session")
        self.assertEqual(captured[1][idx + 1], "sess-oc-1")

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
