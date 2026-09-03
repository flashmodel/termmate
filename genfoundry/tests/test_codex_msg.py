"""
Test CodexAgent two-turn conversation.

Mocks the codex app-server subprocess to verify that CodexAgent can
send two rounds of messages and receive the expected responses.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.codex_agent import CodexAgent
from genfoundry.base_agent import AgentOptions, MessageType


class FakeProcess:
    """Simulates a codex app-server subprocess with scripted JSON-RPC responses."""

    def __init__(self):
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.returncode = None

        self._stdout_lines: list[bytes] = []
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
        # stderr: never produces output
        await asyncio.sleep(10)
        return b""

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        pass


class TestCodexTwoTurns(unittest.IsolatedAsyncioTestCase):

    async def _create_agent(self, fake_proc: FakeProcess) -> CodexAgent:
        """Create a CodexAgent with a fake subprocess."""
        opts = AgentOptions(cli_path="/usr/bin/true")
        agent = CodexAgent(options=opts)

        # Intercept _write_json to capture outgoing requests and auto-reply
        original_write = agent._write_json
        self._rpc_calls: list[dict] = []

        async def mock_write_json(data: dict):
            self._rpc_calls.append(data)
            await original_write(data)

            # Auto-reply to JSON-RPC requests (those with an 'id')
            if "id" in data:
                method = data.get("method", "")
                rid = data["id"]
                if method == "initialize":
                    fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                elif method == "thread/start":
                    fake_proc.feed({"id": rid, "result": {"thread": {"id": "thread-001"}}})
                elif method == "turn/start":
                    turn_id = f"turn-{rid}"
                    fake_proc.feed({"id": rid, "result": {}})
                    # Simulate agent response events
                    fake_proc.feed({"method": "turn/started", "params": {"turnId": turn_id}})
                    fake_proc.feed({
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": f"msg-{rid}", "delta": f"Reply to turn {rid}"},
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "agentMessage",
                                "id": f"msg-{rid}",
                                "text": f"Reply to turn {rid}",
                            }
                        },
                    })
                    fake_proc.feed({"method": "turn/completed", "params": {"turnId": turn_id}})

        agent._write_json = mock_write_json

        # Patch create_subprocess_exec to return our fake process
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()

        return agent

    async def _collect_turn(self, agent: CodexAgent) -> list:
        """Collect messages from receive_messages until STOP."""
        messages = []
        async for msg in agent.receive_messages():
            messages.append(msg)
            if getattr(msg, "type", None) == MessageType.STOP.value:
                break
        return messages

    async def test_two_turn_conversation(self):
        """Verify that sending two messages produces two separate turn responses."""
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        try:
            # --- Turn 1 ---
            await agent.send_message("Hello, first message")
            msgs1 = await self._collect_turn(agent)

            text_msgs1 = [m for m in msgs1 if getattr(m, "type", None) == MessageType.TEXT.value]
            stop_msgs1 = [m for m in msgs1 if getattr(m, "type", None) == MessageType.STOP.value]

            self.assertTrue(len(text_msgs1) > 0, "Turn 1 should have at least one text message")
            self.assertEqual(len(stop_msgs1), 1, "Turn 1 should have exactly one STOP")
            self.assertIn("Reply to turn", text_msgs1[0].content)

            # --- Turn 2 ---
            await agent.send_message("Hello, second message")
            msgs2 = await self._collect_turn(agent)

            text_msgs2 = [m for m in msgs2 if getattr(m, "type", None) == MessageType.TEXT.value]
            stop_msgs2 = [m for m in msgs2 if getattr(m, "type", None) == MessageType.STOP.value]

            self.assertTrue(len(text_msgs2) > 0, "Turn 2 should have at least one text message")
            self.assertEqual(len(stop_msgs2), 1, "Turn 2 should have exactly one STOP")
            self.assertIn("Reply to turn", text_msgs2[0].content)

            # Verify it's a different reply (different RPC id)
            self.assertNotEqual(text_msgs1[0].content, text_msgs2[0].content,
                                "Two turns should produce different replies")

            # Verify thread was reused
            self.assertEqual(agent.thread_id, "thread-001")

        finally:
            await agent.disconnect()

    async def test_thread_id_persists_across_turns(self):
        """Verify thread_id stays the same for both turns."""
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        try:
            tid_before = agent.thread_id
            self.assertEqual(tid_before, "thread-001")

            await agent.send_message("First")
            await self._collect_turn(agent)

            self.assertEqual(agent.thread_id, tid_before)

            await agent.send_message("Second")
            await self._collect_turn(agent)

            self.assertEqual(agent.thread_id, tid_before)

        finally:
            await agent.disconnect()

    async def test_steer_plan(self):
        """Verify that steer() sends a turn/start RPC (via send_message)."""
        fake_proc = FakeProcess()
        opts = AgentOptions(cli_path="/usr/bin/true")
        agent = CodexAgent(options=opts)
        self._rpc_calls = []

        async def mock_write_json(data: dict):
            self._rpc_calls.append(data)
            rid = data.get("id")
            method = data.get("method")
            if rid is not None:
                if method == "initialize":
                    fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                elif method == "thread/start":
                    fake_proc.feed({"id": rid, "result": {"thread": {"id": "thread-001"}}})
                elif method == "turn/start":
                    fake_proc.feed({"id": rid, "result": {}})

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()
            try:
                agent._active_turn_id = "turn-XYZ"
                await agent.steer("Implement this plan")
                steer_call = next((c for c in self._rpc_calls if c.get("method") == "turn/start"), None)
                self.assertIsNotNone(steer_call)
                self.assertEqual(steer_call["params"]["input"][0]["text"], "Implement this plan")
                self.assertEqual(steer_call["params"]["expectedTurnId"], "turn-XYZ")
                self.assertEqual(steer_call["params"]["threadId"], "thread-001")
            finally:
                await agent.disconnect()

    async def test_command_execution_handling(self):
        """Verify that command execution extracts clean command and avoids duplication."""
        fake_proc = FakeProcess()
        opts = AgentOptions(cli_path="/usr/bin/true")
        agent = CodexAgent(options=opts)

        rpc_responses = []
        async def mock_write_json(data: dict):
            rid = data.get("id")
            method = data.get("method")
            if rid is not None:
                if method == "initialize":
                    fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                elif method == "thread/start":
                    fake_proc.feed({"id": rid, "result": {"thread": {"id": "thread-001"}}})
                elif method == "turn/start":
                    fake_proc.feed({"id": rid, "result": {}})
                    # Simulate command execution events
                    fake_proc.feed({
                        "method": "item/started",
                        "params": {
                            "item": {
                                "type": "commandExecution",
                                "id": "cmd-1",
                                "command": "/bin/zsh -lc 'ls'",
                                "commandActions": [{"type": "listFiles", "command": "ls"}]
                            }
                        }
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "commandExecution",
                                "id": "cmd-1",
                                "command": "/bin/zsh -lc 'ls'",
                                "status": "completed"
                            }
                        }
                    })
                    fake_proc.feed({"method": "turn/completed", "params": {"turnId": "turn-1"}})

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()
            try:
                await agent.send_message("Run ls")
                messages = await self._collect_turn(agent)

                # Filter for TOOL_USE messages
                tool_use_msgs = [m for m in messages if getattr(m, "type", None) == MessageType.TOOL_USE.value]

                # Should only have ONE ToolUse message for the command
                self.assertEqual(len(tool_use_msgs), 1, "Should only have one TOOL_USE message for commandExecution")

                # The command should be the clean one from commandActions
                self.assertEqual(tool_use_msgs[0].content["command"], "ls")
                self.assertEqual(tool_use_msgs[0].content["name"], "command_execution")

            finally:
                await agent.disconnect()

    async def test_completed_tool_items_are_forwarded_for_formatting(self):
        agent = CodexAgent(options=AgentOptions(cli_path="/usr/bin/true"))
        items = [
            {
                "type": "mcpToolCall",
                "id": "mcp-1",
                "server": "github",
                "tool": "search_repositories",
                "arguments": {"query": "codex"},
                "status": "completed",
            },
            {
                "type": "dynamicToolCall",
                "id": "dynamic-1",
                "namespace": "design",
                "tool": "choose",
                "arguments": {"theme": "editorial"},
                "status": "completed",
            },
            {
                "type": "collabAgentToolCall",
                "id": "collab-1",
                "tool": "spawnAgent",
                "receiverThreadIds": ["thread-2"],
                "prompt": "Inspect tests",
                "model": "gpt-5",
                "reasoningEffort": "high",
                "status": "completed",
            },
            {
                "type": "webSearch",
                "id": "search-1",
                "query": "Codex app-server protocol",
                "action": {"type": "search"},
            },
            {
                "type": "imageView",
                "id": "image-view-1",
                "path": "/tmp/preview.png",
            },
            {
                "type": "imageGeneration",
                "id": "image-generation-1",
                "status": "completed",
                "result": "ignored",
            },
        ]

        for item in items:
            await agent._handle_item_completed({"item": item})

        messages = [await agent._message_queue.get() for _ in items]

        self.assertEqual(
            [message.content["name"] for message in messages],
            [
                "mcpToolCall",
                "dynamicToolCall",
                "collabAgentToolCall",
                "webSearch",
                "imageView",
                "imageGeneration",
            ],
        )
        self.assertEqual(
            messages[0].content,
            {
                "name": "mcpToolCall",
                "server": "github",
                "tool": "search_repositories",
                "arguments": {"query": "codex"},
                "status": "completed",
            },
        )
        self.assertNotIn("result", messages[-1].content)

    def test_find_git_dirs(self):
        from genfoundry.codex_agent import find_git_dirs
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            # 1. Root .git
            root_git = os.path.join(temp_dir, ".git")
            os.makedirs(root_git)

            # 2. Submodule .git
            sub_dir = os.path.join(temp_dir, "pkg", "subpkg")
            os.makedirs(os.path.join(sub_dir, ".git"))

            # 3. Worktree pointer file with commondir
            worktree_dir = os.path.join(temp_dir, "pkg", "worktree")
            os.makedirs(worktree_dir)
            target_git_dir = os.path.join(temp_dir, "real_git_dir", "worktrees", "wt1")
            common_git_dir = os.path.join(temp_dir, "real_git_dir")
            os.makedirs(target_git_dir)
            with open(os.path.join(worktree_dir, ".git"), "w") as f:
                f.write(f"gitdir: {target_git_dir}\n")
            with open(os.path.join(target_git_dir, "commondir"), "w") as f:
                f.write("../..\n")

            dirs = find_git_dirs(temp_dir)
            self.assertIn(root_git, dirs)
            self.assertIn(os.path.join(sub_dir, ".git"), dirs)
            self.assertIn(target_git_dir, dirs)
            self.assertIn(common_git_dir, dirs)
        finally:
            shutil.rmtree(temp_dir)

    async def test_thread_start_sandbox_parameter(self):
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)
        try:
            start_call = next((c for c in self._rpc_calls if c.get("method") == "thread/start"), None)
            self.assertIsNotNone(start_call)
            params = start_call.get("params", {})
            # sandbox must be a string enum, never a dict
            self.assertIsInstance(params.get("sandbox"), str)
            self.assertEqual(params.get("sandbox"), "workspace-write")
        finally:
            await agent.disconnect()


if __name__ == "__main__":
    unittest.main()

