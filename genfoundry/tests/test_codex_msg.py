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

    async def _create_agent(self, fake_proc: FakeProcess, opts: AgentOptions = None) -> CodexAgent:
        """Create a CodexAgent with a fake subprocess."""
        if opts is None:
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
                elif method == "thread/resume":
                    t_id = data.get("params", {}).get("threadId", "thread-001")
                    fake_proc.feed({"id": rid, "result": {"thread": {"id": t_id}}})
                elif method == "thread/turns/list":
                    fake_proc.feed({"id": rid, "result": {"data": [], "nextCursor": None}})
                elif method == "turn/start":
                    turn_id = f"turn-{rid}"
                    fake_proc.feed({
                        "id": rid,
                        "result": {
                            "turn": {
                                "id": turn_id,
                                "status": "inProgress",
                                "items": [],
                            }
                        },
                    })
                    # Simulate agent response events
                    fake_proc.feed({
                        "method": "turn/started",
                        "params": {
                            "threadId": "thread-001",
                            "turn": {
                                "id": turn_id,
                                "rootTurnId": turn_id,
                                "status": "inProgress",
                            },
                        },
                    })
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
                    fake_proc.feed({
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-001",
                            "turn": {
                                "id": turn_id,
                                "rootTurnId": turn_id,
                                "status": "completed",
                            },
                        },
                    })
                elif method == "thread/revert":
                    fake_proc.feed({"id": rid, "result": {"thread": {"id": "thread-001"}}})

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

    async def test_v1_legacy_turn_fallback(self):
        """Verify backward compatibility when server uses v1 flat turnId and empty turn/start."""
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
                    # Legacy v1: empty result dictionary
                    fake_proc.feed({"id": rid, "result": {}})
                    # Legacy v1: flat turnId in turn/started notification
                    fake_proc.feed({
                        "method": "turn/started",
                        "params": {"turnId": "legacy-turn-1"},
                    })
                    fake_proc.feed({
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "msg-1", "delta": "Legacy response text"},
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "type": "agentMessage",
                                "id": "msg-1",
                                "text": "Legacy response text",
                            }
                        },
                    })
                    # Legacy v1: flat turnId in turn/completed notification
                    fake_proc.feed({
                        "method": "turn/completed",
                        "params": {"turnId": "legacy-turn-1"},
                    })

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()
            try:
                await agent.send_message("Legacy test")
                messages = await self._collect_turn(agent)

                # Verify turn tracking via fallback extraction
                self.assertEqual(agent._turn_count, 1)
                self.assertEqual(agent._turn_ids, ["legacy-turn-1"])
                self.assertEqual(agent._active_turn_id, "legacy-turn-1")
                self.assertFalse(agent._is_turn_active)

                types = [m.type for m in messages]
                self.assertEqual(types.count("turn_started"), 1)
                self.assertEqual(types[-1], "stop")
                text_msgs = [m for m in messages if getattr(m, "type", None) == "text"]
                self.assertTrue(len(text_msgs) > 0)
                self.assertEqual(text_msgs[0].content, "Legacy response text")
            finally:
                await agent.disconnect()

    async def test_v2_turn_full_lifecycle(self):
        """Verify full v2 turn lifecycle with reasoning, commentary, mcp, and final answer."""
        fake_proc = FakeProcess()
        opts = AgentOptions(cli_path="/usr/bin/true")
        agent = CodexAgent(options=opts)
        self._rpc_calls = []

        turn_id = "01a18b0f-0120-7783-88c2-255ae09d8ee7"
        thread_id = "01a18b0c-f529-77a0-a8f1-f69d17cbb0db"

        async def mock_write_json(data: dict):
            self._rpc_calls.append(data)
            rid = data.get("id")
            method = data.get("method")
            if rid is not None:
                if method == "initialize":
                    fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                elif method == "thread/start":
                    fake_proc.feed({
                        "id": rid,
                        "result": {"thread": {"id": thread_id}},
                    })
                elif method == "turn/start":
                    # v2 standard result structure
                    fake_proc.feed({
                        "id": rid,
                        "result": {
                            "turn": {
                                "id": turn_id,
                                "status": "inProgress",
                                "items": [],
                            }
                        },
                    })
                    # 1. thread/status/changed -> active
                    fake_proc.feed({
                        "method": "thread/status/changed",
                        "params": {"threadId": thread_id, "status": {"type": "active"}},
                    })
                    # 2. turn/started with nested turn object
                    fake_proc.feed({
                        "method": "turn/started",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "rootTurnId": turn_id,
                                "status": "inProgress",
                            },
                        },
                    })
                    # 3. userMessage item events
                    fake_proc.feed({
                        "method": "item/started",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "userMessage",
                                "id": "u-1",
                                "content": [{"type": "text", "text": "TPU analysis"}],
                            },
                        },
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "userMessage",
                                "id": "u-1",
                                "content": [{"type": "text", "text": "TPU analysis"}],
                            },
                        },
                    })
                    # 4. reasoning item events with textDelta
                    fake_proc.feed({
                        "method": "item/started",
                        "params": {
                            "threadId": thread_id,
                            "item": {"type": "reasoning", "id": "rs-1", "summary": []},
                        },
                    })
                    fake_proc.feed({
                        "method": "item/reasoning/textDelta",
                        "params": {
                            "itemId": "rs-1",
                            "delta": "Reasoning about TPU market...",
                        },
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "threadId": thread_id,
                            "item": {"type": "reasoning", "id": "rs-1", "summary": []},
                        },
                    })
                    # 5. commentary agentMessage item (pre-tool thoughts)
                    fake_proc.feed({
                        "method": "item/started",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "agentMessage",
                                "id": "msg-comm",
                                "text": "",
                                "phase": "commentary",
                            },
                        },
                    })
                    fake_proc.feed({
                        "method": "item/agentMessage/delta",
                        "params": {
                            "itemId": "msg-comm",
                            "delta": "Searching latest 2026 filings...",
                        },
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "agentMessage",
                                "id": "msg-comm",
                                "text": "Searching latest 2026 filings...",
                                "phase": "commentary",
                            },
                        },
                    })
                    # 6. mcpToolCall item (e.g. search_service.web_run)
                    fake_proc.feed({
                        "method": "item/started",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "mcpToolCall",
                                "id": "exec-1",
                                "server": "codex_apps",
                                "tool": "search_service.web_run",
                                "status": "inProgress",
                            },
                        },
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "mcpToolCall",
                                "id": "exec-1",
                                "server": "codex_apps",
                                "tool": "search_service.web_run",
                                "status": "completed",
                                "arguments": {"query": "Google Cloud TPU revenue"},
                                "result": {
                                    "content": [{"type": "text", "text": "Estimates 70B"}]
                                },
                            },
                        },
                    })
                    # 7. final_answer agentMessage item
                    fake_proc.feed({
                        "method": "item/started",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "agentMessage",
                                "id": "msg-final",
                                "text": "",
                                "phase": "final_answer",
                            },
                        },
                    })
                    fake_proc.feed({
                        "method": "item/agentMessage/delta",
                        "params": {
                            "itemId": "msg-final",
                            "delta": "Estimated TPU revenue is 600-750B.",
                        },
                    })
                    fake_proc.feed({
                        "method": "item/completed",
                        "params": {
                            "threadId": thread_id,
                            "item": {
                                "type": "agentMessage",
                                "id": "msg-final",
                                "text": "Estimated TPU revenue is 600-750B.",
                                "phase": "final_answer",
                            },
                        },
                    })
                    # 8. thread/tokenUsage/updated
                    fake_proc.feed({
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "tokenUsage": {
                                "total": {
                                    "totalTokens": 409378,
                                    "inputTokens": 405641,
                                    "cachedInputTokens": 327001,
                                    "outputTokens": 3737,
                                    "reasoningOutputTokens": 1133,
                                },
                                "last": {
                                    "totalTokens": 81037,
                                    "inputTokens": 78616,
                                    "cachedInputTokens": 70459,
                                    "outputTokens": 2421,
                                    "reasoningOutputTokens": 716,
                                },
                                "modelContextWindow": 258400,
                            },
                        },
                    })
                    # 9. thread/readState/changed & thread/status/changed -> idle
                    fake_proc.feed({
                        "method": "thread/readState/changed",
                        "params": {
                            "threadId": thread_id,
                            "readState": {
                                "firstUnread": {"type": "turn", "turnId": turn_id},
                                "revision": "rev-001",
                            },
                        },
                    })
                    fake_proc.feed({
                        "method": "thread/status/changed",
                        "params": {"threadId": thread_id, "status": {"type": "idle"}},
                    })
                    # 10. turn/completed
                    fake_proc.feed({
                        "method": "turn/completed",
                        "params": {
                            "threadId": thread_id,
                            "turn": {
                                "id": turn_id,
                                "rootTurnId": turn_id,
                                "status": "completed",
                            },
                        },
                    })

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()
            try:
                await agent.send_message("Analyze TPU revenue")
                messages = await self._collect_turn(agent)

                # State verification
                self.assertEqual(agent.thread_id, thread_id)
                self.assertEqual(agent._turn_count, 1)
                self.assertEqual(agent._turn_ids, [turn_id])
                self.assertEqual(agent._active_turn_id, turn_id)
                self.assertFalse(agent._is_turn_active)

                # Message stream verification
                types = [m.type for m in messages]
                self.assertIn("turn_started", types)
                self.assertIn(MessageType.THINKING.value, types)
                self.assertIn(MessageType.TOOL_USE.value, types)
                self.assertIn(MessageType.TOKEN_USAGE.value, types)
                self.assertEqual(types[-1], MessageType.STOP.value)

                # Verify MCP tool message formatting
                mcp_msgs = [m for m in messages if m.type == MessageType.TOOL_USE.value]
                self.assertEqual(len(mcp_msgs), 1)
                self.assertEqual(mcp_msgs[0].content["name"], "mcpToolCall")
                self.assertEqual(mcp_msgs[0].content["server"], "codex_apps")
                self.assertEqual(mcp_msgs[0].content["tool"], "search_service.web_run")
                self.assertEqual(
                    mcp_msgs[0].content["arguments"],
                    {"query": "Google Cloud TPU revenue"},
                )

                # Verify token usage values
                token_msgs = [m for m in messages if m.type == MessageType.TOKEN_USAGE.value]
                self.assertEqual(len(token_msgs), 1)
                self.assertEqual(token_msgs[0].content["total"]["totalTokens"], 409378)
            finally:
                await agent.disconnect()

    async def test_steer_plan(self):
        """Verify that steer() sends a turn/steer RPC when turn is active."""
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
                elif method == "thread/resume":
                    t_id = data.get("params", {}).get("threadId", "thread-001")
                    fake_proc.feed({"id": rid, "result": {"thread": {"id": t_id}}})
                elif method == "thread/turns/list":
                    fake_proc.feed({"id": rid, "result": {"data": [], "nextCursor": None}})
                elif method == "turn/start":
                    fake_proc.feed({"id": rid, "result": {}})
                elif method == "turn/steer":
                    fake_proc.feed({"id": rid, "result": {}})

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()
            try:
                agent._active_turn_id = "turn-XYZ"
                agent._is_turn_active = True
                await agent.steer("Implement this plan")
                steer_call = next(
                    (c for c in self._rpc_calls if c.get("method") == "turn/steer"), None
                )
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
                elif method == "thread/resume":
                    t_id = data.get("params", {}).get("threadId", "thread-001")
                    fake_proc.feed({"id": rid, "result": {"thread": {"id": t_id}}})
                elif method == "thread/turns/list":
                    fake_proc.feed({"id": rid, "result": {"data": [], "nextCursor": None}})
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

    def test_find_git_dirs_from_subdirectory(self):
        from genfoundry.codex_agent import find_git_dirs
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            # Setup: root_repo/.git and root_repo/deep/nested/workspace
            root_git = os.path.join(temp_dir, ".git")
            os.makedirs(root_git)
            deep_workspace = os.path.join(temp_dir, "deep", "nested", "workspace")
            os.makedirs(deep_workspace)

            # Upward traversal finds parent repository root .git
            dirs = find_git_dirs(deep_workspace)
            self.assertEqual(dirs, [root_git])
        finally:
            shutil.rmtree(temp_dir)

    def test_find_git_dirs_non_git_workspace(self):
        from genfoundry.codex_agent import find_git_dirs
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            # Workspace with no git repo around
            dirs = find_git_dirs(temp_dir)
            self.assertEqual(dirs, [])
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
            expected_sandbox = "danger-full-access" if sys.platform == "win32" else "workspace-write"
            self.assertEqual(params.get("sandbox"), expected_sandbox)
        finally:
            await agent.disconnect()

    async def test_thread_start_sandbox_parameter_boolean_true(self):
        fake_proc = FakeProcess()
        opts = AgentOptions(cli_path="/usr/bin/true", sandbox_mode=True)
        agent = await self._create_agent(fake_proc, opts=opts)
        try:
            start_call = next((c for c in self._rpc_calls if c.get("method") == "thread/start"), None)
            self.assertIsNotNone(start_call)
            params = start_call.get("params", {})
            self.assertIsInstance(params.get("sandbox"), str)
            expected_sandbox = "danger-full-access" if sys.platform == "win32" else "workspace-write"
            self.assertEqual(params.get("sandbox"), expected_sandbox)
        finally:
            await agent.disconnect()

    async def test_thread_start_think_level_and_turn_effort(self):
        fake_proc = FakeProcess()
        opts = AgentOptions(cli_path="/usr/bin/true", think_level="high")
        agent = await self._create_agent(fake_proc, opts=opts)
        try:
            # 1. Verify thread/start config contains model_reasoning_effort
            start_call = next((c for c in self._rpc_calls if c.get("method") == "thread/start"), None)
            self.assertIsNotNone(start_call)
            config = start_call.get("params", {}).get("config", {})
            self.assertEqual(config.get("model_reasoning_effort"), "high")

            # 2. Verify turn/start receives effort param
            await agent.send_message("Solve this puzzle")
            turn_call = next((c for c in self._rpc_calls if c.get("method") == "turn/start"), None)
            self.assertIsNotNone(turn_call)
            turn_params = turn_call.get("params", {})
            self.assertEqual(turn_params.get("effort"), "high")

            # 3. Dynamically change think level and verify next turn
            agent.set_think_level("low")
            await agent.send_message("Solve faster")
            turn_calls = [c for c in self._rpc_calls if c.get("method") == "turn/start"]
            self.assertGreaterEqual(len(turn_calls), 2)
            self.assertEqual(turn_calls[-1].get("params", {}).get("effort"), "low")

            # 4. Set to auto and verify effort is not attached (allowing model default)
            agent.set_think_level("auto")
            await agent.send_message("Solve normally")
            turn_calls = [c for c in self._rpc_calls if c.get("method") == "turn/start"]
            self.assertGreaterEqual(len(turn_calls), 3)
            self.assertNotIn("effort", turn_calls[-1].get("params", {}))
        finally:
            await agent.disconnect()

    async def test_token_usage_cold_start_event(self):
        """Verify cold start thread/tokenUsage/updated preserves cached=0 and cacheWrite."""
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)
        try:
            usage_payload = {
                "total": {
                    "totalTokens": 10172,
                    "inputTokens": 10030,
                    "cachedInputTokens": 0,
                    "cacheWriteInputTokens": 10027,
                    "outputTokens": 142,
                    "reasoningOutputTokens": 35,
                },
                "last": {
                    "totalTokens": 10172,
                    "inputTokens": 10030,
                    "cachedInputTokens": 0,
                    "cacheWriteInputTokens": 10027,
                    "outputTokens": 142,
                    "reasoningOutputTokens": 35,
                },
                "modelContextWindow": 258400,
            }
            fake_proc.feed({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-001",
                    "turnId": "turn-001",
                    "tokenUsage": usage_payload,
                },
            })

            async for msg in agent.receive_messages():
                if msg.type == MessageType.TOKEN_USAGE.value:
                    self.assertEqual(msg.content, usage_payload)
                    last = msg.content["last"]
                    self.assertEqual(last["inputTokens"], 10030)
                    self.assertEqual(last["cachedInputTokens"], 0)
                    self.assertEqual(last["cacheWriteInputTokens"], 10027)
                    self.assertEqual(last["outputTokens"], 142)
                    self.assertEqual(last["reasoningOutputTokens"], 35)
                    self.assertEqual(msg.content["modelContextWindow"], 258400)
                    break
        finally:
            await agent.disconnect()

    async def test_token_usage_multi_step_turn_completion_event(self):
        """Verify multi-step completed turnUsage has cache hit, reasoning and total breakdown."""
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)
        try:
            usage_payload = {
                "total": {
                    "totalTokens": 57274,
                    "inputTokens": 56540,
                    "cachedInputTokens": 39910,
                    "cacheWriteInputTokens": 16618,
                    "outputTokens": 734,
                    "reasoningOutputTokens": 326,
                },
                "last": {
                    "totalTokens": 17098,
                    "inputTokens": 16621,
                    "cachedInputTokens": 14993,
                    "cacheWriteInputTokens": 1625,
                    "outputTokens": 477,
                    "reasoningOutputTokens": 276,
                },
                "modelContextWindow": 258400,
            }
            fake_proc.feed({
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-001",
                    "turnId": "turn-001",
                    "tokenUsage": usage_payload,
                },
            })

            async for msg in agent.receive_messages():
                if msg.type == MessageType.TOKEN_USAGE.value:
                    self.assertEqual(msg.content, usage_payload)
                    last = msg.content["last"]
                    self.assertEqual(last["totalTokens"], 17098)
                    self.assertEqual(last["inputTokens"], 16621)
                    self.assertEqual(last["cachedInputTokens"], 14993)
                    self.assertEqual(last["cacheWriteInputTokens"], 1625)
                    self.assertEqual(last["outputTokens"], 477)
                    self.assertEqual(last["reasoningOutputTokens"], 276)

                    total = msg.content["total"]
                    self.assertEqual(total["totalTokens"], 57274)
                    self.assertEqual(total["inputTokens"], 56540)
                    self.assertEqual(total["cachedInputTokens"], 39910)
                    self.assertEqual(total["reasoningOutputTokens"], 326)
                    break
        finally:
            await agent.disconnect()

    async def test_rewind_with_thread_revert(self):
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        try:
            # Turn 1
            await agent.send_message("First message")
            await self._collect_turn(agent)

            # Turn 2
            await agent.send_message("Second message")
            await self._collect_turn(agent)

            self.assertEqual(agent._turn_count, 2)
            self.assertEqual(len(agent._turn_ids), 2)
            turn_2_id = agent._turn_ids[1]

            # Rewind using turn ID
            thread_id = await agent.rewind(turn_2_id)
            self.assertEqual(thread_id, "thread-001")
            self.assertEqual(agent._turn_count, 1)
            self.assertEqual(len(agent._turn_ids), 1)

            revert_call = next(
                c for c in self._rpc_calls if c.get("method") == "thread/revert"
            )
            self.assertEqual(revert_call["params"]["threadId"], "thread-001")
            self.assertEqual(revert_call["params"]["beforeTurnId"], turn_2_id)
        finally:
            await agent.disconnect()

    async def test_rewind_with_turn_index_resolves_to_revert(self):
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        try:
            # Turn 1
            await agent.send_message("First message")
            await self._collect_turn(agent)

            # Turn 2
            await agent.send_message("Second message")
            await self._collect_turn(agent)

            turn_2_id = agent._turn_ids[1]

            # Rewind using index string "2"
            thread_id = await agent.rewind("2")
            self.assertEqual(thread_id, "thread-001")
            self.assertEqual(agent._turn_count, 1)

            revert_call = next(
                c for c in self._rpc_calls if c.get("method") == "thread/revert"
            )
            self.assertEqual(revert_call["params"]["beforeTurnId"], turn_2_id)
        finally:
            await agent.disconnect()

    async def test_subagent_activity_visible_and_lifecycle_isolated(self):
        fake_proc = FakeProcess()
        agent = await self._create_agent(fake_proc)

        try:
            # Turn 1: normal turn
            await agent.send_message("First message")
            await self._collect_turn(agent)

            self.assertEqual(agent._turn_count, 1)
            turn_1_id = agent._turn_ids[0]

            # In Turn 2, simulate an active turn that spawns a subagent
            turn_2_id = "turn-custom-2"
            fake_proc.feed({
                "method": "turn/started",
                "params": {
                    "threadId": "thread-001",
                    "turn": {
                        "id": turn_2_id,
                        "rootTurnId": turn_2_id,
                    },
                },
            })

            # Subagent thread and turn
            sub_thread_id = "subthread-xyz"
            sub_turn_id = "subturn-abc"

            # Main agent tool call spawns subagent
            fake_proc.feed({
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "collabAgentToolCall",
                        "id": "collab-1",
                        "tool": "spawnAgent",
                        "receiverThreadIds": [sub_thread_id],
                        "prompt": "Investigate something",
                        "status": "completed",
                    }
                },
            })

            # Subagent starts its turn
            fake_proc.feed({
                "method": "turn/started",
                "params": {
                    "threadId": sub_thread_id,
                    "turn": {
                        "id": sub_turn_id,
                        "rootTurnId": turn_2_id,
                    },
                },
            })

            # Subagent produces tool call and text
            fake_proc.feed({
                "method": "item/started",
                "params": {
                    "item": {
                        "type": "mcpToolCall",
                        "id": "mcp-1",
                        "server": "web_search",
                        "tool": "search",
                        "status": "inProgress",
                    },
                    "threadId": sub_thread_id,
                },
            })
            fake_proc.feed({
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": "submsg-1",
                        "text": "Subagent findings",
                    },
                    "threadId": sub_thread_id,
                },
            })

            # Subagent turn completes
            fake_proc.feed({
                "method": "turn/completed",
                "params": {
                    "threadId": sub_thread_id,
                    "turn": {
                        "id": sub_turn_id,
                        "rootTurnId": turn_2_id,
                    },
                },
            })

            # Main agent produces final text
            fake_proc.feed({
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "id": "mainmsg-2",
                        "text": "Main agent final answer",
                    },
                    "threadId": "thread-001",
                },
            })

            # Yield to let reader task process events up to subagent completion
            await asyncio.sleep(0.05)
            # Subagent completion must not reset main turn active flag
            self.assertTrue(agent._is_turn_active)

            # Main agent completes
            fake_proc.feed({
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-001",
                    "turn": {
                        "id": turn_2_id,
                        "rootTurnId": turn_2_id,
                    },
                },
            })

            # Collect all messages for turn 2
            messages = await self._collect_turn(agent)
            types = [m.type for m in messages]

            # 1. Main turn started was emitted exactly once
            self.assertEqual(types.count("turn_started"), 1)
            # 2. Subagent tool calls and messages were emitted
            self.assertIn("tool_use", types)
            self.assertIn("assistant", types)
            # 3. Main thread turn state was preserved
            self.assertEqual(agent._turn_count, 2)
            self.assertEqual(agent._turn_ids, [turn_1_id, turn_2_id])
            self.assertEqual(agent._active_turn_id, turn_2_id)
            self.assertFalse(agent._is_turn_active)
            # 4. Turn ended with exactly one stop (when main turn finished)
            self.assertEqual(types[-1], "stop")

            # 5. Rewind with subagent turn ID resolves to root turn_2_id
            reverted_thread = await agent.rewind(sub_turn_id)
            self.assertEqual(reverted_thread, "thread-001")
            self.assertEqual(agent._turn_count, 1)
            self.assertEqual(agent._turn_ids, [turn_1_id])

            revert_call = next(
                c for c in self._rpc_calls if c.get("method") == "thread/revert"
            )
            self.assertEqual(revert_call["params"]["threadId"], "thread-001")
            self.assertEqual(revert_call["params"]["beforeTurnId"], turn_2_id)
        finally:
            await agent.disconnect()

    async def test_thread_resume_exclude_turns_and_turns_list_hydration(self):
        """Verify thread/resume sends excludeTurns: True and hydrates via thread/turns/list."""
        fake_proc = FakeProcess()
        opts = AgentOptions(
            cli_path="/usr/bin/true",
            session_id="thread-resume-001",
        )
        agent = CodexAgent(options=opts)
        rpc_calls = []
        original_write = agent._write_json

        async def mock_write_json(data: dict):
            rpc_calls.append(data)
            await original_write(data)
            if "id" in data:
                method = data.get("method", "")
                rid = data["id"]
                if method == "initialize":
                    fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                elif method == "thread/resume":
                    fake_proc.feed({
                        "id": rid,
                        "result": {
                            "thread": {"id": "thread-resume-001"},
                        },
                    })
                elif method == "thread/turns/list":
                    fake_proc.feed({
                        "id": rid,
                        "result": {
                            "data": [
                                {"id": "turn-r1", "rootTurnId": "turn-r1"},
                                {"id": "turn-r2", "rootTurnId": "turn-r2"},
                                {"id": "turn-r2-sub", "rootTurnId": "turn-r2"},
                            ],
                            "nextCursor": None,
                        },
                    })

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()

        try:
            # 1. thread/resume had excludeTurns: True
            resume_call = next(c for c in rpc_calls if c.get("method") == "thread/resume")
            self.assertTrue(resume_call["params"].get("excludeTurns"))
            self.assertEqual(resume_call["params"].get("threadId"), "thread-resume-001")

            # 2. thread/turns/list was called with itemsView: notLoaded
            turns_call = next(c for c in rpc_calls if c.get("method") == "thread/turns/list")
            self.assertEqual(turns_call["params"].get("threadId"), "thread-resume-001")
            self.assertEqual(turns_call["params"].get("itemsView"), "notLoaded")

            # 3. turns were properly hydrated into agent
            self.assertEqual(agent._turn_ids, ["turn-r1", "turn-r2"])
            self.assertEqual(agent._turn_count, 2)
            self.assertEqual(agent._subagent_turn_to_root, {"turn-r2-sub": "turn-r2"})
        finally:
            await agent.disconnect()

    async def test_thread_resume_fallback_to_hydrated_turns(self):
        """Verify fallback to thread.turns when thread/turns/list fails or returns empty."""
        fake_proc = FakeProcess()
        opts = AgentOptions(
            cli_path="/usr/bin/true",
            session_id="thread-resume-legacy",
        )
        agent = CodexAgent(options=opts)
        original_write = agent._write_json

        async def mock_write_json(data: dict):
            await original_write(data)
            if "id" in data:
                method = data.get("method", "")
                rid = data["id"]
                if method == "initialize":
                    fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                elif method == "thread/resume":
                    fake_proc.feed({
                        "id": rid,
                        "result": {
                            "thread": {
                                "id": "thread-resume-legacy",
                                "turns": [
                                    {"id": "turn-legacy-1", "rootTurnId": "turn-legacy-1"},
                                    {"id": "turn-legacy-sub", "rootTurnId": "turn-legacy-1"},
                                    {"id": "turn-legacy-2", "rootTurnId": "turn-legacy-2"},
                                ],
                            },
                        },
                    })
                elif method == "thread/turns/list":
                    fake_proc.feed({
                        "id": rid,
                        "error": {"code": -32601, "message": "Method not found"},
                    })

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()

        try:
            self.assertEqual(agent._turn_ids, ["turn-legacy-1", "turn-legacy-2"])
            self.assertEqual(agent._turn_count, 2)
            self.assertEqual(
                agent._subagent_turn_to_root,
                {"turn-legacy-sub": "turn-legacy-1"}
            )
        finally:
            await agent.disconnect()

    async def test_steer_inactive_turn_falls_back_to_turn_start(self):
        """Verify steer() uses turn/start without expectedTurnId when no turn is active."""
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
                agent._active_turn_id = "turn-old"
                agent._is_turn_active = False
                await agent.steer("New turn message")
                start_call = next(
                    (c for c in self._rpc_calls if c.get("method") == "turn/start"), None
                )
                self.assertIsNotNone(start_call)
                self.assertEqual(start_call["params"]["input"][0]["text"], "New turn message")
                self.assertNotIn("expectedTurnId", start_call["params"])
            finally:
                await agent.disconnect()

    async def test_steer_race_condition_falls_back_to_turn_start(self):
        """Verify steer() falls back to turn/start when turn/steer fails due to completed turn."""
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
                elif method == "turn/steer":
                    # Simulate race condition: turn completed before steer reached server.
                    # 1. Server emits turn/completed on stdout, resetting active turn flag.
                    fake_proc.feed({
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-001",
                            "turn": {
                                "id": "turn-race-1",
                                "rootTurnId": "turn-race-1",
                                "status": "completed",
                            },
                        },
                    })
                    # 2. Server responds with error to turn/steer request.
                    fake_proc.feed({
                        "id": rid,
                        "error": {
                            "code": -32600,
                            "message": "Turn turn-race-1 is no longer active",
                        },
                    })
                elif method == "turn/start":
                    # Fallback turn/start arrives and succeeds
                    fake_proc.feed({
                        "id": rid,
                        "result": {
                            "turn": {
                                "id": "turn-race-2",
                                "status": "inProgress",
                            }
                        },
                    })

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()
            try:
                agent._active_turn_id = "turn-race-1"
                agent._is_turn_active = True

                await agent.steer("Steering instruction during race")

                # Verify turn/steer was attempted first
                steer_call = next(
                    (c for c in self._rpc_calls if c.get("method") == "turn/steer"), None
                )
                self.assertIsNotNone(steer_call)
                self.assertEqual(steer_call["params"]["expectedTurnId"], "turn-race-1")

                # Verify it fell back to turn/start without expectedTurnId
                start_call = next(
                    (c for c in self._rpc_calls if c.get("method") == "turn/start"), None
                )
                self.assertIsNotNone(start_call)
                self.assertEqual(
                    start_call["params"]["input"][0]["text"],
                    "Steering instruction during race",
                )
                self.assertNotIn("expectedTurnId", start_call["params"])
            finally:
                await agent.disconnect()

    async def test_list_codex_sessions_rpc(self):
        """Verify _list_codex_sessions_rpc uses --listen stdio:// and TermMate clientInfo."""
        from genfoundry.codex_agent import _list_codex_sessions_rpc

        fake_proc = FakeProcess()
        executed_cmd = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            executed_cmd.extend(args)
            return fake_proc

        written_requests = []
        original_write = fake_proc.stdin.write

        def intercept_write(payload: bytes):
            original_write(payload)
            for line in payload.split(b"\n"):
                if not line.strip():
                    continue
                data = json.loads(line.decode("utf-8"))
                written_requests.append(data)
                rid = data.get("id")
                method = data.get("method")
                if rid is not None:
                    if method == "initialize":
                        fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                    elif method == "thread/list":
                        fake_proc.feed({
                            "id": rid,
                            "result": {
                                "data": [
                                    {"id": "t1", "name": "Thread 1", "updatedAt": 123456.0},
                                ],
                            },
                        })

        fake_proc.stdin.write = intercept_write

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            threads = await _list_codex_sessions_rpc("/usr/bin/codex", cwd="/workspace")

        self.assertEqual(executed_cmd, ["/usr/bin/codex", "app-server", "--listen", "stdio://"])
        init_req = next((r for r in written_requests if r.get("method") == "initialize"), None)
        self.assertIsNotNone(init_req)
        self.assertEqual(init_req["params"]["clientInfo"]["name"], "TermMate")
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["session_id"], "t1")

    async def test_get_codex_session_info_rpc(self):
        """Verify _get_codex_session_info_rpc uses thread/read + thread/turns/list."""
        from genfoundry.codex_agent import _get_codex_session_info_rpc

        fake_proc = FakeProcess()
        executed_cmd = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            executed_cmd.extend(args)
            return fake_proc

        written_requests = []
        original_write = fake_proc.stdin.write

        def intercept_write(payload: bytes):
            original_write(payload)
            for line in payload.split(b"\n"):
                if not line.strip():
                    continue
                data = json.loads(line.decode("utf-8"))
                written_requests.append(data)
                rid = data.get("id")
                method = data.get("method")
                if rid is not None:
                    if method == "initialize":
                        fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
                    elif method == "thread/read":
                        fake_proc.feed({
                            "id": rid,
                            "result": {
                                "thread": {
                                    "id": "t1",
                                    "name": "Thread 1",
                                    "updatedAt": 123456.0,
                                }
                            },
                        })
                    elif method == "thread/turns/list":
                        fake_proc.feed({
                            "id": rid,
                            "result": {
                                "data": [
                                    {
                                        "id": "turn-1",
                                        "items": [
                                            {
                                                "type": "userMessage",
                                                "content": [{"type": "text", "text": "Hello"}],
                                            },
                                            {
                                                "type": "agentMessage",
                                                "text": "Hi there!",
                                            },
                                        ],
                                    }
                                ]
                            },
                        })

        fake_proc.stdin.write = intercept_write

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
            info = await _get_codex_session_info_rpc(
                "/usr/bin/codex", "t1", cwd="/workspace", history_limit=5
            )

        self.assertEqual(executed_cmd, ["/usr/bin/codex", "app-server", "--listen", "stdio://"])
        read_req = next((r for r in written_requests if r.get("method") == "thread/read"), None)
        self.assertIsNotNone(read_req)
        self.assertFalse(read_req["params"].get("includeTurns"))

        turns_req = next(
            (r for r in written_requests if r.get("method") == "thread/turns/list"), None
        )
        self.assertIsNotNone(turns_req)
        self.assertEqual(turns_req["params"]["threadId"], "t1")
        self.assertEqual(turns_req["params"]["itemsView"], "summary")
        self.assertEqual(turns_req["params"]["limit"], 5)

        self.assertIsNotNone(info)
        self.assertEqual(info["summary"], "Thread 1")
        self.assertEqual(info["prompt"], "Hello")
        self.assertEqual(info["response"], "Hi there!")

    async def test_unhandled_server_request_handling(self):
        """Verify unhandled server requests return decline for approvals and -32601 for others."""
        fake_proc = FakeProcess()
        opts = AgentOptions(cli_path="/usr/bin/true")
        agent = CodexAgent(options=opts)
        written_messages = []

        async def mock_write_json(data: dict):
            written_messages.append(data)
            rid = data.get("id")
            method = data.get("method")
            if rid is not None and method == "initialize":
                fake_proc.feed({"id": rid, "result": {"capabilities": {}}})
            elif rid is not None and method == "thread/start":
                fake_proc.feed({"id": rid, "result": {"thread": {"id": "t1"}}})

        agent._write_json = mock_write_json
        with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
            await agent.connect()
            try:
                # 1. Unhandled approval request -> decision: decline
                fake_proc.feed({
                    "id": 901,
                    "method": "unknown/approvalRequest",
                    "params": {},
                })
                # Yield to let created background task run
                await asyncio.sleep(0.05)
                resp_901 = next((m for m in written_messages if m.get("id") == 901), None)
                self.assertIsNotNone(resp_901)
                self.assertEqual(resp_901.get("result"), {"decision": "decline"})

                # 2. Unhandled non-approval request -> JSON-RPC error -32601
                fake_proc.feed({
                    "id": 902,
                    "method": "item/tool/call",
                    "params": {},
                })
                await asyncio.sleep(0.05)
                resp_902 = next((m for m in written_messages if m.get("id") == 902), None)
                self.assertIsNotNone(resp_902)
                self.assertIn("error", resp_902)
                self.assertEqual(resp_902["error"]["code"], -32601)
                self.assertIn("Method not found", resp_902["error"]["message"])
            finally:
                await agent.disconnect()


if __name__ == "__main__":
    unittest.main()
