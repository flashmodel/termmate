"""
Test QwenAgent's event parsing and spawn-per-turn argv construction.

Fixtures are a trimmed copy of a REAL `qwen -p "say hi" --output-format
stream-json` invocation captured earlier in this project's development
(the request hit a billing error, but the JSON envelope came through
fully -- session_id, assistant/message/content shape, and result are all
genuine, not fabricated).
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.qwen_agent import QwenAgent
from genfoundry.base_agent import AgentOptions, MessageType

REAL_INIT_EVENT = {
    "type": "system", "subtype": "init",
    "uuid": "d88605ac-6536-4d98-b004-8bb581b37e69",
    "session_id": "d88605ac-6536-4d98-b004-8bb581b37e69",
    "cwd": "C:\\Users\\donal\\AppData\\Local\\Temp",
    "model": "z-ai/glm-5.2",
    "permission_mode": "auto",
    "qwen_code_version": "0.21.1",
}
REAL_ASSISTANT_EVENT = {
    "type": "assistant",
    "uuid": "df7ebd0d-47a1-42d7-9987-c60c8b45431c",
    "session_id": "d88605ac-6536-4d98-b004-8bb581b37e69",
    "parent_tool_use_id": None,
    "message": {
        "id": "df7ebd0d-47a1-42d7-9987-c60c8b45431c",
        "type": "message", "role": "assistant", "model": "z-ai/glm-5.2",
        "content": [{"type": "text", "text": "[API Error: 402 This request requires more credits]"}],
        "stop_reason": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    },
}
REAL_RESULT_EVENT = {
    "type": "result", "subtype": "success",
    "uuid": "06e266c3-93a0-4fa8-ac5a-3b213bb1ea24",
    "session_id": "d88605ac-6536-4d98-b004-8bb581b37e69",
    "is_error": False, "duration_ms": 1623, "num_turns": 1,
    "result": "[API Error: 402 This request requires more credits]",
    "usage": {"input_tokens": 0, "output_tokens": 0},
    "permission_denials": [],
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


class TestQwenAgent(unittest.IsolatedAsyncioTestCase):

    async def _collect_turn(self, agent: QwenAgent) -> list:
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == MessageType.STOP.value:
                break
        return messages

    async def test_real_captured_envelope_parses(self):
        fake_proc = FakeProcess()
        fake_proc.feed(REAL_INIT_EVENT)
        fake_proc.feed(REAL_ASSISTANT_EVENT)
        fake_proc.feed(REAL_RESULT_EVENT)

        opts = AgentOptions(cli_path="qwen")
        with patch("shutil.which", return_value="/usr/bin/qwen"):
            agent = QwenAgent(options=opts)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
            await agent.connect()
            await agent.send_message("say hi")
            messages = await self._collect_turn(agent)

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        self.assertEqual(len(assistant_msgs), 1)
        self.assertIn("402", assistant_msgs[0].content[0].text)
        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(agent._session_id, "d88605ac-6536-4d98-b004-8bb581b37e69")

        await agent.disconnect()

    async def test_resume_argv_uses_session_id_from_init_event(self):
        fake_proc_1 = FakeProcess()
        fake_proc_1.feed(REAL_INIT_EVENT)
        fake_proc_1.feed(REAL_RESULT_EVENT)

        opts = AgentOptions(cli_path="qwen")
        with patch("shutil.which", return_value="/usr/bin/qwen"):
            agent = QwenAgent(options=opts)

        captured_argvs = []

        async def fake_create_subprocess_exec(*argv, **kwargs):
            captured_argvs.append(argv)
            return fake_proc_1 if len(captured_argvs) == 1 else FakeProcess()

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_create_subprocess_exec)):
            await agent.connect()
            await agent.send_message("first turn")
            await self._collect_turn(agent)
            await agent.send_message("second turn")

        self.assertNotIn("--resume", captured_argvs[0])
        self.assertIn("--resume", captured_argvs[1])
        idx = captured_argvs[1].index("--resume")
        self.assertEqual(captured_argvs[1][idx + 1], "d88605ac-6536-4d98-b004-8bb581b37e69")

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
