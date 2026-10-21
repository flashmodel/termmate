import asyncio
import json
import signal
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from genfoundry.base_agent import AgentOptions
from genfoundry.opencode_agent import (
    OpenCodeAgent,
    OpenCodeHTTPError,
    detect_opencode_version,
    detect_opencode_version_info,
    find_opencode_cli,
    find_opencode_v2_password,
    find_opencode_v2_service,
    get_opencode_session_info,
    list_opencode_sessions,
    parse_opencode_version_output,
    probe_opencode_service,
)


class TestFindOpenCodeCLI(unittest.TestCase):
    def test_finds_official_install_location(self):
        expected = "/Users/test/.opencode/bin/opencode"

        with patch("genfoundry.opencode_agent.shutil.which", return_value=None):
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
        self.agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
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
    async def test_terminates_the_managed_process_group(self):
        process = SimpleNamespace(
            pid=43210,
            returncode=None,
            wait=AsyncMock(return_value=0),
        )
        agent = OpenCodeAgent(AgentOptions(cwd="/workspace"))
        agent._server_process = process

        with patch("genfoundry.opencode_agent.sys.platform", "darwin"), patch(
            "genfoundry.opencode_agent.os.killpg"
        ) as killpg:
            await agent._terminate_server()

        killpg.assert_called_once_with(43210, signal.SIGTERM)
        self.assertIsNone(agent._server_process)

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
        if sys.platform == "win32":
            self.assertEqual(
                args[:5],
                ("opencode", "serve", "--hostname=127.0.0.1", "--port=0"),
            )
        else:
            self.assertEqual(args[:2], ("/bin/sh", "-c"))
            self.assertEqual(
                args[-4:],
                ("opencode", "serve", "--hostname=127.0.0.1", "--port=0"),
            )
            self.assertIn("kill -TERM -$$", args[2])
            self.assertEqual(
                create_process.await_args.kwargs["stdin"], asyncio.subprocess.PIPE
            )
            self.assertTrue(create_process.await_args.kwargs["start_new_session"])


class TestOpenCodeTurns(unittest.IsolatedAsyncioTestCase):
    async def test_first_prompt_creates_session_lazily(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
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
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
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
        agent = OpenCodeAgent(
            AgentOptions(cwd=".", session_id="ses_old"), version=1
        )
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
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
        agent._is_connected = True
        agent._session_id = "ses_old"
        agent._http = AsyncMock(return_value={})

        with self.assertRaisesRegex(
            RuntimeError, "OpenCode did not return a valid session"
        ):
            await agent.new_session()

        self.assertEqual(agent._session_id, "ses_old")

    async def test_prompt_uses_server_generated_message_id(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
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
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
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
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=1)
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
    def test_allow_edit_allows_edit_tools(self):
        options = AgentOptions(
            cwd=".",
            approve_mode="allow-edit",
            allowed_tools=["Read", "Glob", "Grep", "TodoWrite"],
            disallowed_tools=["AskUserQuestion"],
        )
        agent = OpenCodeAgent(options)
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["permission"]["edit"], "allow")
        self.assertEqual(config["permission"]["read"], "allow")
        self.assertEqual(config["permission"]["todowrite"], "allow")
        self.assertEqual(config["permission"]["*"], "ask")
        self.assertEqual(config["permission"]["question"], "deny")

    def test_defaults_to_allow_edit(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."))
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["permission"], {"*": "ask", "edit": "allow"})

    def test_default_mode_asks_for_edit(self):
        agent = OpenCodeAgent(AgentOptions(cwd=".", approve_mode="default"))
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(config["permission"], {"*": "ask"})

    def test_accept_all_allows_edit_and_asks_for_other_tools(self):
        agent = OpenCodeAgent(AgentOptions(cwd=".", approve_mode="accept-all"))
        config = json.loads(agent._runtime_env()["OPENCODE_CONFIG_CONTENT"])

        self.assertEqual(
            config["permission"],
            {"*": "ask", "edit": "allow"},
        )


class TestOpenCodeVersionDetection(unittest.TestCase):
    def test_parse_version_formats(self):
        self.assertEqual(
            parse_opencode_version_output("opencode v2.0.29"), (2, "2.0.29")
        )
        self.assertEqual(
            parse_opencode_version_output("1.18.25"), (1, "1.18.25")
        )
        self.assertEqual(
            parse_opencode_version_output("v1.18.25"), (1, "1.18.25")
        )

    def test_detect_version_v2(self):
        with patch("genfoundry.opencode_agent.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                stdout="opencode v2.0.29\n", stderr=""
            )
            self.assertEqual(detect_opencode_version("opencode"), 2)

    def test_detect_version_v1(self):
        with patch("genfoundry.opencode_agent.subprocess.run") as mock_run:
            mock_run.return_value = SimpleNamespace(
                stdout="1.18.25\n", stderr=""
            )
            self.assertEqual(detect_opencode_version("opencode"), 1)

    def test_detect_version_failure_returns_none(self):
        with patch(
            "genfoundry.opencode_agent.subprocess.run",
            side_effect=OSError("not found"),
        ):
            self.assertIsNone(detect_opencode_version("opencode"))

    def test_agent_auto_detects_version_from_cli(self):
        with patch(
            "genfoundry.opencode_agent.detect_opencode_version_info",
            return_value=(2, "2.0.29"),
        ):
            agent = OpenCodeAgent(AgentOptions(cwd="."))
            self.assertEqual(agent.version, 2)
            self.assertEqual(agent.version_str, "2.0.29")

        with patch(
            "genfoundry.opencode_agent.detect_opencode_version_info",
            return_value=(1, "1.18.25"),
        ):
            agent = OpenCodeAgent(AgentOptions(cwd="."))
            self.assertEqual(agent.version, 1)
            self.assertEqual(agent.version_str, "1.18.25")

    def test_find_opencode_v2_password(self):
        fake_json = json.dumps({"password": "secret_v2_password"})
        with patch("genfoundry.opencode_agent.os.path.isfile", return_value=True):
            with patch(
                "builtins.open", unittest.mock.mock_open(read_data=fake_json)
            ):
                self.assertEqual(find_opencode_v2_password(), "secret_v2_password")


class TestOpenCodeV2AgentEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        self.agent._session_id = "ses_v2"
        self.agent._turn_active = True

    async def _messages(self):
        result = []
        while not self.agent._message_queue.empty():
            result.append(await self.agent._message_queue.get())
        return result

    async def test_v2_text_delta_streaming(self):
        await self.agent._dispatch_event({
            "type": "session.text.delta",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "delta": "Hello from OpenCode V2",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "text")
        self.assertEqual(messages[0].content, "Hello from OpenCode V2")
        self.assertEqual(messages[0].id, "msg_v2_1")

    async def test_v2_reasoning_delta_streaming(self):
        await self.agent._dispatch_event({
            "type": "session.reasoning.delta",
            "data": {
                "sessionID": "ses_v2",
                "assistantMessageID": "msg_v2_1",
                "ordinal": 0,
                "delta": "Thinking step 1",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "thinking")
        self.assertEqual(messages[0].content, "Thinking step 1")

    async def test_v2_inbox_admission_emits_user_message_id(self):
        self.agent._awaiting_user_message_id = True
        await self.agent._dispatch_event({
            "type": "session.inbox.enqueued",
            "data": {
                "sessionID": "ses_v2",
                "inboxID": "msg_user_v2",
            },
        })

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "user_message_id")
        self.assertEqual(messages[0].content, {"message_id": "msg_user_v2"})
        self.assertIn("msg_user_v2", self.agent._user_message_ids)

    async def test_v2_tool_called_and_success(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_1",
                "name": "bash",
                "input": {"command": "ls -la"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_1",
                "output": "file1.txt\nfile2.txt",
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].content["name"], "command_execution")
        self.assertEqual(tools[0].content["command"], "ls -la")
        self.assertEqual(tools[0].content["output"], "file1.txt\nfile2.txt")
        self.assertEqual(tools[0].content["status"], "completed")

    async def test_v2_tool_lifecycle_input_started_and_patch(self):
        # Realistic OpenCode v2 flow: name is in input.started, not in called or success
        await self.agent._dispatch_event({
            "type": "session.tool.input.started",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_edit",
                "name": "edit",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_edit",
                "input": {
                    "path": "README.md",
                    "oldString": "hello",
                    "newString": "world",
                },
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_edit",
                "content": [{"type": "text", "text": "Edited README.md"}],
                "metadata": {
                    "files": [{
                        "file": "README.md",
                        "patch": "@@ -1 +1 @@\n-hello\n+world\n",
                    }],
                },
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].content["name"], "edit")
        self.assertEqual(tools[0].content["input"]["path"], "README.md")
        self.assertEqual(
            tools[0].content["metadata"]["diff"],
            "@@ -1 +1 @@\n-hello\n+world\n",
        )
        self.assertEqual(tools[0].content["status"], "completed")

    async def test_v2_tool_shell_lifecycle(self):
        await self.agent._dispatch_event({
            "type": "session.tool.input.started",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_sh",
                "name": "shell",
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_sh",
                "input": {"command": "git diff"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.success",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_sh",
                "content": [{"type": "text", "text": "diff output"}],
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].content["name"], "command_execution")
        self.assertEqual(tools[0].content["command"], "git diff")
        self.assertEqual(tools[0].content["output"], "diff output")

    async def test_v2_tool_failed(self):
        await self.agent._dispatch_event({
            "type": "session.tool.called",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_2",
                "name": "read",
                "input": {"path": "missing.py"},
            },
        })
        await self.agent._dispatch_event({
            "type": "session.tool.failed",
            "data": {
                "sessionID": "ses_v2",
                "id": "call_v2_2",
                "error": {"message": "File not found"},
            },
        })

        messages = await self._messages()
        tools = [m for m in messages if m.type == "tool_use"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].content["name"], "read")
        self.assertEqual(tools[0].content["error"], "File not found")
        self.assertEqual(tools[0].content["status"], "error")

    async def test_v2_execution_succeeded_finishes_turn(self):
        await self.agent._dispatch_event({
            "type": "session.execution.succeeded",
            "data": {"sessionID": "ses_v2"},
        })

        self.assertFalse(self.agent._turn_active)
        messages = await self._messages()
        self.assertEqual([m.type for m in messages], ["stop"])

    async def test_v2_execution_failed_emits_error(self):
        await self.agent._dispatch_event({
            "type": "session.execution.failed",
            "data": {
                "sessionID": "ses_v2",
                "error": {"message": "Model execution context exhausted"},
            },
        })

        self.assertFalse(self.agent._turn_active)
        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "error")
        self.assertIn("Model execution context exhausted", messages[0].content)

    async def test_v2_permission_asked_with_action_field(self):
        await self.agent._dispatch_event({
            "type": "permission.asked",
            "data": {
                "id": "per_123",
                "sessionID": "ses_v2",
                "action": "shell",
                "metadata": {"command": "ls -la"},
            },
        })

        messages = await self._messages()
        requests = [m for m in messages if m.type == "control_request"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].content["request_id"], "per_123")
        self.assertEqual(
            requests[0].content["request"]["tool_name"], "Bash"
        )


class TestOpenCodeV2Turns(unittest.IsolatedAsyncioTestCase):
    async def test_v2_connect_health_check(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._loop = asyncio.get_running_loop()
        agent._http = AsyncMock(return_value={"status": "ok"})
        agent._start_sse_thread = lambda env: agent._sse_ready.set()
        agent._fetch_models = AsyncMock()

        await agent._finish_connect({}, None)

        self.assertEqual(agent._http.await_args.args, ("GET", "/api/info"))
        await agent.disconnect()

    async def test_v2_first_prompt_creates_session_with_location(self):
        agent = OpenCodeAgent(AgentOptions(cwd="/test/workspace"), version=2)
        agent._is_connected = True
        agent._http = AsyncMock(side_effect=[
            {"id": "ses_v2_new"},
            None,
            None,
        ])

        await agent.send_message("hello v2")

        self.assertEqual(agent._session_id, "ses_v2_new")
        calls = [call.args for call in agent._http.await_args_list]
        self.assertEqual(
            calls,
            [
                ("POST", "/api/session"),
                ("POST", "/api/session/ses_v2_new/agent"),
                ("POST", "/api/session/ses_v2_new/prompt"),
            ],
        )
        self.assertEqual(
            agent._http.await_args_list[0].kwargs["body"]["location"]["directory"],
            "/test/workspace",
        )
        self.assertEqual(
            agent._http.await_args_list[1].kwargs["body"],
            {"agent": "build"},
        )
        self.assertEqual(
            agent._http.await_args_list[2].kwargs["body"],
            {"text": "hello v2"},
        )

    async def test_v2_session_create_with_disallowed_tools(self):
        agent = OpenCodeAgent(
            AgentOptions(
                cwd="/test/workspace",
                disallowed_tools=["AskUserQuestion", "bash"],
            ),
            version=2,
        )
        agent._is_connected = True
        agent._http = AsyncMock(side_effect=[
            {"id": "ses_v2_perms"},
            None,
            None,
        ])

        await agent.send_message("hello permissions")

        self.assertEqual(agent._session_id, "ses_v2_perms")
        post_session_body = agent._http.await_args_list[0].kwargs["body"]
        self.assertEqual(
            post_session_body["location"]["directory"],
            "/test/workspace",
        )
        self.assertEqual(
            post_session_body["permissions"],
            [
                {"action": "question", "resource": "*", "effect": "deny"},
                {"action": "shell", "resource": "*", "effect": "deny"},
            ],
        )

    async def test_v2_new_session_carries_permissions(self):
        agent = OpenCodeAgent(
            AgentOptions(
                cwd="/test/workspace",
                disallowed_tools=["AskUserQuestion"],
            ),
            version=2,
        )
        agent._is_connected = True
        agent._session_id = "ses_v2_old"
        agent._http = AsyncMock(return_value={"id": "ses_v2_fresh"})

        await agent.new_session()

        self.assertEqual(agent._session_id, "ses_v2_fresh")
        agent._http.assert_awaited_once_with(
            "POST",
            "/api/session",
            body={
                "location": {"directory": "/test/workspace"},
                "permissions": [
                    {"action": "question", "resource": "*", "effect": "deny"}
                ],
            },
        )

    async def test_v2_send_message_with_model_selection(self):
        agent = OpenCodeAgent(
            AgentOptions(cwd=".", model="anthropic/claude-3-7-sonnet"),
            version=2,
        )
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._http = AsyncMock(return_value=None)

        await agent.send_message("analyze code")

        calls = [call.args for call in agent._http.await_args_list]
        self.assertEqual(
            calls,
            [
                ("POST", "/api/session/ses_v2/agent"),
                ("POST", "/api/session/ses_v2/model"),
                ("POST", "/api/session/ses_v2/prompt"),
            ],
        )
        self.assertEqual(
            agent._http.await_args_list[1].kwargs["body"],
            {"model": {"providerID": "anthropic", "id": "claude-3-7-sonnet"}},
        )

    async def test_v2_interrupt_route(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._turn_active = True
        agent._http = AsyncMock(return_value=None)

        await agent.interrupt()

        agent._http.assert_awaited_once_with(
            "POST", "/api/session/ses_v2/interrupt", body={}, timeout=5.0
        )
        await agent._finish_turn()
        self.assertFalse(agent._turn_active)

    async def test_v2_rewind_routes(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._is_connected = True
        agent._session_id = "ses_v2"
        agent._http = AsyncMock(return_value=None)

        await agent.rewind("msg_123")

        calls = [call.args for call in agent._http.await_args_list]
        self.assertEqual(
            calls,
            [
                ("POST", "/api/session/ses_v2/revert/stage"),
                ("POST", "/api/session/ses_v2/revert/commit"),
            ],
        )
        self.assertEqual(
            agent._http.await_args_list[0].kwargs["body"],
            {"messageID": "msg_123"},
        )


class TestOpenCodeV2ModelsAndPermissions(unittest.IsolatedAsyncioTestCase):
    async def test_v2_fetch_models_parses_model_list(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        raw_models = [
            {
                "id": "gpt-6.1-sol-fast",
                "modelID": "gpt-6.1-sol",
                "providerID": "openai",
                "name": "GPT-6.1 Sol Fast",
            },
            {
                "id": "claude-3-5-sonnet",
                "modelID": "claude-3-5-sonnet",
                "providerID": "anthropic",
                "name": "Claude 3.5 Sonnet",
            },
        ]
        agent._http = AsyncMock(return_value=raw_models)

        await agent._fetch_models()

        agent._http.assert_awaited_once_with("GET", "/api/model")
        self.assertEqual(len(agent.available_models), 2)
        self.assertEqual(
            agent.available_models[0]["value"], "openai/gpt-6.1-sol-fast"
        )
        self.assertEqual(
            agent.available_models[1]["value"], "anthropic/claude-3-5-sonnet"
        )

    async def test_v2_permission_reply_route(self):
        agent = OpenCodeAgent(AgentOptions(cwd="."), version=2)
        agent._session_id = "ses_v2"
        agent._permission_sessions["perm_v2_1"] = "ses_v2"
        agent._http = AsyncMock(return_value=None)

        await agent.send_approval_response("perm_v2_1", {"behavior": "allow"})

        agent._http.assert_awaited_once_with(
            "POST",
            "/api/session/ses_v2/permission/perm_v2_1/reply",
            body={"decision": "once"},
        )


class TestOpenCodeSessionsAndResumption(unittest.TestCase):
    def test_unwrap_v2_envelope(self):
        envelope = {"data": [{"id": "s1"}], "cursor": "c1"}
        self.assertEqual(OpenCodeAgent._unwrap(envelope), [{"id": "s1"}])

        single = {"data": {"id": "s1"}}
        self.assertEqual(OpenCodeAgent._unwrap(single), {"id": "s1"})

        raw_list = [{"id": "s1"}]
        self.assertEqual(OpenCodeAgent._unwrap(raw_list), [{"id": "s1"}])

        with self.assertRaises(RuntimeError):
            OpenCodeAgent._unwrap({"error": {"message": "session failed"}})

    def test_list_opencode_sessions_v2(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:49374", password="pwd")
        v2_sessions = [
            {"id": "ses_v2_1", "title": "Session 1", "time": {"updated": 1700000000000}},
            {"id": "ses_v2_2", "title": "Session 2", "time": {"updated": 1700000010000}},
        ]

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", return_value=v2_sessions
            ):
                sessions = list_opencode_sessions(cwd="/workspace")

        self.assertEqual(len(sessions), 2)
        # Newest first
        self.assertEqual(sessions[0]["session_id"], "ses_v2_2")
        self.assertEqual(sessions[0]["mtime"], 1700000010.0)
        self.assertEqual(sessions[1]["session_id"], "ses_v2_1")
        self.assertEqual(sessions[1]["mtime"], 1700000000.0)

    def test_list_opencode_sessions_v1(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:50000", password="", version=1)
        v1_sessions = [
            {"id": "ses_v1", "title": "V1 Session", "time": {"updated": 1600000000000}},
        ]

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", return_value=v1_sessions
            ) as mock_req:
                sessions = list_opencode_sessions(cwd="/workspace")
                mock_req.assert_called_once_with(
                    "http://127.0.0.1:50000", "/session", "/workspace", None, ""
                )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "ses_v1")
        self.assertEqual(sessions[0]["summary"], "V1 Session")
        self.assertEqual(sessions[0]["mtime"], 1600000000.0)

    def test_get_opencode_session_info_v2_descending_messages(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:49374", password="pwd")
        v2_session = {
            "id": "ses_v2",
            "title": "V2 Title",
            "time": {"updated": 1700000050000},
        }
        # V2 returns messages descending (newest first)
        v2_messages = [
            {"type": "idle", "time": {"created": 1700000050000}},
            {
                "type": "assistant",
                "content": [{"type": "text", "text": "Turn 2 response"}],
                "time": {"created": 1700000040000},
            },
            {
                "type": "user",
                "text": "Turn 2 prompt",
                "time": {"created": 1700000030000},
            },
            {
                "type": "assistant",
                "content": [{"type": "text", "text": "Turn 1 response"}],
                "time": {"created": 1700000020000},
            },
            {
                "type": "user",
                "text": "Turn 1 prompt",
                "time": {"created": 1700000010000},
            },
        ]

        def fake_request(base_url, path, cwd, extra_env, password=None):
            if path == "/api/session/ses_v2":
                return v2_session
            if path == "/api/session/ses_v2/message":
                return v2_messages
            return None

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", side_effect=fake_request
            ):
                info = get_opencode_session_info("ses_v2", cwd="/workspace")

        self.assertIsNotNone(info)
        self.assertEqual(info["summary"], "V2 Title")
        self.assertEqual(info["mtime"], 1700000050.0)
        self.assertEqual(len(info["turns"]), 2)
        self.assertEqual(info["turns"][0]["prompt"], "Turn 1 prompt")
        self.assertEqual(info["turns"][0]["response"], "Turn 1 response")
        self.assertEqual(info["turns"][1]["prompt"], "Turn 2 prompt")
        self.assertEqual(info["turns"][1]["response"], "Turn 2 response")
        self.assertEqual(info["prompt"], "Turn 2 prompt")
        self.assertEqual(info["response"], "Turn 2 response")

    def test_get_opencode_session_info_v1_parts(self):
        mock_server = SimpleNamespace(url="http://127.0.0.1:50000", password="")
        v1_session = {
            "id": "ses_v1",
            "title": "V1 Title",
            "time": {"updated": 1600000020000},
        }
        v1_messages = [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "V1 prompt"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "V1 response"}],
            },
        ]

        def fake_request(base_url, path, cwd, extra_env, password=None):
            if path == "/session/ses_v1":
                return v1_session
            if path == "/session/ses_v1/message":
                return v1_messages
            return None

        with patch("genfoundry.opencode_agent._SyncOpenCodeServer") as mock_cls:
            mock_cls.return_value.__enter__.return_value = mock_server
            with patch(
                "genfoundry.opencode_agent._sync_request", side_effect=fake_request
            ):
                info = get_opencode_session_info("ses_v1", cwd="/workspace")

        self.assertIsNotNone(info)
        self.assertEqual(info["summary"], "V1 Title")
        self.assertEqual(len(info["turns"]), 1)
        self.assertEqual(info["turns"][0]["prompt"], "V1 prompt")
        self.assertEqual(info["turns"][0]["response"], "V1 response")

    def test_probe_opencode_service(self):
        class _MockContext:
            def __enter__(self):
                return SimpleNamespace(status=200)

            def __exit__(self, *args):
                pass

        with patch("genfoundry.opencode_agent.urlopen", return_value=_MockContext()):
            self.assertTrue(probe_opencode_service("http://127.0.0.1:49374", "pwd"))

        with patch(
            "genfoundry.opencode_agent.urlopen", side_effect=Exception("unreachable")
        ):
            self.assertFalse(probe_opencode_service("http://127.0.0.1:9999", "pwd"))


if __name__ == "__main__":
    unittest.main()
