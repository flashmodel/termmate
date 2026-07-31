"""
Test MimoAgent -- confirms it inherits OpenCodeAgent's argv/event-parsing
behavior correctly (mimo.xiaomi.com/mimocode's own docs show an identical
CLI surface to opencode: `mimo run [message] --format json`, -c/--continue,
-s/--session, --fork), just spawning a different binary name.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.mimo_agent import MimoAgent
from genfoundry.opencode_agent import OpenCodeAgent
from genfoundry.base_agent import AgentOptions, MessageType

TEXT_EVENT = {
    "type": "text", "timestamp": 1722384000000, "sessionID": "sess-mimo-1",
    "part": {"type": "text", "text": "hello from mimo"},
}
STEP_FINISH_EVENT = {
    "type": "step_finish", "timestamp": 1722384003000, "sessionID": "sess-mimo-1",
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


class TestMimoAgent(unittest.IsolatedAsyncioTestCase):

    def test_is_opencode_agent_subclass(self):
        self.assertTrue(issubclass(MimoAgent, OpenCodeAgent))

    async def test_spawns_mimo_binary_and_parses_opencode_shaped_events(self):
        fake_proc = FakeProcess()
        fake_proc.feed(TEXT_EVENT)
        fake_proc.feed(STEP_FINISH_EVENT)

        opts = AgentOptions(cli_path="mimo")
        with patch("shutil.which", return_value="/usr/bin/mimo"):
            agent = MimoAgent(options=opts)

        captured = []

        async def fake_exec(*argv, **kwargs):
            captured.append(argv)
            return fake_proc

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_exec)):
            await agent.connect()
            await agent.send_message("hi")

            messages = []
            async for msg in agent.receive_messages():
                messages.append(msg)
                if getattr(msg, "type", None) == MessageType.STOP.value:
                    break

        self.assertEqual(captured[0][0], "/usr/bin/mimo")
        self.assertIn("run", captured[0])
        self.assertIn("--format", captured[0])
        self.assertIn("json", captured[0])

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0].content[0].text, "hello from mimo")

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
