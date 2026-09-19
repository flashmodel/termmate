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
    find_antigravity_cli,
    list_antigravity_sessions,
    get_antigravity_session_tail,
    _clean_antigravity_prompt,
    _get_transcript_paths,
    _get_base_dir,
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

        self.assertEqual(messages[1].type, "tool_use")
        self.assertEqual(messages[1].content["id"], "call_1")
        self.assertEqual(messages[1].content["output"], "file1.txt\nfile2.txt")

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

    async def test_send_message_proceed_plan(self):
        self.agent.plan_mode = True
        self.agent.options.plan_mode = True
        await self.agent.send_message("Proceed with plan", proceed_plan=True)
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
            brain_dir = os.path.join(tmpdir, "brain")
            # Session 1: Older mtime
            s1_log_dir = os.path.join(brain_dir, "s1", ".system_generated", "logs")
            os.makedirs(s1_log_dir, exist_ok=True)
            s1_file = os.path.join(s1_log_dir, "transcript.jsonl")
            with open(s1_file, "w") as f:
                f.write(json.dumps({
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>\nOld first task\n</USER_REQUEST>",
                    "step_index": 0,
                }) + "\n")
                f.write(json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [{"name": "run_command", "args": {"Cwd": "/workspace/target"}}],
                    "step_index": 1,
                }) + "\n")
            os.utime(s1_file, (1700000000.0, 1700000000.0))

            # Session 2: Newer mtime
            s2_log_dir = os.path.join(brain_dir, "s2", ".system_generated", "logs")
            os.makedirs(s2_log_dir, exist_ok=True)
            s2_file = os.path.join(s2_log_dir, "transcript.jsonl")
            with open(s2_file, "w") as f:
                f.write(json.dumps({
                    "type": "USER_INPUT",
                    "content": "<USER_REQUEST>\nNew second task\n</USER_REQUEST>",
                    "step_index": 0,
                }) + "\n")
                f.write(json.dumps({
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [{"name": "run_command", "args": {"Cwd": "/workspace/target"}}],
                    "step_index": 1,
                }) + "\n")
            os.utime(s2_file, (1700000500.0, 1700000500.0))

            with patch("genfoundry.antigravity_agent._get_base_dir", return_value=tmpdir), \
                 patch("genfoundry.antigravity_agent._get_history_file", return_value=os.path.join(tmpdir, "history.jsonl")):
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
            brain_dir = os.path.join(tmpdir, "brain")
            # Session for workspace A
            sA_dir = os.path.join(brain_dir, "sA", ".system_generated", "logs")
            os.makedirs(sA_dir, exist_ok=True)
            sA_file = os.path.join(sA_dir, "transcript.jsonl")
            with open(sA_file, "w") as f:
                f.write(json.dumps({"type": "USER_INPUT", "content": "Task A"}) + "\n")
                f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"Cwd": "/workspace/a"}}]}) + "\n")

            # Session for workspace B
            sB_dir = os.path.join(brain_dir, "sB", ".system_generated", "logs")
            os.makedirs(sB_dir, exist_ok=True)
            sB_file = os.path.join(sB_dir, "transcript.jsonl")
            with open(sB_file, "w") as f:
                f.write(json.dumps({"type": "USER_INPUT", "content": "Task B"}) + "\n")
                f.write(json.dumps({"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"Cwd": "/workspace/b"}}]}) + "\n")

            with patch("genfoundry.antigravity_agent._get_base_dir", return_value=tmpdir), \
                 patch("genfoundry.antigravity_agent._get_history_file", return_value=os.path.join(tmpdir, "history.jsonl")):
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


if __name__ == "__main__":
    unittest.main()
