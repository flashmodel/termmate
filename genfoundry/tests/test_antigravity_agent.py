import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.antigravity_agent import (
    AntigravityAgent,
    PLAN_MODE_SYSTEM_PROMPT,
    find_antigravity_cli,
    list_antigravity_sessions,
    get_antigravity_session_tail,
    _clean_antigravity_prompt,
    _get_transcript_paths,
    _get_base_dir,
    _parse_iso_timestamp,
    _is_workspace_match,
    _read_transcript_summary_and_mtime,
)
from genfoundry.base_agent import AgentOptions, Message


class TestFindAntigravityCLI(unittest.TestCase):
    def test_finds_on_path_agy(self):
        with patch("genfoundry.antigravity_agent.shutil.which", side_effect=lambda cmd: "/usr/local/bin/agy" if cmd == "agy" else None):
            self.assertEqual(find_antigravity_cli(), "/usr/local/bin/agy")

    def test_finds_on_path_antigravity(self):
        with patch("genfoundry.antigravity_agent.shutil.which", side_effect=lambda cmd: "/opt/bin/antigravity" if cmd == "antigravity" else None):
            self.assertEqual(find_antigravity_cli(), "/opt/bin/antigravity")

    def test_finds_candidate_path_when_not_on_path(self):
        expected = os.path.expanduser("~/.local/bin/agy")
        with patch("genfoundry.antigravity_agent.shutil.which", return_value=None), \
             patch("genfoundry.antigravity_agent.os.path.isfile", side_effect=lambda p: p == expected), \
             patch("genfoundry.antigravity_agent.os.access", return_value=True):
            self.assertEqual(find_antigravity_cli(), expected)

    def test_returns_none_when_not_found(self):
        with patch("genfoundry.antigravity_agent.shutil.which", return_value=None), \
             patch("genfoundry.antigravity_agent.os.path.isfile", return_value=False):
            self.assertIsNone(find_antigravity_cli())


class TestAntigravityAgentCommand(unittest.IsolatedAsyncioTestCase):
    def _create_fake_proc(self):
        fake_proc = AsyncMock()
        fake_proc.stdin = MagicMock()
        fake_proc.stdin.drain = AsyncMock()
        fake_proc.stdout = AsyncMock()
        fake_proc.stderr = AsyncMock()
        fake_proc.terminate = MagicMock()
        fake_proc.kill = MagicMock()
        fake_proc.returncode = None
        return fake_proc

    async def _connect_with_mocks(self, agent):
        fake_proc = self._create_fake_proc()
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec, \
             patch.object(agent, "_read_stdout", new_callable=AsyncMock), \
             patch.object(agent, "_read_stderr", new_callable=AsyncMock):
            await agent.connect()
            return mock_exec.call_args[0]

    async def test_connect_default_flags(self):
        opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace")
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertEqual(args[0], "/mock/bin/agy")
        self.assertIn("--input-format", args)
        self.assertIn("stream-json", args)
        self.assertIn("--output-format", args)

    async def test_connect_model_and_effort_flags(self):
        opts = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            model="gemini-2.5-pro",
            think_level="high",
        )
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertIn("--model", args)
        idx_model = args.index("--model")
        self.assertEqual(args[idx_model + 1], "gemini-2.5-pro")

        self.assertIn("--effort", args)
        idx_effort = args.index("--effort")
        self.assertEqual(args[idx_effort + 1], "high")

    async def test_connect_effort_auto_or_none_omitted(self):
        for level in ("auto", "default", "none", "off"):
            opts = AgentOptions(
                cli_path="/mock/bin/agy",
                cwd="/workspace",
                think_level=level,
            )
            agent = AntigravityAgent(options=opts)
            args = await self._connect_with_mocks(agent)
            self.assertNotIn("--effort", args)

    async def test_connect_model_requires_effort_defaults(self):
        opts = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            model="gemini-3.8-flash",
            think_level=None,
        )
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertIn("--model", args)
        self.assertIn("--effort", args)
        idx_effort = args.index("--effort")
        self.assertEqual(args[idx_effort + 1], "medium")

        opts_pro = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            model="gemini-3.1-pro",
            think_level=None,
        )
        agent_pro = AntigravityAgent(options=opts_pro)
        args_pro = await self._connect_with_mocks(agent_pro)
        self.assertIn("--model", args_pro)
        self.assertIn("--effort", args_pro)
        idx_effort_pro = args_pro.index("--effort")
        self.assertEqual(args_pro[idx_effort_pro + 1], "high")

    async def test_connect_approve_mode_accept_all(self):
        opts = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            approve_mode="accept-all",
        )
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertIn("--dangerously-skip-permissions", args)

    async def test_connect_plan_mode_omits_dangerously_skip_permissions(self):
        opts = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            approve_mode="accept-all",
            plan_mode=True,
        )
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        # In plan mode, --mode plan is passed as CLI mode-level lock
        self.assertIn("--mode", args)
        idx_mode = args.index("--mode")
        self.assertEqual(args[idx_mode + 1], "plan")
        # In plan mode, omit dangerously-skip-permissions so stop hook review policy
        # does not auto-approve artifact feedback requests and start executing
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_set_plan_mode_toggles_state(self):
        opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace", plan_mode=False)
        agent = AntigravityAgent(options=opts)
        agent.set_plan_mode(True)
        self.assertTrue(agent.plan_mode)
        self.assertTrue(agent.options.plan_mode)

    async def test_connect_sandbox_mode_enabled(self):
        opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace", sandbox_mode=True)
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertIn("--sandbox", args)

    async def test_connect_sandbox_mode_string_mode(self):
        opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace", sandbox_mode="workspace-write")
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertIn("--sandbox", args)

    async def test_connect_sandbox_mode_disabled(self):
        opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace", sandbox_mode=False)
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertNotIn("--sandbox", args)

    async def test_connect_sandbox_mode_danger_full_access(self):
        opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace", sandbox_mode="danger-full-access")
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertNotIn("--sandbox", args)

    async def test_connect_session_id_flag(self):
        opts = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            session_id="conv-12345",
        )
        agent = AntigravityAgent(options=opts)
        args = await self._connect_with_mocks(agent)
        self.assertIn("--conversation", args)
        idx = args.index("--conversation")
        self.assertEqual(args[idx + 1], "conv-12345")


class TestAntigravityAgentEvents(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace")
        self.agent = AntigravityAgent(options=self.opts)
        self.agent.process = MagicMock()
        self.agent.process.stdin = MagicMock()
        self.agent.process.stdin.write = MagicMock()
        self.agent.process.stdin.drain = AsyncMock()
        self.agent.process.returncode = None
        self.agent.is_connected = True

    async def _messages(self):
        msgs = []
        while not self.agent.message_queue.empty():
            msgs.append(await self.agent.message_queue.get())
        return msgs

    async def test_init_event_sets_session_id(self):
        line = json.dumps({
            "event": "init",
            "conversation_id": "test-session-abc",
            "model": "gemini-2.5-pro",
        })
        await self.agent._handle_json_event(json.loads(line))
        self.assertEqual(self.agent.session_id, "test-session-abc")
        self.assertEqual(self.agent.options.session_id, "test-session-abc")

    async def test_text_delta_event(self):
        event = {
            "event": "step_update",
            "step_index": 1,
            "step_type": "agent_response",
            "state": "ACTIVE",
            "text_delta": "Hello from ",
        }
        await self.agent._handle_json_event(event)

        event2 = {
            "event": "step_update",
            "step_index": 1,
            "step_type": "agent_response",
            "state": "ACTIVE",
            "text_delta": "Antigravity!",
        }
        await self.agent._handle_json_event(event2)

        messages = await self._messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual([m.content for m in messages], ["Hello from ", "Antigravity!"])
        self.assertEqual(messages[0].type, "text_delta")

    async def test_thinking_event(self):
        event = {
            "event": "step_update",
            "step_index": 0,
            "step_type": "thinking",
            "state": "ACTIVE",
            "text_delta": "Analyzing the codebase...",
        }
        await self.agent._handle_json_event(event)

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "thinking")
        self.assertEqual(messages[0].content, "Analyzing the codebase...")

    async def test_tool_start_and_complete(self):
        start_event = {
            "event": "step_update",
            "step_index": 2,
            "step_type": "tool",
            "state": "ACTIVE",
            "tool_call": {
                "id": "call_1",
                "name": "run_command",
                "args": {"CommandLine": "ls -la"},
            },
        }
        await self.agent._handle_json_event(start_event)

        complete_event = {
            "event": "step_update",
            "step_index": 2,
            "step_type": "tool",
            "state": "DONE",
            "tool_call": {
                "id": "call_1",
                "name": "run_command",
            },
            "tool_result": {
                "id": "call_1",
                "output": "file1.txt\nfile2.txt",
                "is_error": False,
            },
        }
        await self.agent._handle_json_event(complete_event)

        messages = await self._messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].type, "tool_use")
        self.assertEqual(messages[0].content["name"], "run_command")
        self.assertEqual(messages[0].content["id"], "call_1")
        self.assertEqual(messages[0].content["state"], "ACTIVE")

        self.assertEqual(messages[1].type, "tool_use")
        self.assertEqual(messages[1].content["id"], "call_1")
        self.assertEqual(messages[1].content["output"], "file1.txt\nfile2.txt")
        self.assertEqual(messages[1].content["state"], "DONE")

    async def test_tool_event_with_json_string_params(self):
        event = {
            "event": "step_update",
            "step_index": 3,
            "step_type": "tool",
            "state": "ACTIVE",
            "tool_call": {
                "id": "call_2",
                "name": "run_command",
                "args": '{"CommandLine": "echo test"}',
            },
        }
        await self.agent._handle_json_event(event)
        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content["parameters"], {"CommandLine": "echo test"})

    async def test_result_event(self):
        event = {
            "event": "result",
            "status": "SUCCESS",
            "content": "Finished successfully.",
        }
        await self.agent._handle_json_event(event)

        messages = await self._messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "result")
        self.assertEqual(messages[0].content["status"], "SUCCESS")

    async def test_result_event_with_token_usage(self):
        step_event = {
            "event": "step_update",
            "step_update": {
                "step_index": 1960,
                "state": "DONE",
                "step_type": "agent_response",
                "usage": {
                    "input_tokens": 3099,
                    "output_tokens": 3692,
                    "thinking_tokens": 1918,
                    "cache_read_tokens": 218110,
                    "total_tokens": 6791,
                },
            },
        }
        await self.agent._handle_json_event(step_event)

        result_event = {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "num_turns": 58,
                "usage": {
                    "input_tokens": 5972947,
                    "output_tokens": 550875,
                    "thinking_tokens": 364558,
                    "cache_read_tokens": 130547261,
                    "total_tokens": 6523822,
                },
            },
        }
        await self.agent._handle_json_event(result_event)

        messages = await self._messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].type, "token_usage")
        self.assertEqual(messages[1].type, "result")

        token_usage = messages[0].content
        self.assertEqual(token_usage["last"]["inputTokens"], 221209)
        self.assertEqual(token_usage["last"]["outputTokens"], 3692)
        self.assertEqual(token_usage["last"]["cachedInputTokens"], 218110)
        self.assertEqual(token_usage["last"]["totalTokens"], 224901)
        self.assertEqual(token_usage["last"]["reasoningOutputTokens"], 1918)

        self.assertEqual(token_usage["total"]["inputTokens"], 136520208)
        self.assertEqual(token_usage["total"]["outputTokens"], 550875)
        self.assertEqual(token_usage["total"]["cachedInputTokens"], 130547261)
        self.assertEqual(token_usage["total"]["totalTokens"], 137071083)
        self.assertEqual(token_usage["total"]["reasoningOutputTokens"], 364558)

    async def test_result_event_with_multi_step_token_accumulation(self):
        # Step 1: calling grep tool (output 400, thinking 100)
        step1 = {
            "event": "step_update",
            "step_update": {
                "step_index": 10,
                "state": "DONE",
                "step_type": "agent_response",
                "usage": {
                    "input_tokens": 2000,
                    "output_tokens": 400,
                    "thinking_tokens": 100,
                    "cache_read_tokens": 100000,
                    "total_tokens": 2400,
                },
            },
        }
        await self.agent._handle_json_event(step1)

        # Step 2: calling edit tool (output 300, thinking 50)
        step2 = {
            "event": "step_update",
            "step_update": {
                "step_index": 12,
                "state": "DONE",
                "step_type": "agent_response",
                "usage": {
                    "input_tokens": 2500,
                    "output_tokens": 300,
                    "thinking_tokens": 50,
                    "cache_read_tokens": 100000,
                    "total_tokens": 2800,
                },
            },
        }
        await self.agent._handle_json_event(step2)

        # Step 3: final answer (output 500, thinking 200)
        step3 = {
            "event": "step_update",
            "step_update": {
                "step_index": 14,
                "state": "DONE",
                "step_type": "agent_response",
                "usage": {
                    "input_tokens": 3000,
                    "output_tokens": 500,
                    "thinking_tokens": 200,
                    "cache_read_tokens": 100000,
                    "total_tokens": 3500,
                },
            },
        }
        await self.agent._handle_json_event(step3)

        result_event = {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "num_turns": 3,
                "usage": {
                    "input_tokens": 15000,
                    "output_tokens": 3000,
                    "thinking_tokens": 1000,
                    "cache_read_tokens": 300000,
                    "total_tokens": 18000,
                },
            },
        }
        await self.agent._handle_json_event(result_event)

        messages = await self._messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].type, "token_usage")
        self.assertEqual(messages[1].type, "result")

        usage = messages[0].content
        # Turn output tokens: 400 + 300 + 500 = 1200
        self.assertEqual(usage["last"]["outputTokens"], 1200)
        # Turn thinking tokens: 100 + 50 + 200 = 350
        self.assertEqual(usage["last"]["reasoningOutputTokens"], 350)
        # Accumulated input across all steps: 102000 + 102500 + 103000 = 307500
        self.assertEqual(usage["last"]["inputTokens"], 307500)
        self.assertEqual(usage["last"]["cachedInputTokens"], 300000)
        # Total tokens in turn: 307500 + 1200 = 308700
        self.assertEqual(usage["last"]["totalTokens"], 308700)
        # Active context from latest step: 3000 uncached + 100000 cached = 103000
        self.assertEqual(usage["last"]["activeContextTokens"], 103000)

        # Total session metrics normalized
        self.assertEqual(usage["total"]["inputTokens"], 315000)
        self.assertEqual(usage["total"]["outputTokens"], 3000)
        self.assertEqual(usage["total"]["cachedInputTokens"], 300000)
        self.assertEqual(usage["total"]["totalTokens"], 318000)
        self.assertEqual(usage["total"]["reasoningOutputTokens"], 1000)

        # Buffer is cleared for next turn
        self.assertEqual(self.agent._turn_step_usages, [])
        self.assertIsNone(self.agent._last_step_usage)

    async def test_result_event_context_canceled_suppresses_error_message(self):
        event = {
            "event": "result",
            "result": {
                "conversation_id": "conv-1",
                "status": "ERROR",
                "response": "",
                "error": "context canceled",
            },
        }
        await self.agent._handle_json_event(event)

        messages = await self._messages()
        # Should only yield the result message, not an error message
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, "result")

    async def test_result_event_actual_error_emits_error_message(self):
        event = {
            "event": "result",
            "result": {
                "conversation_id": "conv-1",
                "status": "ERROR",
                "response": "",
                "error": "Quota exceeded",
            },
        }
        await self.agent._handle_json_event(event)

        messages = await self._messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].type, "error")
        self.assertEqual(messages[0].content, "Quota exceeded")
        self.assertEqual(messages[1].type, "result")

    def test_set_plan_mode_marks_restart_pending_without_terminating(self):
        self.agent.plan_mode = False
        self.agent.options.plan_mode = False
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock()
        self.agent.process = mock_proc
        self.agent.is_connected = True

        self.agent.set_plan_mode(True)
        self.assertTrue(self.agent.plan_mode)
        self.assertTrue(self.agent.options.plan_mode)
        self.assertTrue(self.agent._restart_pending)
        mock_proc.terminate.assert_not_called()
        self.assertIsNotNone(self.agent.process)
        self.assertTrue(self.agent.is_connected)

    async def test_disconnect_marks_interrupted(self):
        self.agent.is_connected = True
        self.agent._interrupted = False
        await self.agent.disconnect()
        self.assertTrue(self.agent._interrupted)
        self.assertFalse(self.agent.is_connected)

    async def test_send_message_plan_mode_injects_system_prompt(self):
        self.agent.plan_mode = True
        self.agent.options.plan_mode = True
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.returncode = None
        self.agent.process = mock_proc

        await self.agent.send_message("analyze code and create plan")
        mock_proc.stdin.write.assert_called_once()
        sent_line = mock_proc.stdin.write.call_args[0][0].decode("utf-8")
        payload = json.loads(sent_line)
        expected = f"{PLAN_MODE_SYSTEM_PROMPT}\n\nanalyze code and create plan"
        self.assertEqual(payload["message"]["content"], expected)

    async def test_send_message_fast_mode_does_not_inject_plan_directive(self):
        self.agent.plan_mode = False
        self.agent.options.plan_mode = False
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.returncode = None
        self.agent.process = mock_proc

        await self.agent.send_message("write a python script")
        mock_proc.stdin.write.assert_called_once()
        sent_line = mock_proc.stdin.write.call_args[0][0].decode("utf-8")
        payload = json.loads(sent_line)
        self.assertEqual(payload["message"]["content"], "write a python script")

    async def test_send_message_injects_options_system_prompt(self):
        self.agent.plan_mode = False
        self.agent.options.plan_mode = False
        self.agent.options.system_prompt = "Custom instructions for agent."
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.returncode = None
        self.agent.process = mock_proc

        await self.agent.send_message("hello")
        mock_proc.stdin.write.assert_called_once()
        sent_line = mock_proc.stdin.write.call_args[0][0].decode("utf-8")
        payload = json.loads(sent_line)
        expected = "Custom instructions for agent.\n\nhello"
        self.assertEqual(payload["message"]["content"], expected)

    async def test_send_message_proceed_plan(self):
        self.agent.plan_mode = True
        self.agent.options.plan_mode = True
        self.agent.options.approve_mode = "accept-all"

        old_proc = MagicMock()
        old_proc.returncode = None
        self.agent.process = old_proc
        await self.agent.message_queue.put(Message("stop"))

        with patch.object(self.agent, "_spawn_process", new_callable=AsyncMock) as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdin.write = MagicMock()
            mock_proc.stdin.drain = AsyncMock()
            mock_proc.returncode = None
            def _set_proc():
                self.agent.process = mock_proc
            mock_spawn.side_effect = _set_proc

            await self.agent.send_message("/plan Proceed with plan", proceed_plan=True)
            mock_spawn.assert_awaited_once()
            old_proc.terminate.assert_called_once()
            # Verified residual stop message was discarded
            self.assertTrue(self.agent.message_queue.empty())
            # Verified /plan prefix is stripped when proceeding
            sent_line = mock_proc.stdin.write.call_args[0][0].decode("utf-8")
            payload = json.loads(sent_line)
            self.assertEqual(payload["message"]["content"], "Proceed with plan")
        self.assertFalse(self.agent.plan_mode)
        self.assertFalse(self.agent.options.plan_mode)

    def test_dynamic_set_methods(self):
        self.agent.set_model("gemini-3.8-flash-high")
        self.assertEqual(self.agent.options.model, "gemini-3.8-flash-high")

        self.agent.set_think_level("low")
        self.assertEqual(self.agent.options.think_level, "low")

        self.agent.set_plan_mode(True)
        self.assertTrue(self.agent.plan_mode)
        self.assertTrue(self.agent.options.plan_mode)

        self.agent._session_id = "test-session"
        self.agent.options.session_id = "test-session"
        self.agent.new_session()
        self.assertIsNone(self.agent.session_id)
        self.assertIsNone(self.agent.options.session_id)

    async def test_receive_messages_abnormal_exit_with_stderr(self):
        self.agent._stderr_lines = [
            "I0911 19:00:00 server.go] Starting up",
            "Error: authentication required. Run 'agy' to log in, then retry.",
        ]
        self.agent.process.returncode = 1

        messages = []
        async for msg in self.agent.receive_messages():
            messages.append(msg)

        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].type, "error")
        self.assertIn("authentication required", messages[0].content)
        self.assertEqual(messages[1].type, "stop")

    async def test_interrupt_signals_and_queues_stop(self):
        with patch("sys.platform", "darwin"):
            await self.agent.interrupt()
        self.assertTrue(self.agent._interrupted)
        self.agent.process.send_signal.assert_called_once()
        self.assertFalse(self.agent.message_queue.empty())
        stop_msg = await self.agent.message_queue.get()
        self.assertEqual(stop_msg.type, "stop")

    async def test_receive_messages_handles_interrupted_exit_cleanly(self):
        self.agent._interrupted = True
        self.agent.process.returncode = 130
        # Put a trailing tool_use in the queue
        await self.agent.message_queue.put(Message("tool_use", content={"name": "run_command"}))

        received = []
        gen = self.agent.receive_messages()
        # Collect one message from the generator
        msg = await gen.__anext__()
        received.append(msg)

        # The trailing tool_use should have been discarded and replaced by guaranteed stop
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].type, "stop")
        self.assertFalse(self.agent._interrupted)
        self.assertIsNone(self.agent.process)

    async def test_send_message_resumes_session_after_process_exit(self):
        self.agent._session_id = "resumed-session-123"
        self.agent.process.returncode = 130

        with patch.object(self.agent, "_spawn_process", new_callable=AsyncMock) as mock_spawn:
            mock_proc = MagicMock()
            mock_proc.stdin = MagicMock()
            mock_proc.stdin.write = MagicMock()
            mock_proc.stdin.drain = AsyncMock()
            mock_proc.returncode = None
            def _set_proc():
                self.agent.process = mock_proc
            mock_spawn.side_effect = _set_proc

            await self.agent.send_message("New prompt after interrupt")
            mock_spawn.assert_awaited_once()
            self.assertEqual(self.agent.options.session_id, "resumed-session-123")
            mock_proc.stdin.write.assert_called_once()
            written = mock_proc.stdin.write.call_args[0][0].decode("utf-8")
            self.assertIn("New prompt after interrupt", written)


class TestAntigravitySessions(unittest.TestCase):
    def test_list_sessions_from_history_file(self):
        history_data = [
            {"conversationId": "c1", "display": "First prompt", "timestamp": 1700000000000, "workspace": "/workspace/a"},
            {"conversationId": "c2", "display": "Second prompt", "timestamp": 1700000100000, "workspace": "/workspace/b"},
            {"conversationId": "c3", "display": "Third prompt", "timestamp": 1700000200000, "workspace": "/workspace/a"},
        ]
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            for item in history_data:
                f.write(json.dumps(item) + "\n")
            f.flush()
            temp_path = f.name

        try:
            with patch("genfoundry.antigravity_agent._get_history_file", return_value=temp_path):
                sessions = list_antigravity_sessions("/workspace/a")
                self.assertEqual(len(sessions), 2)
                # Most recent first
                self.assertEqual(sessions[0]["session_id"], "c3")
                self.assertEqual(sessions[0]["summary"], "Third prompt")
                self.assertEqual(sessions[1]["session_id"], "c1")
        finally:
            os.remove(temp_path)

    def test_get_session_tail_from_transcript(self):
        transcript_lines = [
            {"type": "USER_INPUT", "content": "Initial question", "step_index": 0},
            {"type": "PLANNER_RESPONSE", "content": "Initial answer", "step_index": 1},
            {"type": "USER_INPUT", "content": "Follow-up question", "step_index": 2},
            {"type": "PLANNER_RESPONSE", "content": "Follow-up answer", "step_index": 3},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "brain", "conv-999", ".system_generated", "logs")
            os.makedirs(log_dir, exist_ok=True)
            transcript_file = os.path.join(log_dir, "transcript.jsonl")
            with open(transcript_file, "w") as f:
                for item in transcript_lines:
                    f.write(json.dumps(item) + "\n")

            with patch("genfoundry.antigravity_agent.list_antigravity_sessions", return_value=[{"session_id": "conv-999", "summary": "Conv 999", "mtime": 1700000000.0}]), \
                 patch("genfoundry.antigravity_agent._get_transcript_paths", return_value=[transcript_file]):
                tail = get_antigravity_session_tail("conv-999", "/workspace", history_limit=1)
                self.assertIsNotNone(tail)
                self.assertEqual(len(tail["turns"]), 1)
                self.assertEqual(tail["turns"][0]["prompt"], "Follow-up question")
                self.assertEqual(tail["turns"][0]["response"], "Follow-up answer")

    def test_get_session_tail_without_history_metadata(self):
        transcript_lines = [
            {"type": "USER_INPUT", "content": "Direct prompt", "step_index": 0},
            {"type": "PLANNER_RESPONSE", "content": "Direct response", "step_index": 1},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "brain", "conv-direct", ".system_generated", "logs")
            os.makedirs(log_dir, exist_ok=True)
            transcript_file = os.path.join(log_dir, "transcript.jsonl")
            with open(transcript_file, "w") as f:
                for item in transcript_lines:
                    f.write(json.dumps(item) + "\n")

            with patch("genfoundry.antigravity_agent.list_antigravity_sessions", return_value=[]), \
                 patch("genfoundry.antigravity_agent._get_transcript_paths", return_value=[transcript_file]):
                tail = get_antigravity_session_tail("conv-direct", "/workspace", history_limit=5)
                self.assertIsNotNone(tail)
                self.assertEqual(len(tail["turns"]), 1)
                self.assertEqual(tail["turns"][0]["prompt"], "Direct prompt")
                self.assertEqual(tail["summary"], "Direct prompt")


    def test_clean_antigravity_prompt(self):
        # Raw text without tags
        self.assertEqual(_clean_antigravity_prompt("Hello world"), "Hello world")
        # Text with <USER_REQUEST> wrapper and surrounding metadata
        tagged = (
            "<USER_REQUEST>\nexplain git diff\n</USER_REQUEST>\n"
            "<ADDITIONAL_METADATA>\ntime: 2026-09-11\n</ADDITIONAL_METADATA>\n"
            "<USER_SETTINGS_CHANGE>\nmodel changed\n</USER_SETTINGS_CHANGE>"
        )
        self.assertEqual(_clean_antigravity_prompt(tagged), "explain git diff")
        # Multiline prompt normalized to single line
        multiline = "<USER_REQUEST>\nline 1\n  line 2\n\nline 3\n</USER_REQUEST>"
        self.assertEqual(_clean_antigravity_prompt(multiline), "line 1 line 2 line 3")
        # Empty string
        self.assertEqual(_clean_antigravity_prompt(""), "")

    def test_transcript_paths_only_antigravity_cli(self):
        paths = _get_transcript_paths("test-conv-id")
        self.assertEqual(len(paths), 2)
        self.assertIn("transcript_full.jsonl", paths[0])
        self.assertIn("transcript.jsonl", paths[1])
        for p in paths:
            self.assertIn("antigravity-cli", p)
            self.assertNotIn("antigravity-ide", p)

    def test_list_sessions_from_brain_transcripts_reverse_chronological(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            meta_path = os.path.join(cache_dir, "conversation_metadata.json")
            data = {
                "conversations": {
                    "s1": {
                        "summary": {
                            "ID": "s1",
                            "Title": "Old first task",
                            "NumSteps": 2,
                            "UpdatedAt": "2026-10-16T10:00:00Z",
                            "WorkspaceURIs": ["file:///workspace/target"],
                        }
                    },
                    "s2": {
                        "summary": {
                            "ID": "s2",
                            "Title": "New second task",
                            "NumSteps": 3,
                            "UpdatedAt": "2026-10-16T12:00:00Z",
                            "WorkspaceURIs": ["file:///workspace/target"],
                        }
                    },
                }
            }
            with open(meta_path, "w") as f:
                json.dump(data, f)

            with patch("genfoundry.antigravity_agent._get_base_dir", return_value=tmpdir), \
                 patch(
                     "genfoundry.antigravity_agent._get_history_file",
                     return_value=os.path.join(tmpdir, "history.jsonl"),
                 ):
                sessions = list_antigravity_sessions("/workspace/target")
                self.assertEqual(len(sessions), 2)
                # Verify reverse chronological ordering (newest first)
                self.assertEqual(sessions[0]["session_id"], "s2")
                self.assertEqual(sessions[0]["summary"], "New second task")
                self.assertEqual(sessions[1]["session_id"], "s1")
                self.assertEqual(sessions[1]["summary"], "Old first task")
                self.assertGreater(sessions[0]["mtime"], sessions[1]["mtime"])

    def test_list_sessions_filters_by_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = os.path.join(tmpdir, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            meta_path = os.path.join(cache_dir, "conversation_metadata.json")
            data = {
                "conversations": {
                    "sA": {
                        "summary": {
                            "ID": "sA",
                            "Title": "Task A",
                            "NumSteps": 2,
                            "UpdatedAt": "2026-10-16T10:00:00Z",
                            "WorkspaceURIs": ["file:///workspace/a"],
                        }
                    },
                    "sB": {
                        "summary": {
                            "ID": "sB",
                            "Title": "Task B",
                            "NumSteps": 2,
                            "UpdatedAt": "2026-10-16T11:00:00Z",
                            "WorkspaceURIs": ["file:///workspace/b"],
                        }
                    },
                }
            }
            with open(meta_path, "w") as f:
                json.dump(data, f)

            with patch("genfoundry.antigravity_agent._get_base_dir", return_value=tmpdir), \
                 patch(
                     "genfoundry.antigravity_agent._get_history_file",
                     return_value=os.path.join(tmpdir, "history.jsonl"),
                 ):
                sessions_a = list_antigravity_sessions("/workspace/a")
                self.assertEqual(len(sessions_a), 1)
                self.assertEqual(sessions_a[0]["session_id"], "sA")

                sessions_all = list_antigravity_sessions(None)
                self.assertEqual(len(sessions_all), 2)

    def test_get_session_tail_cleans_prompt_tags(self):
        transcript_lines = [
            {"type": "USER_INPUT", "content": "<USER_REQUEST>\nTagged question\n</USER_REQUEST>", "step_index": 0},
            {"type": "PLANNER_RESPONSE", "content": "Tagged answer", "step_index": 1},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "brain", "conv-clean", ".system_generated", "logs")
            os.makedirs(log_dir, exist_ok=True)
            transcript_file = os.path.join(log_dir, "transcript.jsonl")
            with open(transcript_file, "w") as f:
                for item in transcript_lines:
                    f.write(json.dumps(item) + "\n")

            with patch("genfoundry.antigravity_agent.list_antigravity_sessions", return_value=[]), \
                 patch("genfoundry.antigravity_agent._get_transcript_paths", return_value=[transcript_file]):
                tail = get_antigravity_session_tail("conv-clean", "/workspace", history_limit=5)
                self.assertIsNotNone(tail)
                self.assertEqual(tail["turns"][0]["prompt"], "Tagged question")
                self.assertEqual(tail["summary"], "Tagged question")


class TestAntigravityModelLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.opts = AgentOptions(cli_path="/mock/bin/agy", cwd="/workspace")
        self.agent = AntigravityAgent(options=self.opts)
        self.agent.is_connected = True
        self.mock_proc = AsyncMock()
        self.mock_proc.terminate = MagicMock()
        self.mock_proc.stdin = MagicMock()
        self.mock_proc.stdin.drain = AsyncMock()
        self.mock_proc.returncode = None
        self.agent.process = self.mock_proc

    def test_set_model_marks_restart_pending(self):
        self.assertFalse(self.agent._restart_pending)
        self.agent.set_model("claude-sonnet-5-5")
        self.assertEqual(self.agent.options.model, "claude-sonnet-5-5")
        self.assertTrue(self.agent._restart_pending)

    def test_set_think_level_marks_restart_pending(self):
        self.assertFalse(self.agent._restart_pending)
        self.agent.set_think_level("high")
        self.assertEqual(self.agent.options.think_level, "high")
        self.assertTrue(self.agent._restart_pending)

    def test_new_session_clears_session_and_marks_restart_pending(self):
        self.agent._session_id = "test-session-123"
        self.agent.options.session_id = "test-session-123"
        self.agent.new_session()
        self.assertIsNone(self.agent._session_id)
        self.assertIsNone(self.agent.options.session_id)
        self.assertTrue(self.agent._restart_pending)
        self.mock_proc.terminate.assert_not_called()
        self.assertIsNotNone(self.agent.process)

    def test_parse_models_output(self):
        raw = (
            "⠋ Fetching available models...gemini-3.8-flash-high     Gemini 3.8 Flash (High)\n"
            "gemini-3.8-flash-medium   Gemini 3.8 Flash (Medium)\n"
            "gemini-3.8-flash-low      Gemini 3.8 Flash (Low)\n"
            "claude-sonnet-5-5-high    Claude Sonnet 5.5 (High)\n"
            "custom-tool-model         Custom Tool Model\n"
        )
        parsed = AntigravityAgent._parse_models_output(raw)
        self.assertEqual(len(parsed), 3)

        flash = next((m for m in parsed if m["modelId"] == "gemini-3.8-flash"), None)
        self.assertIsNotNone(flash)
        self.assertTrue(flash["supportsEffort"])
        self.assertEqual(
            flash["supportedReasoningEfforts"], ["high", "medium", "low"]
        )
        self.assertEqual(flash["displayName"], "Gemini 3.8 Flash")

        sonnet = next((m for m in parsed if m["modelId"] == "claude-sonnet-5-5"), None)
        self.assertIsNotNone(sonnet)
        self.assertTrue(sonnet["supportsEffort"])
        self.assertEqual(sonnet["supportedReasoningEfforts"], ["high"])

        custom = next((m for m in parsed if m["modelId"] == "custom-tool-model"), None)
        self.assertIsNotNone(custom)
        self.assertFalse(custom["supportsEffort"])

    def test_model_supports_effort(self):
        self.agent.available_models = [
            {"modelId": "model-with-effort", "supportsEffort": True},
            {"modelId": "model-without-effort", "supportsEffort": False},
        ]
        self.assertTrue(self.agent._model_supports_effort("model-with-effort"))
        self.assertFalse(self.agent._model_supports_effort("model-without-effort"))
        # Cleaned suffix
        self.assertTrue(self.agent._model_supports_effort("model-with-effort-high"))
        # Fallback heuristic
        self.assertTrue(self.agent._model_supports_effort("gemini-3.8-flash"))
        self.assertTrue(self.agent._model_supports_effort("claude-sonnet-5-5"))
        self.assertFalse(self.agent._model_supports_effort("default"))
        self.assertFalse(self.agent._model_supports_effort(None))

    async def test_spawn_process_strips_effort_suffix(self):
        opts = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            model="gemini-3.8-flash-high",
            think_level=None,
        )
        agent = AntigravityAgent(options=opts)
        fake_proc = AsyncMock()
        fake_proc.returncode = None
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec, \
             patch.object(agent, "_read_stdout", new_callable=AsyncMock), \
             patch.object(agent, "_read_stderr", new_callable=AsyncMock):
            await agent._spawn_process()
            cmd = mock_exec.call_args[0]
            self.assertIn("--model", cmd)
            idx_m = cmd.index("--model")
            self.assertEqual(cmd[idx_m + 1], "gemini-3.8-flash")
            self.assertIn("--effort", cmd)
            idx_e = cmd.index("--effort")
            self.assertEqual(cmd[idx_e + 1], "high")

    async def test_spawn_process_overrides_effort_from_think_level(self):
        opts = AgentOptions(
            cli_path="/mock/bin/agy",
            cwd="/workspace",
            model="gemini-3.8-flash-high",
            think_level="low",
        )
        agent = AntigravityAgent(options=opts)
        fake_proc = AsyncMock()
        fake_proc.returncode = None
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc) as mock_exec, \
             patch.object(agent, "_read_stdout", new_callable=AsyncMock), \
             patch.object(agent, "_read_stderr", new_callable=AsyncMock):
            await agent._spawn_process()
            cmd = mock_exec.call_args[0]
            idx_e = cmd.index("--effort")
            self.assertEqual(cmd[idx_e + 1], "low")

    async def test_fetch_models_updates_available_models_and_emits_message(self):
        mock_output = b"gemini-3.8-flash-high Gemini 3.8 Flash (High)\n"
        fake_proc = AsyncMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(mock_output, b""))

        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await self.agent._fetch_models()

        self.assertEqual(len(self.agent.available_models), 1)
        self.assertEqual(
            self.agent.available_models[0]["modelId"], "gemini-3.8-flash"
        )
        self.assertFalse(self.agent.message_queue.empty())
        msg = await self.agent.message_queue.get()
        self.assertEqual(msg.type, "models_update")
        self.assertEqual(len(msg.content["models"]), 1)


class TestAntigravityMetadataAndSessionDiscovery(unittest.TestCase):
    def test_parse_iso_timestamp(self):
        ts = "2026-10-16T15:00:00Z"
        val = _parse_iso_timestamp(ts)
        self.assertGreater(val, 0.0)

        # Microseconds handling
        ts_micro = "2026-10-16T15:00:00.123456Z"
        val_micro = _parse_iso_timestamp(ts_micro)
        self.assertGreater(val_micro, val)

        # Invalid returns 0.0
        self.assertEqual(_parse_iso_timestamp("invalid"), 0.0)

    def test_is_workspace_match(self):
        ws = "/path/to/project"
        norm_cwd = os.path.normcase(os.path.realpath("/path/to/project"))
        self.assertTrue(_is_workspace_match(ws, norm_cwd))

        # Subdirectory match
        norm_sub = os.path.normcase(os.path.realpath("/path/to/project/src/sub"))
        self.assertTrue(_is_workspace_match(ws, norm_sub))

        # Parent directory match
        norm_parent = os.path.normcase(os.path.realpath("/path/to"))
        self.assertTrue(_is_workspace_match(ws, norm_parent))

        # Unrelated path
        norm_other = os.path.normcase(os.path.realpath("/other/project"))
        self.assertFalse(_is_workspace_match(ws, norm_other))
        self.assertFalse(_is_workspace_match("", norm_cwd))

    def test_read_transcript_summary_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cid = "sess-direct-1"
            # When transcript exists
            log_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
            os.makedirs(log_dir, exist_ok=True)
            t_path = os.path.join(log_dir, "transcript.jsonl")
            with open(t_path, "w") as f:
                f.write(json.dumps({
                    "type": "USER_INPUT",
                    "content": "Optimize search index query",
                }) + "\n")

            with patch("genfoundry.antigravity_agent._get_base_dir", return_value=tmp_dir):
                summary, mtime = _read_transcript_summary_and_mtime(cid)
                self.assertEqual(summary, "Optimize search index query")
                self.assertGreater(mtime, 0.0)

    def test_list_antigravity_sessions_metadata_multi_workspace_and_title(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = os.path.join(tmp_dir, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            meta_path = os.path.join(cache_dir, "conversation_metadata.json")

            ws_alpha = os.path.join(tmp_dir, "workspace_alpha")
            ws_beta = os.path.join(tmp_dir, "workspace_beta")
            ws_gamma = os.path.join(tmp_dir, "workspace_gamma")
            ws_unrelated = os.path.join(tmp_dir, "workspace_unrelated")
            for w in (ws_alpha, ws_beta, ws_gamma, ws_unrelated):
                os.makedirs(w, exist_ok=True)

            data = {
                "conversations": {
                    "sess-1": {
                        "summary": {
                            "ID": "sess-1",
                            "Title": "Multi Root Project Work",
                            "NumSteps": 5,
                            "UpdatedAt": "2026-10-16T15:00:00Z",
                            "WorkspaceURIs": [
                                f"file://{ws_alpha}",
                                f"file://{ws_beta}",
                                f"file://{ws_gamma}",
                            ],
                        }
                    },
                    "sess-empty": {
                        "summary": {
                            "ID": "sess-empty",
                            "Title": "Empty Session",
                            "NumSteps": 0,
                            "UpdatedAt": "2026-10-16T15:00:00Z",
                            "WorkspaceURIs": [f"file://{ws_beta}"],
                        }
                    },
                }
            }
            with open(meta_path, "w") as f:
                json.dump(data, f)

            hist_file = os.path.join(tmp_dir, "history.jsonl")
            with patch("genfoundry.antigravity_agent._get_base_dir", return_value=tmp_dir), \
                 patch("genfoundry.antigravity_agent._get_history_file", return_value=hist_file):
                # Query with second workspace: ws_beta (should find sess-1)
                results_beta = list_antigravity_sessions(ws_beta)
                self.assertEqual(len(results_beta), 1)
                self.assertEqual(results_beta[0]["session_id"], "sess-1")
                self.assertEqual(results_beta[0]["summary"], "Multi Root Project Work")
                self.assertEqual(results_beta[0]["steps"], 5)

                # Query with third workspace: ws_gamma (should also find sess-1)
                results_gamma = list_antigravity_sessions(ws_gamma)
                self.assertEqual(len(results_gamma), 1)
                self.assertEqual(results_gamma[0]["session_id"], "sess-1")

                # Query with unrelated workspace: should return 0
                results_unrelated = list_antigravity_sessions(ws_unrelated)
                self.assertEqual(len(results_unrelated), 0)

                # Query with None (all): returns sess-1, skips sess-empty (0 steps)
                results_all = list_antigravity_sessions(None)
                self.assertEqual(len(results_all), 1)
                self.assertEqual(results_all[0]["session_id"], "sess-1")


if __name__ == "__main__":
    unittest.main()
