import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from genfoundry.base_agent import AgentOptions
from genfoundry.opencode_agent import (
    OpenCodeAgent,
    OpenCodeHTTPError,
    find_opencode_cli,
)


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

    async def test_user_text_part_is_not_emitted(self):
        await self.agent._dispatch_event({
            "type": "message.updated",
            "properties": {
                "info": {
                    "id": "msg_user",
                    "sessionID": "ses_test",
                    "role": "user",
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "id": "part_user",
                    "sessionID": "ses_test",
                    "messageID": "msg_user",
                    "type": "text",
                    "text": "about you",
                },
            },
        })

        self.assertEqual(await self._messages(), [])

    async def test_submitted_user_delta_is_ignored_before_role_event(self):
        self.agent._user_message_ids.add("msg_user")
        await self.agent._dispatch_event({
            "type": "message.part.delta",
            "properties": {
                "sessionID": "ses_test",
                "messageID": "msg_user",
                "partID": "part_user",
                "field": "text",
                "delta": "about you",
            },
        })

        self.assertEqual(await self._messages(), [])

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

    async def test_current_permission_event_with_string_kind_is_emitted(self):
        await self.agent._dispatch_event({
            "type": "permission.asked",
            "properties": {
                "id": "perm_current",
                "sessionID": "ses_test",
                "permission": "bash",
                "patterns": ["git status && git diff HEAD"],
                "metadata": {"command": "git status && git diff HEAD"},
                "always": ["git status*"],
                "tool": {
                    "messageID": "msg_1",
                    "callID": "call_1",
                },
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        request = messages[0].content["request"]
        self.assertEqual(request["tool_name"], "Bash")
        self.assertEqual(
            request["input"]["pattern"],
            ["git status && git diff HEAD"],
        )
        self.assertEqual(
            self.agent._permission_sessions["perm_current"], "ses_test"
        )

    async def test_permission_response_uses_current_reply_route(self):
        self.agent._permission_sessions["perm_1"] = "ses_test"
        self.agent._http = AsyncMock(return_value=True)

        await self.agent.send_approval_response(
            "perm_1", {"behavior": "allow"}
        )

        self.agent._http.assert_awaited_once_with(
            "POST",
            "/permission/perm_1/reply",
            body={"reply": "once"},
        )

    async def test_permission_response_falls_back_to_legacy_route(self):
        self.agent._permission_sessions["perm_1"] = "ses_test"
        missing = OpenCodeHTTPError("POST", "/permission/perm_1/reply", 404, "missing")
        self.agent._http = AsyncMock(side_effect=[missing, None])

        await self.agent.send_approval_response(
            "perm_1", {"behavior": "deny", "message": "no"}
        )

        self.assertEqual(self.agent._http.await_count, 2)
        self.agent._http.assert_awaited_with(
            "POST",
            "/session/ses_test/permissions/perm_1",
            body={"response": "reject"},
        )


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


class TestOpenCodeTurns(unittest.IsolatedAsyncioTestCase):
    async def test_first_prompt_creates_session_lazily(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."))
        agent._is_connected = True
        agent._http = AsyncMock(side_effect=[{"id": "ses_new"}, None])

        await agent.send_message("hello")

        self.assertEqual(agent._session_id, "ses_new")
        self.assertEqual(
            [call.args for call in agent._http.await_args_list],
            [
                ("POST", "/session"),
                ("POST", "/session/ses_new/prompt_async"),
            ],
        )
        self.assertEqual(agent._http.await_args_list[0].kwargs, {"body": {}})

        messages = []
        while not agent._message_queue.empty():
            messages.append(await agent._message_queue.get())
        self.assertEqual(
            [message.type for message in messages],
            ["thread_started", "turn_started"],
        )
        self.assertEqual(messages[0].content, {"session_id": "ses_new"})

    async def test_connect_without_prompt_does_not_create_session(self):
        agent = OpenCodeAgent(AgentOptions(cwd=".", server_url="http://localhost"))
        agent._loop = asyncio.get_running_loop()
        agent._http = AsyncMock(return_value={"healthy": True})
        agent._start_sse_thread = lambda env: agent._sse_ready.set()
        agent._fetch_models = AsyncMock()

        await agent._finish_connect({}, None)
        await asyncio.sleep(0)

        self.assertIsNone(agent._session_id)
        self.assertEqual(agent._http.await_count, 1)
        self.assertEqual(agent._http.await_args.args, ("GET", "/global/health"))
        await agent.disconnect()

    async def test_new_session_switches_session_without_reconnecting(self):
        agent = OpenCodeAgent(AgentOptions(cwd=".", session_id="ses_old"))
        agent._is_connected = True
        agent._session_id = "ses_old"
        agent._turn_active = True
        agent._turn_plan_mode = True
        agent._uses_session_status = True
        agent._awaiting_user_message_id = True
        agent._text_cache["part_old"] = "old text"
        agent._part_types["part_old"] = "text"
        agent._message_roles["msg_old"] = "assistant"
        agent._user_message_ids.add("msg_user")
        agent._terminal_tools.add("tool_old")
        agent._permission_sessions["permission_old"] = "ses_old"
        agent._seen_permissions.add("permission_old")
        agent._diff_cache["file.py"] = "old diff"
        agent._plan_text = "old plan"
        agent._http = AsyncMock(side_effect=[None, {"id": "ses_new"}])

        session_id = await agent.new_session()

        self.assertEqual(session_id, "ses_new")
        self.assertEqual(agent._session_id, "ses_new")
        self.assertIsNone(agent.options.session_id)
        self.assertTrue(agent._is_connected)
        self.assertEqual(
            [call.args for call in agent._http.await_args_list],
            [
                ("POST", "/session/ses_old/abort"),
                ("POST", "/session"),
            ],
        )
        self.assertEqual(agent._http.await_args_list[1].kwargs, {"body": {}})
        self.assertFalse(agent._turn_active)
        self.assertFalse(agent._turn_plan_mode)
        self.assertFalse(agent._uses_session_status)
        self.assertFalse(agent._awaiting_user_message_id)
        self.assertTrue(agent._turn_done.is_set())
        self.assertEqual(agent._text_cache, {})
        self.assertEqual(agent._part_types, {})
        self.assertEqual(agent._message_roles, {})
        self.assertEqual(agent._user_message_ids, set())
        self.assertEqual(agent._terminal_tools, set())
        self.assertEqual(agent._permission_sessions, {})
        self.assertEqual(agent._seen_permissions, set())
        self.assertEqual(agent._diff_cache, {})
        self.assertEqual(agent._plan_text, "")

        message = await agent._message_queue.get()
        self.assertEqual(message.type, "thread_started")
        self.assertEqual(message.content, {"session_id": "ses_new"})

    async def test_new_session_rejects_invalid_response(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."))
        agent._is_connected = True
        agent._session_id = "ses_old"
        agent._http = AsyncMock(return_value={})

        with self.assertRaisesRegex(
            RuntimeError, "OpenCode did not return a valid session"
        ):
            await agent.new_session()

        self.assertEqual(agent._session_id, "ses_old")

    async def test_prompt_uses_server_generated_message_id(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."))
        agent._is_connected = True
        agent._session_id = "ses_test"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("hello")

        body = agent._http.await_args.kwargs["body"]
        self.assertNotIn("messageID", body)
        self.assertEqual(body["parts"], [{"type": "text", "text": "hello"}])

        await agent._dispatch_event({
            "type": "message.updated",
            "properties": {
                "info": {
                    "id": "msg_server_generated",
                    "sessionID": "ses_test",
                    "role": "user",
                },
            },
        })

        messages = []
        while not agent._message_queue.empty():
            messages.append(await agent._message_queue.get())
        assigned = [m for m in messages if m.type == "user_message_id"]
        self.assertEqual(len(assigned), 1)
        self.assertEqual(
            assigned[0].content["message_id"], "msg_server_generated"
        )
        self.assertIn("msg_server_generated", agent._user_message_ids)

    async def test_status_idle_releases_next_prompt_and_legacy_idle_is_ignored(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."))
        agent._is_connected = True
        agent._session_id = "ses_test"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("first")
        second = asyncio.create_task(agent.send_message("second"))
        await asyncio.sleep(0)

        self.assertEqual(agent._http.await_count, 1)
        self.assertFalse(second.done())

        await agent._dispatch_event({
            "type": "session.status",
            "properties": {
                "sessionID": "ses_test",
                "status": {"type": "idle"},
            },
        })
        await second
        self.assertEqual(agent._http.await_count, 2)
        self.assertTrue(agent._turn_active)

        # The compatibility event for the first turn may arrive after the
        # second prompt starts.  It must not finish that new turn.
        await agent._dispatch_event({
            "type": "session.idle",
            "properties": {"sessionID": "ses_test"},
        })
        self.assertTrue(agent._turn_active)

        await agent._dispatch_event({
            "type": "session.status",
            "properties": {
                "sessionID": "ses_test",
                "status": {"type": "idle"},
            },
        })
        self.assertFalse(agent._turn_active)

    async def test_legacy_idle_releases_prompt_without_status_protocol(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."))
        agent._is_connected = True
        agent._session_id = "ses_test"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("first")
        second = asyncio.create_task(agent.send_message("second"))
        await asyncio.sleep(0)

        await agent._dispatch_event({
            "type": "session.idle",
            "properties": {"sessionID": "ses_test"},
        })
        await second

        self.assertEqual(agent._http.await_count, 2)


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
