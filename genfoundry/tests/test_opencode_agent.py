import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from genfoundry.base_agent import AgentOptions
from genfoundry.opencode_agent import OpenCodeAgent, find_opencode_cli


class TestFindOpenCodeCLI(unittest.TestCase):
    def test_finds_official_install_location(self):
        expected = "/Users/test/.opencode/bin/opencode"

        with patch(
            "genfoundry.opencode_agent.os.path.expanduser",
            return_value="/Users/test",
        ):
            with patch(
                "genfoundry.opencode_agent.os.path.isfile",
                side_effect=lambda path: path == expected,
            ):
                with patch(
                    "genfoundry.opencode_agent.os.access", return_value=True
                ):
                    self.assertEqual(find_opencode_cli(), expected)


class TestOpenCodeAgentEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = OpenCodeAgent(AgentOptions(cwd="."))
        self.agent._session_id = "ses_test"
        self.agent._turn_active = True

    async def _messages(self):
        result = []
        while not self.agent._message_queue.empty():
            result.append(await self.agent._message_queue.get())
        return result

    async def test_text_delta_and_full_update_are_not_duplicated(self):
        await self.agent._dispatch_event({
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_test",
                "messageID": "msg_1",
                "partID": "part_1",
                "field": "text",
                "delta": "hello",
            },
        })
        await self.agent._dispatch_event({
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part_1",
                    "sessionID": "ses_test",
                    "messageID": "msg_1",
                    "type": "text",
                    "text": "hello world",
                }
            },
        })

        messages = await self._messages()
        self.assertEqual(
            [message.content for message in messages if message.type == "text"],
            ["hello", " world"],
        )

    async def test_tool_is_emitted_only_once_at_terminal_state(self):
        base_part = {
            "id": "tool_1",
            "sessionID": "ses_test",
            "messageID": "msg_1",
            "type": "tool",
            "tool": "bash",
        }
        for status in ("pending", "running", "completed", "completed"):
            part = dict(base_part)
            part["state"] = {
                "status": status,
                "input": {"command": "pwd"},
                "title": "pwd",
                "output": "/tmp",
            }
            await self.agent._dispatch_event({
                "type": "message.part.updated",
                "properties": {"part": part},
            })

        messages = await self._messages()
        tools = [message for message in messages if message.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].content["status"], "completed")
        self.assertEqual(tools[0].content["command"], "pwd")

    async def test_plan_text_is_flushed_when_session_becomes_idle(self):
        self.agent._turn_plan_mode = True
        await self.agent._dispatch_event({
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_test",
                "messageID": "msg_1",
                "partID": "part_1",
                "field": "text",
                "delta": "1. inspect\n2. edit",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.idle",
            "properties": {"sessionID": "ses_test"},
        })

        messages = await self._messages()
        self.assertEqual([message.type for message in messages], ["plan_delta", "stop"])
        self.assertEqual(messages[0].content, "1. inspect\n2. edit")

    async def test_permission_event_uses_termmate_control_shape(self):
        await self.agent._dispatch_event({
            "type": "permission.asked",
            "properties": {
                "requestID": "perm_1",
                "sessionID": "ses_test",
                "type": "bash",
                "title": "Run tests",
                "metadata": {"command": "python -m unittest"},
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        request = messages[0].content["request"]
        self.assertEqual(messages[0].type, "control_request")
        self.assertEqual(request["tool_name"], "Bash")
        self.assertEqual(request["input"]["command"], "python -m unittest")


class TestOpenCodeManagedServer(unittest.IsolatedAsyncioTestCase):
    async def test_requests_an_os_assigned_port(self):
        output = asyncio.StreamReader()
        output.feed_data(
            b"opencode server listening on http://127.0.0.1:53127\n"
        )
        output.feed_eof()
        process = SimpleNamespace(stdout=output, returncode=None)
        agent = OpenCodeAgent(AgentOptions(cwd="/workspace", cli_path="opencode"))
        agent.cli_path = "opencode"

        with patch(
            "genfoundry.opencode_agent.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as create_process:
            url = await agent._start_server({"PATH": "/bin"})
            await agent._server_log_task

        self.assertEqual(url, "http://127.0.0.1:53127")
        args = create_process.await_args.args
        self.assertEqual(
            args[:5],
            ("opencode", "serve", "--hostname=127.0.0.1", "--port=0"),
        )


class TestOpenCodeRuntimeConfig(unittest.TestCase):
    def test_allow_edit_keeps_bash_ask_and_disables_questions(self):
        options = AgentOptions(
            cwd=".",
            approve_mode="allow-edit",
            allowed_tools=["Read", "Glob", "Grep"],
            disallowed_tools=["AskUserQuestion"],
        )
        agent = OpenCodeAgent(options)
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["permission"]["edit"], "allow")
        self.assertEqual(config["permission"]["read"], "allow")
        self.assertEqual(config["permission"]["*"], "ask")
        self.assertEqual(config["permission"]["question"], "deny")


if __name__ == "__main__":
    unittest.main()
