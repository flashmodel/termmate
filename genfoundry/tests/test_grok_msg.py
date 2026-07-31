"""
Test GrokAgent's event parsing and spawn-per-turn argv construction.

Uses the documented streaming-json event shapes (text/thought/end/error,
per https://deepwiki.com/xai-org/grok-build) as fixtures -- not live CLI
output, since the real grok binary was never invoked to produce this test.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.grok_agent import GrokAgent
from genfoundry.base_agent import AgentOptions, MessageType


class FakeProcess:
    """Simulates a grok subprocess: feed() queues NDJSON lines for stdout."""

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


class TestGrokAgent(unittest.IsolatedAsyncioTestCase):

    async def _collect_turn(self, agent: GrokAgent) -> list:
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == MessageType.STOP.value:
                break
        return messages

    async def test_text_thought_end_events(self):
        fake_proc = FakeProcess()
        fake_proc.feed({"type": "text", "data": "Working on it..."})
        fake_proc.feed({"type": "thought", "data": "considering approach"})
        fake_proc.feed({
            "type": "end",
            "stopReason": "complete",
            "sessionId": "sess-abc123",
            "requestId": "req-1",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        })

        opts = AgentOptions(cli_path="grok")
        with patch("shutil.which", return_value="/usr/bin/grok"):
            agent = GrokAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("do something")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        self.assertEqual(len(assistant_msgs), 2, "text + thought should each produce an AssistantMessage")
        self.assertIn("Working on it", assistant_msgs[0].content[0].text)
        self.assertIn("considering approach", assistant_msgs[1].content[0].text)
        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(result_msgs[0].content, "complete")
        self.assertEqual(agent._session_id, "sess-abc123", "end event should capture sessionId for resume")

        await agent.disconnect()

    async def test_error_event(self):
        fake_proc = FakeProcess()
        fake_proc.feed({"type": "error", "message": "API error (status 402 Payment Required)"})

        opts = AgentOptions(cli_path="grok")
        with patch("shutil.which", return_value="/usr/bin/grok"):
            agent = GrokAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("do something")
            messages = await self._collect_turn(agent)

        error_msgs = [m for m in messages if getattr(m, "type", None) == "error"]
        self.assertEqual(len(error_msgs), 1)
        self.assertIn("402", error_msgs[0].content)

        await agent.disconnect()

    async def test_resume_argv_uses_session_id_from_prior_turn(self):
        """Second turn's argv should include -r <session_id captured from turn 1's end event>."""
        fake_proc_1 = FakeProcess()
        fake_proc_1.feed({"type": "end", "stopReason": "complete", "sessionId": "sess-xyz"})

        opts = AgentOptions(cli_path="grok")
        with patch("shutil.which", return_value="/usr/bin/grok"):
            agent = GrokAgent(options=opts)

        captured_argvs = []

        async def fake_create_subprocess_exec(*argv, **kwargs):
            captured_argvs.append(argv)
            return fake_proc_1 if len(captured_argvs) == 1 else FakeProcess()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_create_subprocess_exec)):
            await agent.connect()
            await agent.send_message("first turn")
            await self._collect_turn(agent)

            await agent.send_message("second turn")

        self.assertNotIn("-r", captured_argvs[0], "first turn should not resume anything")
        self.assertIn("-r", captured_argvs[1])
        r_index = captured_argvs[1].index("-r")
        self.assertEqual(captured_argvs[1][r_index + 1], "sess-xyz")

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
