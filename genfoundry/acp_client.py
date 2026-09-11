"""
ACP (Agent Client Protocol) SDK Client - Standard Library Implementation
Communicates with agents implementing the ACP JSON-RPC 2.0 protocol over stdio.
Supports configuring different agents via custom commands, with gemini-cli as the default/example.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Optional, Dict, Any, AsyncIterator, List, Union

from .base_agent import (
    BaseAgent,
    AgentOptions,
    Message,
)

LOG = logging.getLogger("TermMate")


def find_gemini_cli() -> Optional[str]:
    """Search common default install locations for the gemini CLI."""
    cli_on_path = shutil.which("gemini")
    if cli_on_path:
        return cli_on_path

    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(appdata, "npm", "gemini.cmd"),
            os.path.join(appdata, "npm", "gemini"),
            os.path.join(local_appdata, "Programs", "gemini", "gemini.exe"),
            os.path.join(local_appdata, "Programs", "gemini", "gemini.cmd"),
        ]
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "gemini"),
            os.path.join(home, ".npm-global", "bin", "gemini"),
            os.path.join(home, ".yarn", "bin", "gemini"),
            os.path.join(home, ".bun", "bin", "gemini"),
            "/usr/local/bin/gemini",
            "/usr/bin/gemini",
            "/opt/homebrew/bin/gemini",
            "/home/linuxbrew/.linuxbrew/bin/gemini",
        ]

    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found gemini CLI at default location: {path_str}")
            return path_str

    return None


def version_greater_or_equal(v1: str, v2: str) -> bool:
    """Compare two semantic version strings (v1 >= v2)."""
    try:
        parts1 = [int(x) for x in v1.split(".")]
        parts2 = [int(x) for x in v2.split(".")]
        max_len = max(len(parts1), len(parts2))
        parts1.extend([0] * (max_len - len(parts1)))
        parts2.extend([0] * (max_len - len(parts2)))
        return parts1 >= parts2
    except (ValueError, AttributeError):
        return False


def get_gemini_acp_flag(gemini_command: str, env: Optional[Dict[str, str]] = None) -> str:
    """
    Determine the correct ACP flag based on the gemini-cli version.
    Versions prior to 0.34.0 require the --experimental-acp flag.
    Version 0.34.0 and later use the stabilized --acp flag.
    """
    try:
        v_args = {}
        if sys.platform == "win32":
            v_args["creationflags"] = subprocess.CREATE_NO_WINDOW

        version_out = subprocess.check_output(
            [gemini_command, "--version"],
            env=env,
            universal_newlines=True,
            stderr=subprocess.STDOUT,
            **v_args,
        ).strip()
        match = re.search(r"(\d+\.\d+\.\d+)", version_out)
        if match:
            if not version_greater_or_equal(match.group(1), "0.34.0"):
                return "--experimental-acp"
    except Exception as e:
        LOG.warning(f"Failed to check gemini-cli version: {e}")
    return "--acp"


class AcpClient(BaseAgent):
    """
    Generic Agent Client Protocol (ACP) agent client over JSON-RPC 2.0 stdio.
    Can be instantiated with different commands to run different ACP agents.
    """

    def __init__(
        self,
        options: Optional[AgentOptions] = None,
        command: Optional[Union[str, List[str]]] = None,
        args: Optional[List[str]] = None,
        agent_name: Optional[str] = None,
    ):
        super().__init__(options)
        self.agent_name = agent_name or "gemini"
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()

        self._message_id = 0
        self._pending_requests: Dict[int, str] = {}
        self._pending_permissions: Dict[str, Dict[str, Any]] = {}
        self._session_id: Optional[str] = getattr(self.options, "session_id", None)
        self._init_future: Optional[asyncio.Future] = None
        self._session_future: Optional[asyncio.Future] = None

        self.agent_capabilities: Dict[str, Any] = {}
        self.agent_info: Dict[str, Any] = {}
        self.available_models: List[Dict[str, Any]] = []
        self.current_model_id: str = ""

        # Resolve command and arguments
        self.cmd_list = self._resolve_command(command, args)

    def _resolve_command(
        self,
        command: Optional[Union[str, List[str]]],
        args: Optional[List[str]],
    ) -> List[str]:
        """Resolve executable path and argument list."""
        cmd_raw = command or self.options.cli_path
        args_raw = list(args or getattr(self.options, "cli_args", []) or [])

        # Check if this agent is gemini or default
        is_gemini = (
            self.agent_name.lower() == "gemini"
            or (isinstance(cmd_raw, str) and "gemini" in os.path.basename(cmd_raw).lower())
            or (not cmd_raw)
        )

        if is_gemini:
            cli_path = (
                cmd_raw
                if (cmd_raw and shutil.which(cmd_raw))
                else (find_gemini_cli() or cmd_raw or "gemini")
            )
            # If args not specified, check version for --acp flag
            if not args_raw:
                flag = get_gemini_acp_flag(cli_path, self._build_env())
                args_raw = [flag]
            elif "--acp" not in args_raw and "--experimental-acp" not in args_raw:
                flag = get_gemini_acp_flag(cli_path, self._build_env())
                args_raw.append(flag)

            return [cli_path] + args_raw

        # General command handling
        if isinstance(cmd_raw, list):
            return list(cmd_raw) + args_raw
        if isinstance(cmd_raw, str):
            parts = shlex.split(cmd_raw)
            return parts + args_raw

        return ["gemini", "--acp"]

    def _build_env(self) -> Dict[str, str]:
        """Prepare process environment."""
        env = os.environ.copy()
        if self.options.extra_env:
            env.update(self.options.extra_env)

        # Gemini specific optimizations
        env.setdefault("GEMINI_TELEMETRY_ENABLED", "false")
        env.setdefault("GEMINI_CLI_NO_RELAUNCH", "true")

        # Inherit or forward API keys if set in env
        if "GOOGLE_API_KEY" not in env and "GEMINI_API_KEY" in env:
            env["GOOGLE_API_KEY"] = env["GEMINI_API_KEY"]
        elif "GEMINI_API_KEY" not in env and "GOOGLE_API_KEY" in env:
            env["GEMINI_API_KEY"] = env["GOOGLE_API_KEY"]

        return env

    def _next_message_id(self) -> int:
        self._message_id += 1
        return self._message_id

    async def connect(self, prompt: Optional[str] = None) -> None:
        """Spawn the agent process and perform initialize & session/new handshakes."""
        if self.is_connected:
            raise RuntimeError("Client is already connected")

        cmd = self.cmd_list
        LOG.info(f"Connecting AcpClient with command: {cmd}, cwd={self.options.cwd}")

        env = self._build_env()
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.options.cwd,
                **kwargs,
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"Agent command '{cmd[0]}' not found. Please install the CLI or check your configuration."
            ) from e

        self.is_connected = True
        loop = asyncio.get_running_loop()
        self._init_future = loop.create_future()
        self._session_future = loop.create_future()

        self._read_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # 1. Initialize
        await self._send_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False}
                },
            },
        )

        try:
            await asyncio.wait_for(self._init_future, timeout=30.0)
        except asyncio.TimeoutError:
            LOG.warning("AcpClient initialize timed out after 30s")

        # 2. Session new
        await self._send_request(
            "session/new",
            {
                "cwd": self.options.cwd,
                "mcpServers": [],
            },
        )

        try:
            await asyncio.wait_for(self._session_future, timeout=20.0)
        except asyncio.TimeoutError:
            LOG.warning("AcpClient session/new timed out after 20s")

        # 3. Apply model if configured
        if self.options.model and self._session_id:
            await self.set_model(self.options.model)

        # 4. Send initial prompt if provided
        if prompt:
            await self.send_message(prompt)

    async def send_message(
        self,
        content: str,
        parent_tool_use_id: Optional[str] = None,
        proceed_plan: bool = False,
    ) -> None:
        """Send a prompt to the current session."""
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        req_params = {
            "sessionId": self._session_id or "",
            "prompt": [{"type": "text", "text": content}],
        }
        await self._send_request("session/prompt", req_params)

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """Steer the conversation by sending a prompt message."""
        await self.send_message(text)

    async def interrupt(self) -> None:
        """Cancel current session turn."""
        if not self.is_connected:
            return

        if self._session_id:
            try:
                await self._send_request(
                    "session/cancel",
                    {"sessionId": self._session_id},
                )
            except Exception as e:
                LOG.error(f"Failed to send session/cancel: {e}")

    async def set_model(self, model_id: str) -> None:
        """Set the model for the current ACP session."""
        if not self.is_connected or not self._session_id:
            return
        await self._send_request(
            "session/set_model",
            {
                "sessionId": self._session_id,
                "modelId": model_id,
            },
        )

    async def send_approval_response(
        self, request_id: str, response_data: Dict[str, Any]
    ) -> None:
        """Translate permission decision and send ACP response."""
        perm_data = self._pending_permissions.pop(str(request_id), None)
        options = perm_data.get("options", []) if perm_data else []

        behavior = response_data.get("behavior", "deny")
        if behavior == "allow":
            # Select allow option
            selected_option = None
            for opt in options:
                kind = opt.get("kind", "").lower()
                if kind in ("allow_once", "allow", "allow_always"):
                    selected_option = opt
                    break
            if not selected_option:
                for opt in options:
                    opt_id = opt.get("optionId", "").lower()
                    if opt_id in ("proceed_once", "proceed_always", "allow"):
                        selected_option = opt
                        break
            if not selected_option and options:
                selected_option = options[0]

            option_id = selected_option.get("optionId", "proceed_once") if selected_option else "proceed_once"
            await self._send_response(
                int(request_id),
                {"outcome": {"outcome": "selected", "optionId": option_id}},
            )
        else:
            # Deny / cancel
            deny_option = None
            for opt in options:
                kind = opt.get("kind", "").lower()
                if kind in ("deny", "reject", "cancel"):
                    deny_option = opt
                    break
            if deny_option:
                await self._send_response(
                    int(request_id),
                    {"outcome": {"outcome": "selected", "optionId": deny_option.get("optionId")}},
                )
            else:
                await self._send_response(
                    int(request_id),
                    {"outcome": {"outcome": "cancelled"}},
                )

    async def disconnect(self) -> None:
        """Disconnect and clean up resources."""
        if not self.is_connected:
            return

        self.is_connected = False

        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        if self.process:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                except Exception:
                    pass

            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()

            self.process = None

    async def receive_messages(self) -> AsyncIterator[Message]:
        """Stream messages from the internal queue."""
        while self.is_connected:
            try:
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=0.1)
                yield msg
            except asyncio.TimeoutError:
                if self.process and self.process.returncode is not None:
                    break
                continue

    async def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None, msg_id: Optional[int] = None) -> int:
        if msg_id is None:
            msg_id = self._next_message_id()
        self._pending_requests[msg_id] = method

        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        await self._write_json(payload)
        return msg_id

    async def _send_response(self, msg_id: int, result: Any) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }
        await self._write_json(payload)

    async def _send_error_response(self, msg_id: int, code: int, message: str) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }
        await self._write_json(payload)

    async def _write_json(self, data: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Process stdin not available")

        line = json.dumps(data) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()

    async def _read_stdout(self) -> None:
        """Read newline-delimited JSON-RPC messages from agent stdout."""
        if not self.process or not self.process.stdout:
            return

        buffer = b""
        chunk_size = 65536

        try:
            while self.is_connected:
                chunk = await self.process.stdout.read(chunk_size)
                if not chunk:
                    break
                buffer += chunk

                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line_str = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue

                    try:
                        message = json.loads(line_str)
                        await self._handle_jsonrpc_message(message)
                    except json.JSONDecodeError:
                        LOG.warning(f"Acp non-json stdout: {line_str[:120]}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"Error in AcpClient read loop: {e}", exc_info=True)
            await self._message_queue.put(Message("error", content=str(e)))
        finally:
            if self.is_connected:
                await self._message_queue.put(Message("result", content={"success": True}))

    async def _read_stderr(self) -> None:
        """Capture stderr output for debugging and error reporting."""
        if not self.process or not self.process.stderr:
            return

        try:
            while self.is_connected:
                line = await self.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    LOG.info(f"AcpClient stderr: {text}")
                    # If process reports critical authentication or initialization errors
                    if "Error authenticating" in text or "must specify" in text:
                        await self._message_queue.put(Message("error", content=text))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _handle_jsonrpc_message(self, message: Dict[str, Any]) -> None:
        """Handle JSON-RPC 2.0 incoming payloads."""
        if self.options.debug_agent_message:
            LOG.debug(f"ACP payload: {message}")

        if "result" in message:
            await self._handle_result(message.get("id"), message["result"])
        elif "error" in message:
            await self._handle_error(message.get("id"), message["error"])
        else:
            method = message.get("method")
            if method == "session/update":
                await self._handle_session_update(message.get("params", {}).get("update", {}))
            elif method == "session/request_permission":
                await self._handle_permission_request(message)
            elif method == "fs/read_text_file":
                await self._handle_fs_read(message)
            elif method == "fs/write_text_file":
                await self._handle_fs_write(message)
            else:
                LOG.debug(f"Unprocessed ACP method: {method}")

    async def _handle_result(self, msg_id: Optional[int], result: Any) -> None:
        method = self._pending_requests.pop(msg_id, None)

        if method == "initialize":
            self.agent_capabilities = result.get("agentCapabilities", {})
            self.agent_info = result.get("agentInfo", {})
            LOG.info(f"AcpClient initialized: {self.agent_info.get('name', 'agent')} v{self.agent_info.get('version', '')}")
            if self._init_future and not self._init_future.done():
                self._init_future.set_result(result)

        elif method == "session/new":
            session_id = result.get("sessionId", "")
            if session_id:
                self._session_id = session_id
                await self._message_queue.put(
                    Message("system", content={"subtype": "init", "session_id": session_id})
                )

            models_info = result.get("models", {})
            if models_info:
                available = models_info.get("availableModels", [])
                self.current_model_id = models_info.get("currentModelId", "")
                term_models = []
                for m in available:
                    m_id = m.get("id") or m.get("modelId") or ""
                    m_name = m.get("name") or m_id
                    m_desc = m.get("description", "")
                    term_models.append({
                        "id": m_id,
                        "name": m_name,
                        "displayName": m_name,
                        "description": m_desc,
                        "value": m_id,
                    })
                if term_models:
                    self.available_models = term_models
                    await self._message_queue.put(
                        Message("models_update", content={"models": term_models})
                    )

            if self._session_future and not self._session_future.done():
                self._session_future.set_result(result)

        elif method == "session/prompt":
            stop_reason = result.get("stopReason", "end_turn")
            await self._message_queue.put(
                Message("result", content={"success": True, "stopReason": stop_reason})
            )

        elif method == "session/cancel":
            LOG.info("AcpClient session/cancel confirmed")

    async def _handle_error(self, msg_id: Optional[int], error: Dict[str, Any]) -> None:
        method = self._pending_requests.pop(msg_id, None)
        err_msg = error.get("message", "Unknown error")
        data = error.get("data")
        if isinstance(data, dict):
            details = ", ".join(f"{k}: {v}" for k, v in data.items())
            err_msg = f"{err_msg} ({details})"

        LOG.error(f"AcpClient RPC error for {method} (id={msg_id}): {err_msg}")

        if method == "initialize" and self._init_future and not self._init_future.done():
            self._init_future.set_exception(RuntimeError(err_msg))
        elif method == "session/new" and self._session_future and not self._session_future.done():
            self._session_future.set_exception(RuntimeError(err_msg))

        await self._message_queue.put(Message("error", content=err_msg))

    async def _handle_session_update(self, update: Dict[str, Any]) -> None:
        """Handle session/update streaming notifications."""
        update_type = update.get("sessionUpdate")

        if update_type == "agent_message_chunk":
            text = (update.get("content") or {}).get("text", "")
            if text:
                await self._message_queue.put(Message("text", content=text))

        elif update_type == "agent_thought_chunk":
            # Per requirement: think block does not need to be shown in chat view.
            # We emit a transient 'thinking' signal so status bar/spinner updates,
            # but do NOT emit text blocks into the chat view.
            await self._message_queue.put(Message("thinking", content=""))

        elif update_type in ("tool_call", "tool_call_update"):
            # ACP tool calls
            tool_data = {
                "name": update.get("title") or update.get("kind") or "tool",
                "kind": update.get("kind", "tool"),
                "status": update.get("status"),
                "toolCallId": update.get("toolCallId"),
                "title": update.get("title", ""),
                "content": update.get("content", []),
                "locations": update.get("locations", []),
                "rawInput": update.get("rawInput", {}),
            }
            await self._message_queue.put(Message("tool_use", content=tool_data))

    async def _handle_permission_request(self, message: Dict[str, Any]) -> None:
        """Handle session/request_permission by emitting a control_request."""
        msg_id = message.get("id")
        params = message.get("params", {})
        tool_call = params.get("toolCall", {})
        options = params.get("options", [])

        req_id_str = str(msg_id)
        self._pending_permissions[req_id_str] = {
            "options": options,
            "tool_call": tool_call,
            "msg_id": msg_id,
        }

        tool_name = tool_call.get("title") or tool_call.get("kind") or "Tool"
        input_data = tool_call.get("rawInput") or tool_call

        await self._message_queue.put(
            Message(
                "control_request",
                content={
                    "request_id": req_id_str,
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": tool_name,
                        "input": input_data,
                        "options": options,
                        "tool_call": tool_call,
                    },
                },
            )
        )

    async def _handle_fs_read(self, message: Dict[str, Any]) -> None:
        msg_id = message.get("id")
        params = message.get("params", {})
        file_path = params.get("path")
        try:
            if not file_path:
                raise ValueError("Missing path parameter")
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            await self._send_response(msg_id, {"content": content})
        except Exception as e:
            await self._send_error_response(msg_id, -32603, str(e))

    async def _handle_fs_write(self, message: Dict[str, Any]) -> None:
        msg_id = message.get("id")
        params = message.get("params", {})
        file_path = params.get("path")
        content = params.get("content", "")
        try:
            if not file_path:
                raise ValueError("Missing path parameter")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            await self._send_response(msg_id, {"success": True})
        except Exception as e:
            await self._send_error_response(msg_id, -32603, str(e))

