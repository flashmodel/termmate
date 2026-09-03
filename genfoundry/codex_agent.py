"""
Codex Agent SDK Client - App Server Implementation
A client for interacting with Codex via the "codex app-server" JSON-RPC protocol.

The app-server provides a persistent bidirectional stdio connection with support
for interactive approval, multi-turn conversations, and streaming events.
"""

import asyncio
import json
import os
import shutil
import sys
import logging
import re
from typing import Optional, Dict, Any, AsyncIterator, List, Callable, Union

LOG = logging.getLogger("TermMate")

from .base_agent import MessageType, Message, TextBlock, AssistantMessage, \
    PermissionResultAllow, PermissionResultDeny, ToolPermissionContext, AgentOptions, BaseAgent


def find_codex_cli() -> Optional[str]:
    """Search common default install locations for the codex CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "codex.cmd"),
                os.path.join(appdata, "npm", "codex")
            ])

    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "codex"),
            os.path.join(home, ".npm-global", "bin", "codex"),
            os.path.join(home, ".yarn", "bin", "codex"),
            os.path.join(home, ".bun", "bin", "codex"),
            "/usr/local/bin/codex",
            "/usr/bin/codex",
            "/opt/homebrew/bin/codex",           # macOS (Intel/Apple Silicon)
            "/home/linuxbrew/.linuxbrew/bin/codex",  # Linux Homebrew
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found codex CLI at default location: {path_str}")
            return path_str
    
    return None


def find_git_dirs(workspace_root: Optional[str]) -> List[str]:
    """
    Find all .git directories for workspace (root, parent repo, and submodules).
    Scoped strictly to the logical Git repository belonging to workspace_root.
    """
    if not workspace_root or not os.path.isdir(workspace_root):
        return []

    root = os.path.abspath(workspace_root)
    git_dirs = set()

    def add_path(p: str):
        abs_p = os.path.abspath(p) if os.path.isabs(p) else os.path.abspath(os.path.join(root, p))
        if os.path.isdir(abs_p):
            git_dirs.add(abs_p)
        elif os.path.isfile(abs_p):
            git_dirs.add(abs_p)
            try:
                with open(abs_p, "r", encoding="utf-8", errors="ignore") as f:
                    txt = f.read().strip()
                if txt.startswith("gitdir:"):
                    target = os.path.abspath(os.path.join(os.path.dirname(abs_p), txt[7:].strip()))
                    git_dirs.add(target)
                    # Resolve worktree commondir (shared objects and refs)
                    commondir_file = os.path.join(target, "commondir")
                    if os.path.isfile(commondir_file):
                        with open(commondir_file, "r", encoding="utf-8", errors="ignore") as cf:
                            c_target = os.path.abspath(os.path.join(target, cf.read().strip()))
                            git_dirs.add(c_target)
            except Exception:
                pass

    # 1. Resolve root/parent git repository via git CLI
    try:
        import subprocess
        res = subprocess.run(
            ["git", "rev-parse", "--git-dir", "--git-common-dir"],
            cwd=root, capture_output=True, text=True, timeout=2
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if line.strip():
                    add_path(line.strip())
    except Exception:
        pass

    # Direct check on workspace root
    add_path(os.path.join(root, ".git"))

    # 2. Scan nested submodules / monorepos (prune heavy & hidden dirs)
    ignore = {"node_modules", ".venv", "venv", "dist", "build", "target", ".git"}
    for cur_root, dirs, files in os.walk(root):
        if ".git" in dirs:
            add_path(os.path.join(cur_root, ".git"))
            dirs.remove(".git")
        if ".git" in files:
            add_path(os.path.join(cur_root, ".git"))
        dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]

    return sorted(git_dirs)


class CodexAgent(BaseAgent):
    """
    Client for interacting with Codex via "codex app-server" (JSON-RPC over stdio).

    Unlike the old "codex exec --json" approach which spawned a new process per
    turn and had no interactive approval support, the app-server maintains a
    single long-lived process with bidirectional communication.

    Key features:
    - Persistent process: single codex app-server subprocess for the session
    - Bidirectional: send and receive JSON-RPC messages at any time
    - Approval support: handle command/file-change approval requests
    - Multi-turn: reuse the same thread across multiple messages
    """

    def __init__(self, options: Optional[AgentOptions] = None):
        super().__init__(options)
        self.thread_id: Optional[str] = None
        cli_command = self.options.cli_path or "codex"
        self.cli_path = shutil.which(cli_command) or find_codex_cli() or cli_command
        self._is_connected = False
        self.available_models: List[Dict[str, Any]] = []

        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._rpc_id: int = 0
        # Pending RPC responses keyed by request id
        self._pending_responses: Dict[int, asyncio.Future] = {}
        self._permission_callback: Optional[Callable] = self.options.can_use_tool
        # Track active turn so we know when it completes
        self._active_turn_id: Optional[str] = None
        # Pending approval responses from the UI, keyed by approval_id
        self._pending_approvals: Dict[str, asyncio.Future] = {}
        # Cache item data from item/started, keyed by itemId
        self._item_cache: Dict[str, Dict[str, Any]] = {}
        # Plan mode: mutable at runtime (separate from options.plan_mode snapshot)
        self.plan_mode: bool = self.options.plan_mode
        # Accumulates item/plan/delta content in plan mode
        self._plan_text: str = ""
        # Counts user turns sent (1-based); used as rewind handle
        self._turn_count: int = 0

        if not self.cli_path:
            raise FileNotFoundError(
                "Codex CLI not found in PATH. Please install it or set 'codex_command' in settings."
            )
        LOG.info(f"Codex CLI path: {self.cli_path}")

    async def _fetch_models(self) -> None:
        """Fetch available models via the model/list RPC method."""
        try:
            result = await self._rpc_request("model/list", {})
            if not result or not isinstance(result, dict):
                return
            models = [
                {
                    "displayName": m.get("displayName") or m.get("id", ""),
                    "description": m.get("description", ""),
                    "value": m.get("id", ""),
                }
                for m in result.get("data", [])
                if m.get("id") and not m.get("hidden", False)
            ]
            if models:
                self.available_models = models
                LOG.info(f"Fetched {len(models)} models via model/list RPC")
                await self._message_queue.put(Message(
                    "models_update",
                    content={"models": models},
                ))
        except Exception as e:
            LOG.warning(f"Failed to fetch models via model/list: {e}")

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def connect(self, prompt: Optional[str] = None) -> None:
        """
        Start the codex app-server subprocess, perform the JSON-RPC
        initialize handshake, and create a thread.
        """
        if self._is_connected:
            raise RuntimeError("Client is already connected")

        env = os.environ.copy()

        # Inject user-defined extra environment variables (including ANTHROPIC_API_KEY etc.)
        if self.options.extra_env:
            env.update(self.options.extra_env)

        cmd = [self.cli_path, "app-server"]

        LOG.info(f"Starting Codex app-server: {' '.join(cmd)}")
        
        kwargs = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.options.cwd,
            **kwargs
        )

        self._is_connected = True
        self._read_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # JSON-RPC initialize handshake
        await self._rpc_request("initialize", {
            "clientInfo": {"name": "codyform", "title": "Codyform", "version": "1.0"},
            "capabilities": {"experimentalApi": True}
        })
        await self._rpc_notify("initialized")

        # Fetch available models via model/list RPC (no API key required)
        asyncio.create_task(self._fetch_models())

        # Create a thread
        thread_params = {"cwd": self.options.cwd}
        if self.options.session_id:
            thread_params["threadId"] = self.options.session_id
        if self.options.model:
            thread_params["model"] = self.options.model

        # Discover all relevant .git directories strictly for cwd's logical repo boundary (excluding add_dirs)
        git_dirs = find_git_dirs(self.options.cwd)
        writable_roots = list(self.options.add_dirs or [])
        for gd in git_dirs:
            if gd not in writable_roots:
                writable_roots.append(gd)

        # Initialize config if we have add_dirs/git_dirs or other config-based overrides
        config_overrides = {}

        if writable_roots:
            config_overrides["sandbox_workspace_write.writable_roots"] = writable_roots

        if self.options.disallowed_tools and "AskUserQuestion" in self.options.disallowed_tools:
            config_overrides["features.default_mode_request_user_input"] = False

        if config_overrides:
            thread_params["config"] = config_overrides

        # Map approve_mode to Codex approvalPolicy:
        #   "untrusted"  — always ask for approval (commands + file changes)
        #   "on-failure" — ask only when a command fails
        #   "on-request" — ask only when the agent itself requests it
        #   "never"      — never ask
        approve_mode = self.options.approve_mode
        if approve_mode == "accept-all":
            thread_params["approvalPolicy"] = "never"
        else:
            # default / allow-edit → all operations require approval
            thread_params["approvalPolicy"] = "untrusted"

        sandbox_map = {
            "read-only": {"type": "readOnly"},
            "workspace-write": {
                "type": "workspaceWrite",
                "writableRoots": writable_roots,
                "networkAccess": False,
            },
            "danger-full-access": {"type": "dangerFullAccess"},
        }
        if self.options.sandbox_mode:
            thread_params["sandbox"] = sandbox_map.get(self.options.sandbox_mode, {
                "type": "workspaceWrite",
                "writableRoots": writable_roots,
                "networkAccess": False,
            })
        else:
            # Default to workspace-write with git and add_dirs writable
            thread_params["sandbox"] = {
                "type": "workspaceWrite",
                "writableRoots": writable_roots,
                "networkAccess": False,
            }

        method = "thread/resume" if self.options.session_id else "thread/start"
        result = await self._rpc_request(method, thread_params)
        if result and isinstance(result, dict):
            thread = result.get("thread", {})
            self.thread_id = thread.get("id")

            # Capture the default model assigned by the server
            if "model" in result and not self.options.model:
                self.options.model = result.get("model")

            # Sync turn count from resumed thread so rewind math stays correct
            turns = thread.get("turns", [])
            if turns:
                self._turn_count = len(turns)
                LOG.info(f"Codex connect {method}: {self.thread_id} (resumed {self._turn_count} turns)")
            else:
                LOG.info(f"Codex connect {method}: {self.thread_id}")

        if prompt:
            await self.send_message(prompt)

    def set_model(self, model: str) -> None:
        """Dynamically switch the model; takes effect on the next turn."""
        self.options.model = model
        LOG.info(f"Codex model switched to: {model}")

    def _extract_turn_id(self, data: dict) -> Optional[str]:
        """Extract turnId from a message params or result dict."""
        if not data:
            return None
        # Check direct turnId (older format or item notifications)
        turn_id = data.get("turnId")
        if turn_id:
            return turn_id
        # Check nested turn object (Codex app-server format)
        turn = data.get("turn")
        if isinstance(turn, dict):
            return turn.get("id")
        return None

    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None, proceed_plan: bool = False) -> None:
        """Send a user message to Codex by starting a new turn on the thread."""
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        if not self.thread_id:
            raise RuntimeError("No active thread. Call connect() first.")

        self._plan_text = ""
        params: Dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": content}],
        }
        if self._active_turn_id:
            params["expectedTurnId"] = self._active_turn_id
        if self.options.model:
            params["model"] = self.options.model

        # Determine the target collaboration mode for this turn.
        # - If proceed_plan is True, we must explicitly exit Plan mode to execute.
        # - Otherwise, if self.plan_mode is True, we ensure we are in Plan mode.
        target_mode = None
        if proceed_plan:
            target_mode = "default"
        elif self.plan_mode:
            target_mode = "plan"

        if target_mode:
            params["collaborationMode"] = {
                "mode": target_mode,
                "settings": {
                    "model": self.options.model,
                    "developer_instructions": None,
                },
            }

        result = await self._rpc_request("turn/start", params)
        if result and isinstance(result, dict):
            turn_id = self._extract_turn_id(result)
            if turn_id:
                self._active_turn_id = turn_id
                LOG.debug(f"Captured turnId from turn/start result: {turn_id}")

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """Send a steering message to the Agent (e.g. 'Implement this plan')"""
        if not self._is_connected or not self.thread_id:
            raise RuntimeError("Client is not connected or no active thread.")

        LOG.info(f"Steering agent: {text} (proceed_plan={proceed_plan})")
        await self.send_message(text, proceed_plan=proceed_plan)

    async def interrupt(self) -> None:
        """Interrupt the current conversation or turn."""
        if self._is_connected and self.thread_id and self._active_turn_id:
            LOG.info(f"CodexAgent: Interrupting turn {self._active_turn_id} on thread {self.thread_id}")
            await self._rpc_request("turn/interrupt", {
                "threadId": self.thread_id,
                "turnId": self._active_turn_id
            })
        else:
            LOG.warning(f"CodexAgent: Cannot interrupt - is_connected: {self._is_connected}, thread_id: {self.thread_id}, active_turn_id: {self._active_turn_id}")

    async def rewind(self, turn_identifier: str) -> Optional[str]:
        """Roll back the thread to just before the turn at turn_identifier (1-based index).

        Drops the target turn and everything after it.
        numTurns = _turn_count - (target_index - 1) = turns from target to end (inclusive).
        Returns the thread_id (unchanged).
        """
        if not self._is_connected or not self.thread_id:
            raise RuntimeError("Client is not connected or no active thread.")

        target_index = int(turn_identifier)
        num_turns = self._turn_count - (target_index - 1)
        if num_turns < 1:
            return self.thread_id

        LOG.info(f"[rewind] thread/rollback numTurns={num_turns} (turn_count={self._turn_count}, target={target_index})")
        result = await self._rpc_request("thread/rollback", {
            "threadId": self.thread_id,
            "numTurns": num_turns,
        })
        if result is None:
            raise RuntimeError("thread/rollback RPC failed or timed out")

        self._turn_count = target_index - 1
        self._active_turn_id = None
        return self.thread_id

    async def _respond_to_server_request(self, request_id: Any, result: Dict[str, Any]) -> None:
        """Send a JSON-RPC response to a server-initiated request."""
        LOG.debug(f"response for codex request [{request_id}]: {result}")
        await self._write_json({"id": request_id, "result": result})

    async def _write_json(self, data: Dict[str, Any]) -> None:
        if not self._process or not self._process.stdin:
            raise RuntimeError("Process not available")
        raw = json.dumps(data) + "\n"
        self._process.stdin.write(raw.encode("utf-8"))
        await self._process.stdin.drain()

    async def _rpc_request(self, method: str, params: Dict[str, Any], timeout: float = 30.0) -> Any:
        """Send a JSON-RPC request and wait for its response."""
        rid = self._next_id()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[rid] = future

        await self._write_json({"method": method, "id": rid, "params": params})
        LOG.debug(f"rpc_request [{rid}] {method}")

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_responses.pop(rid, None)
            LOG.error(f"RPC timeout for {method} (id={rid})")
            return None

    async def _rpc_notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        msg = {"method": method}
        if params:
            msg["params"] = params
        await self._write_json(msg)

    async def _read_messages(self) -> None:
        """Background task to read JSONL from app-server stdout."""
        if not self._process or not self._process.stdout:
            return

        buffer = b""
        chunk_limit = 65536

        try:
            while self._is_connected:
                try:
                    chunk = await self._process.stdout.read(chunk_limit)
                except Exception:
                    break
                if not chunk:
                    break

                buffer += chunk
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        LOG.debug(f"codex receive: {line}")
                        data = json.loads(line)
                        await self._dispatch(data)
                    except json.JSONDecodeError:
                        LOG.debug(f"codex non-json: {line[:200]}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"read_messages error: {e}")
            await self._message_queue.put(Message(MessageType.ERROR.value, content=str(e)))

    async def _read_stderr(self) -> None:
        if not self._process or not self._process.stderr:
            return
        try:
            while self._is_connected:
                line = await self._process.stderr.readline()
                if not line:
                    break
                # LOG.debug(f"codex stderr: {line.decode('utf-8', errors='replace').strip()}")
        except (asyncio.CancelledError, Exception):
            pass

    def _parse_codex_error(self, error: Any) -> str:
        """Extract a user-friendly error message from Codex error data."""
        if not error:
            return "Unknown error"
        
        message = ""
        if isinstance(error, dict):
            message = error.get("message", "")
        elif isinstance(error, str):
            message = error
        else:
            message = str(error)
            
        try:
            parsed = json.loads(message)
            if isinstance(parsed, dict):
                if "error" in parsed and isinstance(parsed["error"], dict):
                    return parsed["error"].get("message", message)
                if "message" in parsed:
                    return parsed.get("message", message)
        except (json.JSONDecodeError, TypeError):
            pass
            
        return message

    # ── Message dispatch ────────────────────────────────────────────────

    async def _dispatch(self, data: Dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message to the right handler."""

        # RPC response (has "id" but no "method")
        if "id" in data and "method" not in data:
            rid = data["id"]
            future = self._pending_responses.pop(rid, None)
            if future and not future.done():
                if "error" in data:
                    LOG.error(f"RPC error [{rid}]: {data['error']}")
                    future.set_result(None)
                else:
                    future.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {})

        if method == "thread/started":
            thread_data = params.get("thread", {})
            t_id = thread_data.get("id")
            if t_id:
                await self._message_queue.put(Message("thread_started", content={"session_id": t_id}))

        elif method == "turn/started":
            turn_id = self._extract_turn_id(params)
            self._active_turn_id = turn_id
            self._turn_count += 1
            await self._message_queue.put(
                Message("turn_started", content={"turnId": turn_id, "turnIndex": self._turn_count})
            )

        elif method == "turn/completed":
            # Update active turn ID if provided in completion
            turn_id = self._extract_turn_id(params)
            if turn_id:
                self._active_turn_id = turn_id
            # We keep self._active_turn_id here so it can be used as expectedTurnId
            # for the next turn or steering.
            if self.plan_mode and self._plan_text:
                await self._message_queue.put(
                    Message(MessageType.PLAN_DELTA.value, content=self._plan_text)
                )
                self._plan_text = ""
            await self._message_queue.put(Message(MessageType.STOP.value))

        elif method == "item/plan/delta":
            delta = params.get("delta", "")
            if delta:
                self._plan_text += delta

        elif method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if delta:
                await self._message_queue.put(
                    Message(MessageType.TEXT.value, content=delta,
                            msg_id=params.get("itemId"))
                )

        elif method == "item/completed":
            await self._handle_item_completed(params)

        elif method == "item/started":
            await self._handle_item_started(params)

        elif method == "item/commandExecution/requestApproval":
            # Run in a separate task to avoid blocking the message reader
            asyncio.create_task(self._handle_command_approval(data["id"], params))

        elif method == "item/fileChange/requestApproval":
            asyncio.create_task(self._handle_file_approval(data["id"], params))

        elif method == "item/tool/requestUserInput":
            asyncio.create_task(self._handle_request_user_input(data["id"], params))

        elif method == "codex/event/stream_error":
            msg = params.get("msg", {})
            err_text = msg.get("message", "") if isinstance(msg, dict) else str(params)
            LOG.warning(f"Codex stream error: {err_text}")

        elif method == "codex/event/error" or method == "error":
            error = params.get("error") or params.get("msg") or params
            err_text = self._parse_codex_error(error)
            await self._message_queue.put(
                Message(MessageType.ERROR.value, content=err_text)
            )

    async def _handle_item_started(self, params: Dict[str, Any]) -> None:
        item = params.get("item", {})
        item_type = item.get("type")

        # Cache item data so requestApproval handlers can look it up by itemId
        item_id = item.get("id")
        if item_id:
            self._item_cache[item_id] = item

        if item_type == "commandExecution":
            # Extract cleaner command from commandActions if available
            command = item.get("command")
            actions = item.get("commandActions", [])
            if actions and isinstance(actions, list) and actions[0].get("command"):
                command = actions[0].get("command")

            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "command_execution",
                    "command": command,
                    "status": "in_progress",
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

    async def _handle_item_completed(self, params: Dict[str, Any]) -> None:
        item = params.get("item", {})
        item_type = item.get("type")

        if item_type == "agentMessage":
            text = item.get("text", "")
            if text:
                blocks = [TextBlock(text)]
                msg = AssistantMessage(content=blocks, msg_id=item.get("id"))
                await self._message_queue.put(msg)

        elif item_type == "commandExecution":
            # Just mark as completed if we want to update UI state,
            # but we've already displayed the command in _handle_item_started.
            # BaseAgent/ChatView might expect a message to know it's done.
            # However, ChatView doesn't seem to have a special handler for command result yet.
            # To avoid "duplication" reported by user, we skip sending another TOOL_USE here.
            pass

        elif item_type == "fileChange":
            changes = item.get("changes", [])
            filenames = []
            for c in changes:
                path = c.get("path")
                if path:
                    filenames.append(os.path.basename(path))

            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "fileChange",
                    "changes": changes,
                    "filenames": filenames,
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "mcpToolCall":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "mcpToolCall",
                    "server": item.get("server", ""),
                    "tool": item.get("tool", ""),
                    "arguments": item.get("arguments", {}),
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "dynamicToolCall":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "dynamicToolCall",
                    "namespace": item.get("namespace"),
                    "tool": item.get("tool", ""),
                    "arguments": item.get("arguments", {}),
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "collabAgentToolCall":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "collabAgentToolCall",
                    "tool": item.get("tool", ""),
                    "receiverThreadIds": item.get("receiverThreadIds", []),
                    "prompt": item.get("prompt"),
                    "model": item.get("model"),
                    "reasoningEffort": item.get("reasoningEffort"),
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "webSearch":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "webSearch",
                    "query": item.get("query", ""),
                    "action": item.get("action"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "imageView":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "imageView",
                    "path": item.get("path", ""),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "imageGeneration":
            tool_msg = Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "imageGeneration",
                    "status": item.get("status"),
                },
                msg_id=item.get("id"),
            )
            await self._message_queue.put(tool_msg)

        elif item_type == "reasoning":
            text = ""
            summary = item.get("summary", [])
            if summary:
                text = summary[0] if isinstance(summary, list) else str(summary)
            if text:
                await self._message_queue.put(
                    Message(MessageType.THINKING.value, content=text, msg_id=item.get("id"))
                )

    async def send_approval_response(self, approval_id: str, response_data: Dict[str, Any]) -> None:
        """
        Called by the UI to respond to an approval request.
        Routes to the pending future so the approval handler can complete.
        """
        future = self._pending_approvals.get(approval_id)
        if future and not future.done():
            future.set_result(response_data)

    async def _handle_command_approval(self, request_id: Any, params: Dict[str, Any]) -> None:
        """Handle a command execution approval request."""
        approval_id = str(request_id)
        command = params.get("command", "")
        cwd = params.get("cwd", "")

        LOG.info(f"Command approval request [rpc_id={request_id}]: {command} in {cwd}")

        input_data = {"command": command}
        approved = await self._emit_approval_request(approval_id, "command_execution", input_data)
        decision = "accept" if approved else "decline"
        await self._respond_to_server_request(request_id, {"decision": decision})

    async def _handle_file_approval(self, request_id: Any, params: Dict[str, Any]) -> None:
        """Handle a file change approval request."""
        approval_id = str(request_id)

        LOG.info(f"File approval request [rpc_id={request_id}]")

        # requestApproval params only contain threadId/turnId/itemId, not the changes.
        # Look up the item data cached from the preceding item/started event.
        item_id = params.get("itemId")
        item_data = self._item_cache.get(item_id, {}) if item_id else {}

        # Pre-process diff data for the UI using the cached item's changes
        processed_diff = self._generate_file_change_diff(item_data)

        input_data = {**params}
        if processed_diff:
            input_data["processed_diff"] = processed_diff

        approved = await self._emit_approval_request(approval_id, "fileChange", input_data)
        decision = "accept" if approved else "decline"
        await self._respond_to_server_request(request_id, {"decision": decision})

    async def _handle_request_user_input(self, request_id: Any, params: Dict[str, Any]) -> None:
        """Handle request for user input via tool/requestUserInput."""
        approval_id = str(request_id)
        LOG.info(f"User input request [rpc_id={request_id}]")

        if self.options.disallowed_tools and "AskUserQuestion" in self.options.disallowed_tools:
            LOG.info(f"AskUserQuestion is disabled, auto-denying [rpc_id={request_id}]")
            formatted_response = {}
            for q in params.get("questions", []):
                q_id = q.get("id", q.get("question", ""))
                formatted_response[q_id] = {
                    "answers": [
                        "This is an automated run. You are running in non-interactive mode."
                        "Do not use requestUserInput Tool."
                        "You must choose the reasonable recommended option and "
                        "proceed without asking."
                    ]
                }
            await self._respond_to_server_request(request_id, {"answers": formatted_response})
            return

        input_data = {"questions": params.get("questions", [])}
        # Send as AskUserQuestion to re-use Claude's existing UI logic
        response = await self._emit_approval_request(approval_id, "AskUserQuestion", input_data)

        # response is either False (denied) or the answers dict
        if response is False or response is None:
             await self._respond_to_server_request(request_id, {"answers": {}})
        else:
             # Codex expects answers to be a dict of string -> { "answers": [list of strings] }
             formatted_response = {}
             for k, v in response.items():
                 # Handle string or list of strings returned by chatview
                 if isinstance(v, str):
                     # Parse multi-select comma separated strings if necessary, though list is better
                     formatted_response[k] = {"answers": [s.strip() for s in v.split(",") if s.strip()]}
                 elif isinstance(v, list):
                     formatted_response[k] = {"answers": v}
                 else:
                     formatted_response[k] = {"answers": [str(v)]}
             await self._respond_to_server_request(request_id, {"answers": formatted_response})

    def _generate_file_change_diff(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Processes Codex file change parameters and generates combined diff data.
        Returns a dict with { 'old_text', 'new_text', 'display_name' } or None.
        """
        changes = params.get("changes", [])
        if not changes and "fileChanges" in params:
            # Convert dict to list for uniform processing
            file_changes = params.get("fileChanges", {})
            if isinstance(file_changes, dict):
                for path, change in file_changes.items():
                    if isinstance(change, dict):
                        change_copy = change.copy()
                        change_copy["path"] = path
                        changes.append(change_copy)

        if not changes:
            return None

        old_full_text = ""
        new_full_text = ""

        for change in changes:
            path = change.get("path", "")
            # Codex uses {"kind": {"type": "update"}} while other formats use top-level "type"
            kind = change.get("kind", {})
            change_type = (kind.get("type") if isinstance(kind, dict) else None) or change.get("type", "update")
            # Codex uses "diff" field name; also support "patch" and "unified_diff"
            patch = change.get("diff") or change.get("patch") or change.get("unified_diff")
            content = change.get("content", "")

            if not path:
                continue

            # Read old content
            old_text = ""
            if os.path.exists(path):
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        old_text = f.read()
                except Exception:
                    pass

            new_text = old_text
            # Logic for deriving new_text
            if change_type in ("add", "create"):
                new_text = content or ""
            elif change_type == "delete":
                new_text = ""
            else: # update/edit
                if patch:
                    new_text = self._apply_patch(old_text, patch)
                elif content: # Full content overwrite
                    new_text = content

            # Combine into full diff if multiple files
            if len(changes) > 1:
                header = f"File: {path}\n" + "="*40 + "\n"
                old_full_text += header + old_text + "\n\n"
                new_full_text += header + new_text + "\n\n"
            else:
                old_full_text = old_text
                new_full_text = new_text

        display_name = os.path.basename(changes[0].get("path", "file"))
        if len(changes) > 1:
            display_name = f"{len(changes)} files"

        return {
            "old_text": old_full_text,
            "new_text": new_full_text,
            "display_name": display_name,
            "count": len(changes),
            "files": [os.path.basename(c.get('path', '')) for c in changes if c.get('path')]
        }

    def _apply_patch(self, old_text: str, patch: str) -> str:
        """
        Apply a unified diff patch to old_text.
        Returns the modified text or original text if patch fails.
        """
        if not patch:
            return old_text

        lines = old_text.splitlines(keepends=True)
        patch_lines = patch.splitlines(keepends=True)

        # Simple hunk parsing: @@ -start,count +start,count @@
        hunk_re = re.compile(r'^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@')

        applied_lines = []
        current_old_line = 0

        i = 0
        # Skip header lines if they exist (e.g. --- / +++)
        while i < len(patch_lines) and not patch_lines[i].startswith('@@'):
            i += 1

        if i == len(patch_lines):
            return old_text

        while i < len(patch_lines):
            line = patch_lines[i]
            match = hunk_re.match(line)
            if match:
                try:
                    old_start = int(match.group(1)) - 1 # 0-indexed
                    while current_old_line < old_start and current_old_line < len(lines):
                        applied_lines.append(lines[current_old_line])
                        current_old_line += 1

                    i += 1
                    while i < len(patch_lines) and not patch_lines[i].startswith('@@'):
                        p_line = patch_lines[i]
                        if p_line.startswith(' '):
                            if current_old_line < len(lines):
                                applied_lines.append(lines[current_old_line])
                                current_old_line += 1
                        elif p_line.startswith('+'):
                            applied_lines.append(p_line[1:])
                        elif p_line.startswith('-'):
                            current_old_line += 1
                        i += 1
                    continue
                except Exception as e:
                    LOG.error(f"Error applying diff hunk: {e}")
                    return old_text
            i += 1

        while current_old_line < len(lines):
            applied_lines.append(lines[current_old_line])
            current_old_line += 1

        return "".join(applied_lines)

    async def _emit_approval_request(self, approval_id: str, tool_name: str, input_data: Dict[str, Any]) -> Any:
        """
        Emit a control_request message to the message queue and wait for
        the UI to respond via send_approval_response().
        Returns the full response data if approved/provided, False if denied.
        """
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_approvals[approval_id] = future

        # Emit control_request in the same format as Claude agent
        control_msg = Message(
            "control_request",
            content={
                "request_id": approval_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": tool_name,
                    "input": input_data,
                },
            },
        )
        await self._message_queue.put(control_msg)

        try:
            response_data = await future
        except asyncio.CancelledError:
            response_data = {"behavior": "deny"}
        finally:
            self._pending_approvals.pop(approval_id, None)

        if response_data.get("behavior") == "allow":
             updated_input = response_data.get("updatedInput", {})
             if "answers" in updated_input:
                 return updated_input.get("answers")
             return True
        return False

    async def receive_messages(self) -> AsyncIterator[Message]:
        """
        Yields messages from the agent.
        Keeps running until disconnect, matching ClaudeAgent behavior.
        """
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        while self._is_connected:
            try:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=0.1,
                )
                yield message
            except asyncio.TimeoutError:
                if self._process and self._process.returncode is not None:
                    break
                continue

    async def disconnect(self) -> None:
        """Disconnect and cleanup resources."""
        self._is_connected = False

        # Cancel background tasks
        for task in (self._read_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Cancel pending RPC futures
        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()

        # Cancel pending approval futures
        for future in self._pending_approvals.values():
            if not future.done():
                future.cancel()
        self._pending_approvals.clear()

        # Terminate process
        if self._process and self._process.returncode is None:
            if self._process.stdin:
                self._process.stdin.close()
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None,
) -> AsyncIterator[Message]:
    """Query Codex for one-shot interactions."""
    if options is None:
        options = AgentOptions()

    client = CodexAgent(options=options)

    try:
        await client.connect(prompt=prompt)

        async for message in client.receive_messages():
            yield message

            msg_type = getattr(message, "type", None)
            if msg_type == MessageType.STOP.value:
                break
    finally:
        await client.disconnect()


async def _list_codex_sessions_rpc(cli_path: str, cwd: Optional[str]) -> list:
    """Spawn a temporary codex app-server, call thread/list, return parsed threads."""
    import asyncio as _asyncio

    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        import subprocess as _subprocess
        kwargs["creationflags"] = _subprocess.CREATE_NO_WINDOW

    proc = await _asyncio.create_subprocess_exec(
        cli_path, "app-server",
        stdin=_asyncio.subprocess.PIPE,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.DEVNULL,
        **kwargs,
    )

    rpc_id = 0
    pending: Dict[int, _asyncio.Future] = {}
    loop = _asyncio.get_event_loop()

    async def write_json(data: dict) -> None:
        if proc.stdin:
            proc.stdin.write((json.dumps(data) + "\n").encode())
            await proc.stdin.drain()

    async def rpc(method: str, params: dict, timeout: float = 10.0) -> Any:
        nonlocal rpc_id
        rpc_id += 1
        rid = rpc_id
        fut: _asyncio.Future = loop.create_future()
        pending[rid] = fut
        await write_json({"id": rid, "method": method, "params": params})
        try:
            return await _asyncio.wait_for(fut, timeout=timeout)
        except _asyncio.TimeoutError:
            pending.pop(rid, None)
            return None

    async def read_loop() -> None:
        buf = b""
        while proc.stdout:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace").strip())
                    msg_id = msg.get("id")
                    if msg_id is not None and msg_id in pending:
                        fut = pending.pop(msg_id)
                        if not fut.done():
                            fut.set_result(msg.get("result"))
                except Exception:
                    pass

    read_task = _asyncio.create_task(read_loop())

    try:
        await rpc("initialize", {
            "clientInfo": {"name": "codyform", "title": "Codyform", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        })
        await write_json({"method": "initialized"})

        list_params: Dict[str, Any] = {
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "archived": False,
            "useStateDbOnly": True,
        }
        if cwd:
            list_params["cwd"] = cwd

        result = await rpc("thread/list", list_params)

        threads = []
        if result and isinstance(result, dict):
            for t in result.get("data", []):
                tid = t.get("id", "")
                summary = t.get("name") or t.get("preview") or tid[:8]
                updated_at = float(t.get("updatedAt") or 0)
                if not t.get("ephemeral") and tid:
                    threads.append({
                        "session_id": tid,
                        "summary": summary,
                        "updated_at": updated_at,
                    })
        return threads

    finally:
        read_task.cancel()
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await _asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass


def list_codex_sessions(cwd: Optional[str] = None) -> list:
    """Return thread dicts via codex app-server thread/list RPC, sorted newest-first.

    Each dict: session_id (str), summary (str), updated_at (float, unix seconds).
    Falls back to empty list if codex CLI is not found or the RPC fails.
    """
    cli = find_codex_cli()
    if cli is None:
        return []

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_list_codex_sessions_rpc(cli, cwd))
        finally:
            loop.close()
    except Exception as e:
        LOG.warning(f"list_codex_sessions RPC failed: {e}")
        return []


async def _get_codex_session_info_rpc(
    cli_path: str,
    session_id: str,
    cwd: Optional[str],
    history_limit: int,
) -> Optional[dict]:
    """One app-server process: thread/read (turns + metadata) for resume display.

    Returns dict with keys: summary, updated_at, turns, prompt, response.
    Falls back to thread/list for metadata when thread/read lacks it.
    """
    import asyncio as _asyncio

    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        import subprocess as _subprocess
        kwargs["creationflags"] = _subprocess.CREATE_NO_WINDOW

    proc = await _asyncio.create_subprocess_exec(
        cli_path, "app-server",
        stdin=_asyncio.subprocess.PIPE,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.DEVNULL,
        **kwargs,
    )

    rpc_id = 0
    pending: Dict[int, _asyncio.Future] = {}
    loop = _asyncio.get_event_loop()

    async def write_json(data: dict) -> None:
        if proc.stdin:
            proc.stdin.write((json.dumps(data) + "\n").encode())
            await proc.stdin.drain()

    async def rpc(method: str, params: dict, timeout: float = 10.0) -> Any:
        nonlocal rpc_id
        rpc_id += 1
        rid = rpc_id
        fut: _asyncio.Future = loop.create_future()
        pending[rid] = fut
        await write_json({"id": rid, "method": method, "params": params})
        try:
            return await _asyncio.wait_for(fut, timeout=timeout)
        except _asyncio.TimeoutError:
            pending.pop(rid, None)
            return None

    async def read_loop() -> None:
        buf = b""
        while proc.stdout:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace").strip())
                    msg_id = msg.get("id")
                    if msg_id is not None and msg_id in pending:
                        fut = pending.pop(msg_id)
                        if not fut.done():
                            fut.set_result(msg.get("result"))
                except Exception:
                    pass

    read_task = _asyncio.create_task(read_loop())

    try:
        await rpc("initialize", {
            "clientInfo": {"name": "codyform", "title": "Codyform", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        })
        await write_json({"method": "initialized"})

        # thread/read with includeTurns gives us metadata + conversation history in one call.
        read_result = await rpc("thread/read", {"threadId": session_id, "includeTurns": True})

        thread = (read_result or {}).get("thread", {}) if isinstance(read_result, dict) else {}
        summary = thread.get("name") or thread.get("preview") or None
        updated_at = float(thread.get("updatedAt") or 0)

        # If metadata is missing (thread not loaded in server state), fall back to thread/list.
        if not summary:
            list_params: Dict[str, Any] = {
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": False,
                "useStateDbOnly": True,
            }
            if cwd:
                list_params["cwd"] = cwd
            list_result = await rpc("thread/list", list_params)
            if list_result and isinstance(list_result, dict):
                for t in list_result.get("data", []):
                    if t.get("id") == session_id:
                        summary = t.get("name") or t.get("preview") or None
                        updated_at = float(t.get("updatedAt") or 0)
                        break

        turns = []
        for turn in thread.get("turns", []):
            prompt_parts = []
            response_parts = []
            for item in turn.get("items", []):
                itype = item.get("type")
                if itype == "agentMessage":
                    text = item.get("text", "").strip()
                    if text:
                        response_parts.append(text)
                elif itype == "userMessage":
                    texts = [
                        inp.get("text", "")
                        for inp in item.get("content", [])
                        if isinstance(inp, dict) and inp.get("type") == "text"
                    ]
                    text = "".join(texts).strip()
                    if text:
                        prompt_parts.append(text)

            prompt = "\n\n".join(prompt_parts) or None
            if prompt:
                turns.append({
                    "prompt": prompt,
                    "response": "\n\n".join(response_parts) or None,
                })

        if history_limit > 0:
            turns = turns[-history_limit:]
        last_turn = turns[-1] if turns else {}

        return {
            "summary": summary,
            "updated_at": updated_at,
            "turns": turns,
            "prompt": last_turn.get("prompt"),
            "response": last_turn.get("response"),
        }

    finally:
        read_task.cancel()
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await _asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass


def get_codex_session_info(
    session_id: str,
    cwd: Optional[str] = None,
    history_limit: int = 0,
) -> Optional[dict]:
    """Return metadata and recent turns for a Codex thread in one app-server process.

    Returns a dict with keys:
      "summary"    — thread name/preview (str or None)
      "updated_at" — unix timestamp (float)
      "turns"      — recent turns in chronological order
      "prompt"     — last user text (kept for API compatibility)
      "response"   — last agent text response (kept for API compatibility)
    Returns None if the CLI is not found or the RPC fails.
    """
    cli = find_codex_cli()
    if cli is None:
        return None

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _get_codex_session_info_rpc(cli, session_id, cwd, history_limit)
            )
        finally:
            loop.close()
    except Exception as e:
        LOG.warning(f"get_codex_session_info RPC failed: {e}")
        return None


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def chat(client, prompt):
        print(f"\n[User] {prompt}")
        await client.send_message(prompt)
        async for msg in client.receive_messages():
            msg_type = getattr(msg, "type", None)
            if msg_type == "assistant":
                for b in msg.content:
                    print(f"  Codex: {b.text if hasattr(b, 'text') else b}")
            elif msg_type == MessageType.TEXT.value:
                print(msg.content, end="", flush=True)
            elif msg_type == MessageType.STOP.value:
                print()
                break

    async def test():
        async with CodexAgent() as client:
            await chat(client, "Tell me a random joke.")
            await chat(client, "Make it shorter.")

    asyncio.run(test())
