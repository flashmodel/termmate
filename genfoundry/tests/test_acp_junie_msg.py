"""
Test ACPAgent's JSON-RPC transport, session/update parsing, and the
bidirectional session/request_permission flow, using JunieAgent as the
concrete subclass under test.

Mocks the ACP subprocess the same way test_codex_msg.py does (patch
agent._write_json to capture outgoing messages and auto-feed scripted
responses/notifications), since ACPAgent is a persistent bidirectional
process like CodexAgent, not a spawn-per-turn one. Fixtures follow the
exact JSON-RPC method names and payload shapes confirmed via the official
Agent Client Protocol docs (agentclientprotocol.com / Context7) -- not
fabricated.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.junie_agent import JunieAgent
from genfoundry.base_agent import AgentOptions, MessageType


class FakeProcess:
    """Simulates a persistent ACP subprocess with scripted JSON-RPC responses."""

    def __init__(self):
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.returncode = None
        self._stdout_lines: list = []
        self._stdout_index = 0
        self.stdout = self
        self.stderr = self

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


class TestACPAgentViaJunie(unittest.IsolatedAsyncioTestCase):

    async def _create_agent(self, fake_proc: FakeProcess, extra_on_write=None) -> JunieAgent:
        opts = AgentOptions(cli_path="/usr/bin/junie", cwd="/tmp/project")
        with patch("shutil.which", return_value="/usr/bin/junie"):
            agent = JunieAgent(options=opts)

        self._rpc_calls: list = []

        async def mock_write_json(data: dict):
            self._rpc_calls.append(data)
            if extra_on_write:
                extra_on_write(data, fake_proc)
            if "id" in data and "method" in data:
                if data["method"] == "initialize":
                    fake_proc.feed({"jsonrpc": "2.0", "id": data["id"], "result": {
                        "protocolVersion": 1, "capabilities": {}, "info": {"name": "junie", "version": "1.0"},
                    }})
                elif data["method"] == "session/new":
                    fake_proc.feed({"jsonrpc": "2.0", "id": data["id"], "result": {"sessionId": "acp-sess-1"}})

        agent._write_json = mock_write_json

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()

        return agent

    async def test_connect_does_initialize_and_session_new_handshake(self):
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        methods = [c.get("method") for c in self._rpc_calls]
        self.assertIn("initialize", methods)
        self.assertIn("session/new", methods)
        self.assertEqual(agent._session_id, "acp-sess-1")

        await agent.disconnect()

    async def test_send_message_streams_updates_then_result(self):
        fake_proc = FakeProcess()

        def on_write(data, proc):
            if data.get("method") == "session/prompt":
                rid = data["id"]
                proc.feed({
                    "jsonrpc": "2.0", "method": "session/update",
                    "params": {"sessionUpdate": "agent_message_chunk", "sessionId": "acp-sess-1",
                               "content": {"type": "text", "text": "Hello from Junie"}},
                })
                proc.feed({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}})

        agent = await self._create_agent(fake_proc, extra_on_write=on_write)

        await agent.send_message("hi")

        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == "result":
                break

        assistant_msgs = [m for m in messages if type(m).__name__ == "AssistantMessage"]
        result_msgs = [m for m in messages if getattr(m, "type", None) == "result"]

        self.assertEqual(len(assistant_msgs), 1)
        self.assertEqual(assistant_msgs[0].content[0].text, "Hello from Junie")
        self.assertEqual(len(result_msgs), 1)
        self.assertEqual(result_msgs[0].content, "end_turn")

        await agent.disconnect()

    async def test_permission_request_round_trip_allow(self):
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        # Agent asks for permission (unsolicited request FROM the agent -- has id + method).
        fake_proc.feed({
            "jsonrpc": "2.0", "id": 99, "method": "session/request_permission",
            "params": {
                "sessionId": "acp-sess-1",
                "toolCall": {"toolCallId": "call_1", "title": "bash"},
                "options": [
                    {"optionId": "allow-once", "kind": "allow_once"},
                    {"optionId": "reject-once", "kind": "reject_once"},
                ],
            },
        })

        control_request_msg = None
        async for msg in agent.receive_messages():
            if getattr(msg, "type", None) == "control_request":
                control_request_msg = msg
                break

        self.assertIsNotNone(control_request_msg)
        self.assertEqual(control_request_msg.content["request"]["tool_name"], "bash")
        self.assertEqual(control_request_msg.content["request_id"], 99)

        await agent.send_permission_response(99, {"behavior": "allow"})

        reply = next(c for c in self._rpc_calls if c.get("id") == 99)
        self.assertEqual(reply["result"]["outcome"], {"outcome": "selected", "optionId": "allow-once"})

        await agent.disconnect()

    async def test_interrupt_answers_pending_permission_as_cancelled(self):
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        fake_proc.feed({
            "jsonrpc": "2.0", "id": 42, "method": "session/request_permission",
            "params": {
                "sessionId": "acp-sess-1",
                "toolCall": {"toolCallId": "call_2", "title": "edit_file"},
                "options": [{"optionId": "allow-once", "kind": "allow_once"}],
            },
        })

        async for msg in agent.receive_messages():
            if getattr(msg, "type", None) == "control_request":
                break

        await agent.interrupt()

        methods = [c.get("method") for c in self._rpc_calls]
        self.assertIn("session/cancel", methods)
        cancelled_reply = next(c for c in self._rpc_calls if c.get("id") == 42)
        self.assertEqual(cancelled_reply["result"]["outcome"], {"outcome": "cancelled"})

        await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
