import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch, MagicMock

from genfoundry.base_agent import AgentOptions, Message
from genfoundry.acp_client import (
    AcpClient,
    find_gemini_cli,
    version_greater_or_equal,
    get_gemini_acp_flag,
)


class TestAcpCliDiscovery(unittest.TestCase):
    def test_version_greater_or_equal(self):
        self.assertTrue(version_greater_or_equal("0.34.0", "0.34.0"))
        self.assertTrue(version_greater_or_equal("0.52.0", "0.34.0"))
        self.assertTrue(version_greater_or_equal("1.0.0", "0.34.0"))
        self.assertFalse(version_greater_or_equal("0.33.9", "0.34.0"))
        self.assertFalse(version_greater_or_equal("0.9.0", "0.34.0"))

    def test_find_gemini_cli_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/gemini"):
            self.assertEqual(find_gemini_cli(), "/usr/local/bin/gemini")

    def test_find_gemini_cli_fallback(self):
        with patch("shutil.which", return_value=None):
            with patch("os.path.isfile", return_value=True), patch("os.access", return_value=True):
                found = find_gemini_cli()
                self.assertIsNotNone(found)

    def test_get_gemini_acp_flag_old_version(self):
        with patch("subprocess.check_output", return_value="0.32.1\n"):
            self.assertEqual(get_gemini_acp_flag("gemini"), "--experimental-acp")

    def test_get_gemini_acp_flag_new_version(self):
        with patch("subprocess.check_output", return_value="0.52.0\n"):
            self.assertEqual(get_gemini_acp_flag("gemini"), "--acp")


class TestAcpClientConfig(unittest.TestCase):
    def test_gemini_command_resolution(self):
        with patch("shutil.which", return_value="/usr/local/bin/gemini"):
            with patch("genfoundry.acp_client.get_gemini_acp_flag", return_value="--acp"):
                agent = AcpClient(agent_name="gemini")
                self.assertEqual(agent.cmd_list, ["/usr/local/bin/gemini", "--acp"])

    def test_custom_command_resolution(self):
        agent = AcpClient(
            command="custom-acp-bot --stdio",
            args=["--verbose"],
            agent_name="custom",
        )
        self.assertEqual(agent.cmd_list, ["custom-acp-bot", "--stdio", "--verbose"])

    def test_custom_list_command_resolution(self):
        agent = AcpClient(
            command=["node", "agent.js"],
            args=["--acp"],
            agent_name="node_agent",
        )
        self.assertEqual(agent.cmd_list, ["node", "agent.js", "--acp"])


class TestAcpClientProtocol(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        options = AgentOptions(cwd="/tmp", model="gemini-2.5-flash")
        self.agent = AcpClient(options, command="gemini", args=["--acp"], agent_name="gemini")
        self.agent.process = MagicMock()
        self.agent.process.stdin = MagicMock()
        self.agent.process.stdin.write = MagicMock()
        self.agent.process.stdin.drain = AsyncMock()
        self.agent.is_connected = True

    async def _drain_messages(self):
        messages = []
        while not self.agent._message_queue.empty():
            messages.append(await self.agent._message_queue.get())
        return messages

    async def test_handle_initialize_result(self):
        loop = asyncio.get_running_loop()
        self.agent._init_future = loop.create_future()
        self.agent._pending_requests[1] = "initialize"

        init_result = {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "agentInfo": {"name": "Gemini CLI", "version": "0.52.0"},
        }
        await self.agent._handle_jsonrpc_message({"jsonrpc": "2.0", "id": 1, "result": init_result})

        self.assertTrue(self.agent._init_future.done())
        self.assertEqual(self.agent.agent_info["name"], "Gemini CLI")
        self.assertEqual(self.agent.agent_capabilities["loadSession"], True)

    async def test_handle_session_new_result(self):
        loop = asyncio.get_running_loop()
        self.agent._session_future = loop.create_future()
        self.agent._pending_requests[2] = "session/new"

        session_result = {
            "sessionId": "ses_12345",
            "models": {
                "currentModelId": "gemini-2.5-flash",
                "availableModels": [
                    {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "description": "Fast"},
                    {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "description": "Powerful"},
                ],
            },
        }
        await self.agent._handle_jsonrpc_message({"jsonrpc": "2.0", "id": 2, "result": session_result})

        self.assertTrue(self.agent._session_future.done())
        self.assertEqual(self.agent._session_id, "ses_12345")
        self.assertEqual(self.agent.current_model_id, "gemini-2.5-flash")

        messages = await self._drain_messages()
        types = [m.type for m in messages]
        self.assertIn("system", types)
        self.assertIn("models_update", types)

        models_msg = next(m for m in messages if m.type == "models_update")
        self.assertEqual(len(models_msg.content["models"]), 2)
        self.assertEqual(models_msg.content["models"][0]["value"], "gemini-2.5-flash")

    async def test_handle_agent_message_chunk(self):
        await self.agent._handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "Hello world!"},
                }
            },
        })

        messages = await self._drain_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "text")
        self.assertEqual(messages[0].content, "Hello world!")

    async def test_thought_chunk_does_not_emit_text(self):
        """Per requirement: think block can be hidden/not displayed."""
        await self.agent._handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"text": "Let me reason about this internal plan..."},
                }
            },
        })

        messages = await self._drain_messages()
        # Verifies no 'text' message is emitted to the chat buffer!
        text_messages = [m for m in messages if m.type == "text"]
        self.assertEqual(text_messages, [])

        # Only a background 'thinking' signal is emitted
        self.assertTrue(all(m.type == "thinking" for m in messages))

    async def test_handle_tool_call(self):
        await self.agent._handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call_1",
                    "kind": "execute",
                    "title": "npm test",
                    "status": "in_progress",
                }
            },
        })

        messages = await self._drain_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "tool_use")
        self.assertEqual(messages[0].content["kind"], "execute")
        self.assertEqual(messages[0].content["title"], "npm test")

    async def test_handle_permission_request_and_response(self):
        await self.agent._handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "session/request_permission",
            "params": {
                "sessionId": "ses_12345",
                "toolCall": {"toolCallId": "call_2", "title": "Run command", "kind": "execute"},
                "options": [
                    {"optionId": "allow_once", "kind": "allow_once", "name": "Allow Once"},
                    {"optionId": "deny", "kind": "deny", "name": "Deny"},
                ],
            },
        })

        messages = await self._drain_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "control_request")
        self.assertEqual(messages[0].content["request_id"], "99")

        # Now test send_approval_response with allow
        await self.agent.send_approval_response("99", {"behavior": "allow"})
        self.agent.process.stdin.write.assert_called()
        written = self.agent.process.stdin.write.call_args[0][0].decode("utf-8")
        payload = json.loads(written)
        self.assertEqual(payload["id"], 99)
        self.assertEqual(payload["result"]["outcome"]["optionId"], "allow_once")

    async def test_handle_permission_deny(self):
        self.agent._pending_permissions["100"] = {
            "options": [{"optionId": "deny", "kind": "deny"}],
            "msg_id": 100,
        }
        await self.agent.send_approval_response("100", {"behavior": "deny"})
        written = self.agent.process.stdin.write.call_args[0][0].decode("utf-8")
        payload = json.loads(written)
        self.assertEqual(payload["id"], 100)
        self.assertEqual(payload["result"]["outcome"]["optionId"], "deny")

    async def test_handle_prompt_result(self):
        self.agent._pending_requests[5] = "session/prompt"
        await self.agent._handle_jsonrpc_message({
            "jsonrpc": "2.0",
            "id": 5,
            "result": {"stopReason": "end_turn"},
        })

        messages = await self._drain_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "result")
        self.assertEqual(messages[0].content["stopReason"], "end_turn")

    async def test_fs_read_and_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("hello from file")

            # Test fs/read_text_file
            await self.agent._handle_jsonrpc_message({
                "jsonrpc": "2.0",
                "id": 10,
                "method": "fs/read_text_file",
                "params": {"path": test_file},
            })
            written = self.agent.process.stdin.write.call_args[0][0].decode("utf-8")
            payload = json.loads(written)
            self.assertEqual(payload["result"]["content"], "hello from file")

            # Test fs/write_text_file
            out_file = os.path.join(tmpdir, "out.txt")
            await self.agent._handle_jsonrpc_message({
                "jsonrpc": "2.0",
                "id": 11,
                "method": "fs/write_text_file",
                "params": {"path": out_file, "content": "written content"},
            })
            with open(out_file, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "written content")
