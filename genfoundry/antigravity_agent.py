"""
Antigravity Agent SDK Client - Standard Library Implementation
A standard library implementation of the Antigravity Agent SDK that calls the agy CLI in headless mode.
Reference: https://antigravity.google/docs/cli/headless
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
from urllib.parse import urlparse, unquote
from typing import Optional, Dict, Any, AsyncIterator, List

# logger by package name
LOG = logging.getLogger("TermMate")

from .base_agent import (
    MessageType,
    Message,
    TextBlock,
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    AgentOptions,
    BaseAgent,
)


def find_antigravity_cli() -> Optional[str]:
    """Search PATH and common default install locations for the agy/antigravity CLI."""
    # 1. First check PATH for 'agy' and 'antigravity'
    which_agy = shutil.which("agy")
    if which_agy:
        return which_agy

    which_antigravity = shutil.which("antigravity")
    if which_antigravity:
        return which_antigravity

    # 2. Check candidate directories
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        userprofile = os.environ.get("USERPROFILE") or os.path.expanduser("~")

        if localappdata:
            candidates.append(os.path.join(localappdata, "agy", "bin", "agy.exe"))
            candidates.append(os.path.join(localappdata, "agy", "bin", "agy.cmd"))
            candidates.append(os.path.join(localappdata, "Programs", "antigravity", "bin", "agy.exe"))
            candidates.append(os.path.join(localappdata, "Programs", "antigravity", "bin", "agy.cmd"))
        if userprofile:
            candidates.append(os.path.join(userprofile, ".local", "bin", "agy.exe"))
            candidates.append(os.path.join(userprofile, ".local", "bin", "agy.cmd"))
            candidates.append(os.path.join(userprofile, ".local", "bin", "agy"))
            candidates.append(os.path.join(userprofile, ".gemini", "antigravity-cli", "bin", "agy.exe"))
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "agy.cmd"),
                os.path.join(appdata, "npm", "agy"),
                os.path.join(appdata, "npm", "antigravity.cmd"),
                os.path.join(appdata, "npm", "antigravity"),
            ])
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "agy"),
            os.path.join(home, ".local", "bin", "antigravity"),
            os.path.join(home, ".gemini", "antigravity-cli", "bin", "agy"),
            os.path.join(home, ".npm-global", "bin", "agy"),
            os.path.join(home, ".npm-global", "bin", "antigravity"),
            os.path.join(home, ".cargo", "bin", "agy"),
            "/usr/local/bin/agy",
            "/usr/bin/agy",
            "/opt/homebrew/bin/agy",
            "/home/linuxbrew/.linuxbrew/bin/agy",
            "/usr/local/bin/antigravity",
            "/usr/bin/antigravity",
            "/opt/homebrew/bin/antigravity",
            "/home/linuxbrew/.linuxbrew/bin/antigravity",
        ]

    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found Antigravity CLI at default location: {path_str}")
            return path_str

    return None


class AntigravityAgent(BaseAgent):
    """
    Client for bidirectional, interactive conversations with the Antigravity CLI (agy)
    using headless streaming JSON protocol.
    """

    def __init__(self, options: Optional[AgentOptions] = None):
        super().__init__(options)
        self.options = options or AgentOptions()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._read_stderr_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._session_id: Optional[str] = self.options.session_id
        self.plan_mode: bool = getattr(self.options, "plan_mode", False)
        self._stderr_lines: List[str] = []
        self._interrupted: bool = False

        # Resolve executable path
        if self.options.cli_path:
            self.cli_path = shutil.which(self.options.cli_path) or self.options.cli_path
        else:
            self.cli_path = (
                shutil.which("agy")
                or shutil.which("antigravity")
                or find_antigravity_cli()
                or "agy"
            )
        if not self.cli_path:
            raise FileNotFoundError(
                "Antigravity CLI ('agy') not found in PATH or standard installation locations."
            )

    def set_model(self, model: str) -> None:
        """Dynamically switch the model; takes effect on the next turn or reconnection."""
        self.options.model = model
        LOG.info(f"Antigravity model switched to: {model}")

    def set_think_level(self, level: str) -> None:
        """Dynamically switch reasoning effort (low, medium, high)."""
        self.options.think_level = level
        LOG.info(f"Antigravity reasoning effort switched to: {level}")

    def set_plan_mode(self, plan_mode: bool) -> None:
        """Dynamically toggle plan mode."""
        self.plan_mode = plan_mode
        self.options.plan_mode = plan_mode
        LOG.info(f"Antigravity plan_mode set to: {plan_mode}")

    def new_session(self) -> None:
        """Clear the current session ID for a fresh connection."""
        self._session_id = None
        self.options.session_id = None

    async def _write_json(self, data: Dict[str, Any]) -> None:
        """Write an NDJSON dictionary line to the subprocess stdin."""
        if not self.is_connected or not self.process or not self.process.stdin:
            return
        line = json.dumps(data) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()

    async def send_approval_response(self, request_id: str, response_data: Dict[str, Any]) -> None:
        """Send a permission/approval response to Antigravity CLI."""
        await self._write_json({
            "type": "permission_response",
            "request_id": request_id,
            "response": response_data,
        })

    async def _spawn_process(self) -> None:
        """Spawn the Antigravity CLI subprocess and start stdout/stderr reader tasks."""
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._read_stderr_task and not self._read_stderr_task.done():
            self._read_stderr_task.cancel()
            try:
                await self._read_stderr_task
            except asyncio.CancelledError:
                pass

        cmd = [
            self.cli_path,
            "--input-format", "stream-json",
            "--output-format", "stream-json",
        ]

        # Model selection
        if self.options.model:
            cmd.extend(["--model", self.options.model])

        # Reasoning effort (low, medium, high)
        effort = None
        if self.options.think_level:
            level = self.options.think_level.lower()
            if level in ("low", "medium", "high"):
                effort = level
            elif level not in ("auto", "default", "none", "off"):
                effort = level
            elif level in ("auto", "default") and self.options.model:
                effort = "high" if "pro" in self.options.model.lower() else "medium"
        elif self.options.model:
            # Models like gemini-3.8-flash require --effort (low, medium, high)
            effort = "high" if "pro" in self.options.model.lower() else "medium"

        if effort:
            cmd.extend(["--effort", effort])

        # Session / conversation resumption
        session_id = self.options.session_id or self._session_id
        if session_id:
            cmd.extend(["--conversation", session_id])

        # Tool permissions
        approve_mode = getattr(self.options, "approve_mode", "") or ""
        permission_mode = getattr(self.options, "permission_mode", "") or ""
        if approve_mode == "accept-all" or permission_mode in ("bypass", "accept-all"):
            cmd.append("--dangerously-skip-permissions")

        # Terminal sandbox
        if getattr(self.options, "sandbox_mode", False):
            cmd.append("--sandbox")

        # Workspace directories
        if getattr(self.options, "add_dirs", None):
            for d in self.options.add_dirs:
                if d:
                    cmd.extend(["--add-dir", d])

        # Plan mode
        if self.plan_mode:
            cmd.extend(["--mode", "plan"])

        # Environment setup
        env = os.environ.copy()
        cli_dir = os.path.dirname(self.cli_path)
        if cli_dir:
            env["PATH"] = cli_dir + os.pathsep + env.get("PATH", "")
        local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
        if self.options.extra_env:
            env.update(self.options.extra_env)

        LOG.info(f"Connecting AntigravityAgent with cmd: {cmd} (cwd: {self.options.cwd})")

        creationflags = 0
        if sys.platform == "win32":
            import subprocess
            creationflags = subprocess.CREATE_NO_WINDOW

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.options.cwd,
            env=env,
            creationflags=creationflags,
        )

        self._read_task = asyncio.create_task(self._read_stdout())
        self._read_stderr_task = asyncio.create_task(self._read_stderr())

    async def connect(self, prompt: Optional[str] = None) -> None:
        """Connect to Antigravity CLI in headless stream-json mode."""
        if self.is_connected and self.process and self.process.returncode is None:
            raise RuntimeError("Client is already connected")

        await self._spawn_process()
        self.is_connected = True

        if prompt:
            await self.send_message(prompt)

    async def send_message(
        self,
        content: str,
        parent_tool_use_id: Optional[str] = None,
        proceed_plan: bool = False,
    ) -> None:
        """Send a user prompt to the Antigravity CLI process on stdin."""
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        # If process was interrupted or exited, clean up and respawn with existing session_id
        if self._interrupted or not self.process or (self.process.returncode is not None and isinstance(self.process.returncode, int)):
            if self.process and self.process.returncode is None:
                try:
                    self.process.kill()
                    await self.process.wait()
                except Exception:
                    pass
            self._interrupted = False
            self.process = None
            if self._session_id:
                self.options.session_id = self._session_id
            await self._spawn_process()

        if proceed_plan:
            self.plan_mode = False
            self.options.plan_mode = False

        message = {
            "event": "user",
            "message": {
                "content": content,
            },
        }
        await self._write_json(message)

    @property
    def message_queue(self) -> asyncio.Queue:
        return self._message_queue

    async def _handle_json_event(self, event: Dict[str, Any]) -> None:
        """Process a single event dictionary and enqueue resulting messages."""
        if self._interrupted:
            # Drop all trailing events from dying process after user interrupted
            return
        messages = self._parse_event(event)
        for msg in messages:
            if self.options.debug_agent_message:
                LOG.info(f"agy received: {msg}")
            await self._message_queue.put(msg)

    async def _read_stdout(self) -> None:
        """Read and parse NDJSON messages from agy stdout."""
        if not self.process or not self.process.stdout:
            return

        try:
            while self.is_connected:
                line = await self.process.stdout.readline()
                if not line:
                    break

                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                try:
                    data = json.loads(line_str)
                    await self._handle_json_event(data)
                except json.JSONDecodeError:
                    LOG.error(f"agy non-json msg: {line_str[:200]}...")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not self._interrupted:
                LOG.error(f"Reading agy stdout error: {e}")
                await self._message_queue.put(Message("error", content=str(e)))

    async def _read_stderr(self) -> None:
        """Read diagnostics from agy stderr."""
        if not self.process or not self.process.stderr:
            return

        try:
            while self.is_connected:
                line = await self.process.stderr.readline()
                if not line:
                    break

                line_str = line.decode("utf-8").strip()
                if line_str:
                    self._stderr_lines.append(line_str)
                    if len(self._stderr_lines) > 50:
                        self._stderr_lines.pop(0)
                    LOG.debug(f"agy stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        """Convert an Antigravity headless streaming JSON event into Message objects."""
        event_type = data.get("event")
        messages: List[Message] = []

        if event_type == "init":
            self._session_id = data.get("conversation_id") or self._session_id
            self.options.session_id = self._session_id
            init_payload = data.get("init") or {}
            messages.append(
                Message(
                    "system",
                    content={
                        "subtype": "init",
                        "session_id": self._session_id,
                        "cwd": init_payload.get("cwd"),
                        "tools": init_payload.get("tools", []),
                    },
                    msg_id=self._session_id,
                    raw_data=data,
                )
            )

        elif event_type == "step_update":
            step_update = data.get("step_update") or data
            step_type = step_update.get("step_type")
            state = step_update.get("state")
            step_index = step_update.get("step_index")
            msg_id = str(step_index) if step_index is not None else None

            if step_type == "agent_response":
                text_delta = step_update.get("text_delta") or step_update.get("content")
                if text_delta:
                    messages.append(
                        Message(
                            "text_delta",
                            content=text_delta,
                            msg_id=msg_id,
                            raw_data=step_update,
                        )
                    )

            elif step_type == "tool":
                tool_call = step_update.get("tool_call") or {}
                tool_result = step_update.get("tool_result") or {}
                tool_info = step_update.get("tool_info") or {}

                tool_name = (
                    step_update.get("tool_name")
                    or tool_call.get("name")
                    or tool_info.get("name")
                    or "tool"
                )
                params = (
                    tool_call.get("args")
                    or tool_call.get("parameters")
                    or tool_info.get("parameters")
                    or {}
                )
                output = (
                    tool_result.get("output")
                    or tool_info.get("output")
                    or ""
                )
                error = (
                    tool_result.get("error")
                    or tool_info.get("error")
                )

                messages.append(
                    Message(
                        "tool_use",
                        content={
                            "id": tool_call.get("id") or tool_result.get("id") or msg_id,
                            "name": tool_name,
                            "parameters": params,
                            "output": output,
                            "error": error,
                            "tool_info": tool_info,
                        },
                        msg_id=msg_id,
                        raw_data=step_update,
                    )
                )

            elif step_type == "thinking":
                thinking_text = (
                    step_update.get("text_delta")
                    or step_update.get("thinking")
                    or step_update.get("content")
                    or ""
                )
                if thinking_text:
                    messages.append(
                        Message(
                            "thinking",
                            content=thinking_text,
                            msg_id=msg_id,
                            raw_data=step_update,
                        )
                    )

        elif event_type == "result":
            result = data.get("result") or data
            status = result.get("status")
            if status == "ERROR":
                err_msg = result.get("error") or "Antigravity agent execution failed."
                response_text = result.get("response") or ""
                has_response = bool(response_text.strip())
                # Only suppress the interrupted error when there is response
                if "stream was interrupted" in err_msg.lower() and has_response:
                    LOG.info(
                        f"Antigravity stream was interrupted after producing response: {err_msg}")
                else:
                    messages.append(
                        Message("error", content=err_msg, msg_id=self._session_id, raw_data=result)
                    )
            messages.append(
                Message(
                    "result",
                    content=result,
                    msg_id=self._session_id,
                    raw_data=result,
                )
            )

        return messages

    def _extract_exit_error(self) -> str:
        """Extract the most relevant user-facing error message from stderr on abnormal exit."""
        for line in reversed(self._stderr_lines):
            low = line.lower()
            if "error:" in low or "please sign in" in low or "authentication" in low:
                return line
        if self._stderr_lines:
            return self._stderr_lines[-1]
        rc = self.process.returncode if self.process else "unknown"
        return f"Antigravity CLI process exited unexpectedly (code {rc})."

    async def receive_messages(self) -> AsyncIterator[Message]:
        """Receive a stream of messages from the agent."""
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        has_yielded_result = False
        while self.is_connected:
            try:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=0.1,
                )
                if self._interrupted and message.type in ("tool_use", "thinking", "text_delta", "text"):
                    continue
                if message.type in ("stop", "result"):
                    has_yielded_result = True
                yield message
            except asyncio.TimeoutError:
                if self.process and self.process.returncode is not None:
                    # Drain remaining queue before handling exit
                    while not self._message_queue.empty():
                        msg = self._message_queue.get_nowait()
                        # If interrupted, drop trailing tool_use, text, thinking from the dying process
                        if self._interrupted and msg.type in ("tool_use", "thinking", "text_delta", "text"):
                            continue
                        if msg.type in ("stop", "result"):
                            has_yielded_result = True
                        yield msg

                    if self._interrupted:
                        # Clean up interrupted process reference, reset flag, and guarantee stop message
                        self._interrupted = False
                        self.process = None
                        yield Message("stop")
                        continue

                    if self.process.returncode != 0 and not has_yielded_result:
                        err_text = self._extract_exit_error()
                        yield Message("error", content=err_text)
                        yield Message("stop")
                    break
                continue

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """Send a steering message to the agent."""
        await self.send_message(text, proceed_plan=proceed_plan)

    async def interrupt(self) -> None:
        """Interrupt the active agent run."""
        if not self.is_connected:
            return

        self._interrupted = True
        proc = self.process
        if proc and proc.returncode is None:
            try:
                if sys.platform == "win32":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            except Exception as e:
                LOG.error(f"Failed to interrupt AntigravityAgent: {e}")

        # Clear any stale in-flight messages in queue before queueing stop
        while not self._message_queue.empty():
            try:
                self._message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Immediately enqueue stop message so UI stops loading animation
        await self._message_queue.put(Message("stop"))

    async def disconnect(self) -> None:
        """Disconnect and cleanly shut down the subprocess."""
        if not self.is_connected:
            return

        self.is_connected = False

        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._read_stderr_task:
            self._read_stderr_task.cancel()
            try:
                await self._read_stderr_task
            except asyncio.CancelledError:
                pass

        if self.process:
            if self.process.stdin:
                try:
                    self.process.stdin.close()
                    await self.process.stdin.wait_closed()
                except Exception:
                    pass

            if self.process.returncode is None:
                try:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                except ProcessLookupError:
                    pass
                except Exception as e:
                    LOG.error(f"Error terminating agy process: {e}")
            self.process = None


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None,
) -> AsyncIterator[Message]:
    """Convenience async generator to run a single prompt query against Antigravity CLI."""
    if options is None:
        options = AgentOptions()

    client = AntigravityAgent(options=options)
    try:
        await client.connect(prompt=prompt)
        async for message in client.receive_messages():
            yield message
            msg_type = getattr(message, "type", None)
            if msg_type in ("stop", "result"):
                break
    finally:
        await client.disconnect()


def _get_base_dir() -> str:
    """Return default ~/.gemini/antigravity-cli directory."""
    return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")


def _get_history_file() -> str:
    """Return default history.jsonl file path."""
    return os.path.join(_get_base_dir(), "history.jsonl")


def _get_transcript_paths(session_id: str) -> List[str]:
    """
    Return candidate transcript log paths for a conversation ID (antigravity-cli only).
    Prefers transcript_full.jsonl (lossless full turns) over transcript.jsonl (compact, contains <truncated N bytes>).
    """
    return [
        # Full untruncated transcript log (lossless)
        os.path.join(_get_base_dir(), "brain", session_id, ".system_generated", "logs", "transcript_full.jsonl"),
        # Compact transcript log fallback (token-optimized, contains <truncated N bytes>)
        os.path.join(_get_base_dir(), "brain", session_id, ".system_generated", "logs", "transcript.jsonl"),
    ]


def _clean_antigravity_prompt(content: str) -> str:
    """Strip XML wrappers like <USER_REQUEST> and normalize whitespace."""
    if not content:
        return ""
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
    if m:
        text = m.group(1).strip()
    else:
        text = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", content, flags=re.DOTALL)
        text = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", text, flags=re.DOTALL)
        text = re.sub(r"<user_information>.*?</user_information>", "", text, flags=re.DOTALL)
        text = text.strip()
    return " ".join(text.split())


def list_antigravity_sessions(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List past Antigravity conversations from ~/.gemini/antigravity-cli/brain and history.jsonl.
    Returns list of {'session_id': str, 'summary': str, 'mtime': float} sorted newest-first.
    """
    base_dir = _get_base_dir()
    brain_dir = os.path.join(base_dir, "brain")
    history_file = _get_history_file()
    last_conv_file = os.path.join(base_dir, "cache", "last_conversations.json")
    meta_file = os.path.join(base_dir, "cache", "conversation_metadata.json")

    ws_by_id: Dict[str, str] = {}

    # 1. Preload workspace mappings from cache/last_conversations.json
    if os.path.isfile(last_conv_file):
        try:
            with open(last_conv_file, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for ws, cid in data.items():
                        if isinstance(cid, str) and isinstance(ws, str):
                            ws_by_id[cid] = ws
        except Exception:
            pass

    # 2. Preload workspace mappings from cache/conversation_metadata.json
    if os.path.isfile(meta_file):
        try:
            with open(meta_file, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    for cid, c_data in data.get("conversations", {}).items():
                        if cid not in ws_by_id and isinstance(c_data, dict):
                            summary = c_data.get("summary", {})
                            uris = summary.get("WorkspaceURIs") or []
                            for u in uris:
                                if isinstance(u, str) and u.startswith("file://"):
                                    ws_by_id[cid] = unquote(urlparse(u).path)
                                    break
        except Exception:
            pass

    # 3. Preload workspace mappings from history.jsonl
    if os.path.isfile(history_file):
        try:
            with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    cid = entry.get("conversationId")
                    ws = entry.get("workspace")
                    if cid and ws and cid not in ws_by_id:
                        ws_by_id[cid] = ws
        except Exception:
            pass

    sessions_by_id: Dict[str, Dict[str, Any]] = {}

    # 4. Scan brain/ directory (standard headless and interactive session logs)
    if os.path.isdir(brain_dir):
        try:
            for entry in os.scandir(brain_dir):
                if not entry.is_dir():
                    continue
                cid = entry.name
                # Prefer lossless full log over compact truncated log
                t_path = os.path.join(entry.path, ".system_generated", "logs", "transcript_full.jsonl")
                if not os.path.isfile(t_path):
                    t_path = os.path.join(entry.path, ".system_generated", "logs", "transcript.jsonl")
                if not os.path.isfile(t_path):
                    continue

                try:
                    mtime = os.path.getmtime(t_path)
                except OSError:
                    continue

                ws = ws_by_id.get(cid)
                summary = ""
                cmd_fallback = ""

                try:
                    with open(t_path, "r", encoding="utf-8", errors="replace") as f:
                        for idx, line in enumerate(f):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                rec = json.loads(line)
                            except Exception:
                                continue

                            rec_type = rec.get("type")
                            content = rec.get("content") or ""

                            if rec_type == "USER_INPUT" and not summary:
                                cleaned = _clean_antigravity_prompt(content)
                                if cleaned:
                                    if cleaned.startswith("/"):
                                        if not cmd_fallback:
                                            cmd_fallback = cleaned
                                    else:
                                        summary = cleaned

                            if not ws:
                                for call in rec.get("tool_calls", []):
                                    args = call.get("args") or {}
                                    if isinstance(args, str):
                                        try:
                                            args = json.loads(args)
                                        except Exception:
                                            pass
                                    if isinstance(args, dict):
                                        for k in ("Cwd", "SearchPath", "DirectoryPath"):
                                            val = args.get(k)
                                            if val and isinstance(val, str):
                                                val = val.strip("\"'")
                                                if os.path.isabs(val) and not val.startswith(base_dir):
                                                    ws = val
                                                    break
                                        if not ws:
                                            val = args.get("AbsolutePath")
                                            if val and isinstance(val, str):
                                                val = val.strip("\"'")
                                                if os.path.isabs(val) and not val.startswith(base_dir):
                                                    ws = val if os.path.isdir(val) else os.path.dirname(val)
                                    if ws:
                                        break

                                if not ws:
                                    m = re.search(r"(/Users/[^\s\n\r\"'>]+)\s*->", content)
                                    if m:
                                        ws = m.group(1)

                            # Break early once we have both summary and workspace
                            if idx > 30 and (summary or cmd_fallback) and ws:
                                break
                except Exception as e:
                    LOG.debug(f"Error scanning transcript {t_path}: {e}")

                final_summary = summary or cmd_fallback
                sessions_by_id[cid] = {
                    "session_id": cid,
                    "summary": final_summary,
                    "mtime": mtime,
                    "workspace": ws,
                }
        except Exception as e:
            LOG.warning(f"Failed to scan antigravity brain dir: {e}")

    # 5. Merge any sessions recorded only in history.jsonl
    if os.path.isfile(history_file):
        try:
            with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue

                    cid = entry.get("conversationId")
                    if not cid:
                        continue

                    ws = entry.get("workspace")
                    timestamp_ms = entry.get("timestamp") or 0
                    mtime = float(timestamp_ms) / 1000.0 if timestamp_ms else 0.0
                    display = entry.get("display") or ""
                    cleaned = _clean_antigravity_prompt(display)

                    if cid in sessions_by_id:
                        curr = sessions_by_id[cid]
                        if not curr.get("workspace") and ws:
                            curr["workspace"] = ws
                        if not curr.get("summary") and cleaned and not cleaned.startswith("/"):
                            curr["summary"] = cleaned
                    else:
                        is_slash = cleaned.startswith("/")
                        sessions_by_id[cid] = {
                            "session_id": cid,
                            "summary": "" if is_slash else cleaned,
                            "mtime": mtime,
                            "workspace": ws,
                        }
        except Exception as e:
            LOG.warning(f"Failed to read antigravity history: {e}")

    # 6. Filter by cwd if requested
    results = []
    norm_cwd = os.path.normcase(os.path.realpath(cwd)) if cwd else None

    for s in sessions_by_id.values():
        ws = s.get("workspace")
        if norm_cwd:
            if not ws:
                continue
            norm_ws = os.path.normcase(os.path.realpath(ws))
            if norm_ws != norm_cwd and not norm_cwd.startswith(norm_ws + os.sep) and not norm_ws.startswith(norm_cwd + os.sep):
                continue

        results.append({
            "session_id": s["session_id"],
            "summary": s["summary"] or "(empty)",
            "mtime": s["mtime"],
        })

    # 7. Sort newest first (reverse chronological order)
    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results


def get_antigravity_session_tail(
    session_id: str,
    cwd: Optional[str] = None,
    history_limit: int = 50,
) -> Optional[Dict[str, Any]]:
    """Retrieve tail information for an Antigravity conversation."""
    sessions = list_antigravity_sessions(cwd)
    meta = next((s for s in sessions if s["session_id"] == session_id), None)
    if not meta and cwd:
        all_sessions = list_antigravity_sessions(None)
        meta = next((s for s in all_sessions if s["session_id"] == session_id), None)

    transcript_paths = _get_transcript_paths(session_id)

    turns = []
    current_prompt = None
    current_response = ""
    file_mtime = 0.0

    for path in transcript_paths:
        if os.path.isfile(path):
            try:
                file_mtime = os.path.getmtime(path)
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except Exception:
                            continue
                        rec_type = record.get("type")
                        content = record.get("content") or ""
                        if rec_type == "USER_INPUT":
                            if current_prompt is not None:
                                turns.append({"prompt": current_prompt, "response": current_response.strip()})
                            current_prompt = _clean_antigravity_prompt(content)
                            current_response = ""
                        elif rec_type in ("PLANNER_RESPONSE", "AGENT_RESPONSE"):
                            if content:
                                current_response += "\n" + content
                if current_prompt is not None:
                    turns.append({"prompt": current_prompt, "response": current_response.strip()})
                break
            except Exception as e:
                LOG.warning(f"Error reading transcript for session {session_id}: {e}")

    if not meta and not turns:
        return None

    if history_limit > 0 and len(turns) > history_limit:
        turns = turns[-history_limit:]

    summary = (meta.get("summary") if meta else "") or (turns[0]["prompt"][:60] if turns else "")
    mtime = (meta.get("mtime") if meta else None) or file_mtime

    return {
        "summary": summary,
        "mtime": mtime,
        "turns": turns,
    }
