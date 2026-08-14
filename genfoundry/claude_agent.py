"""
Claude Agent SDK Client - Standard Library Implementation
A reimplementation of the Claude Agent SDK that calls the Claude Code CLI
using only Python standard libraries (no external dependencies).

Reference: https://github.com/anthropics/claude-agent-sdk-python
"""

import asyncio
import json
import os
import re
import shutil
import sys
import logging
import unicodedata
import uuid as _uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, AsyncIterator, List, Callable, Union
from enum import Enum

# logger by package name
LOG = logging.getLogger("TermMate")

from .base_agent import MessageType, Message, TextBlock, AssistantMessage, \
    PermissionResultAllow, PermissionResultDeny, ToolPermissionContext, AgentOptions, BaseAgent


def find_claude_cli() -> Optional[str]:
    """Search common default install locations for the claude CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "claude.cmd"),
                os.path.join(appdata, "npm", "claude")
            ])

    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "claude"),
            os.path.join(home, ".npm-global", "bin", "claude"),
            os.path.join(home, ".yarn", "bin", "claude"),
            os.path.join(home, ".bun", "bin", "claude"),
            "/usr/local/bin/claude",
            "/usr/bin/claude",
            "/opt/homebrew/bin/claude",           # macOS (Intel/Apple Silicon)
            "/home/linuxbrew/.linuxbrew/bin/claude",  # Linux Homebrew
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found claude CLI at default location: {path_str}")
            return path_str
    
    return None


class ClaudeCodeAgent(BaseAgent):
    """
    Client for bidirectional, interactive conversations with Claude Code.

    This client provides full control over the conversation flow with support
    for streaming, interrupts, and dynamic message sending.

    Key features:
    - Bidirectional: Send and receive messages at any time
    - Stateful: Maintains conversation context across messages
    - Interactive: Send follow-ups based on responses
    - Control flow: Support for interrupts and session management
    """

    def __init__(self, options: Optional[AgentOptions] = None):
        """Initialize Claude SDK client"""
        self.options = options or AgentOptions()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._session_id: Optional[str] = None
        self._permission_callback = self.options.can_use_tool

        # Find claude executable
        cli_command = self.options.cli_path or "claude"
        self.cli_path = shutil.which(cli_command) or find_claude_cli() or cli_command
        if not self.cli_path:
            raise FileNotFoundError(
                "Claude CLI not found. Please install it first:\n"
                "curl -fsSL https://claude.ai/install.sh | bash"
            )

    async def connect(self, prompt: Optional[str] = None) -> None:
        """
        Connect to Claude with an optional initial prompt.

        Args:
            prompt: Optional initial prompt to send after connection
        """
        if self.is_connected:
            raise RuntimeError("Client is already connected")

        # Build command arguments for streaming JSON mode
        # --output-format=stream-json: Stream JSON responses
        # --input-format=stream-json: Accept JSON input stream
        # --replay-user-messages: Echo user messages for acknowledgment
        # --verbose: Required when using stream-json output format
        # --permission-prompt-tool: Enable permission prompts for tool usage
        cmd = [
            self.cli_path,
            "--output-format=stream-json",
            "--input-format=stream-json",
            "--replay-user-messages",
            "--verbose",
            "--permission-prompt-tool=stdio",
        ]

        # Add resume flag if session_id is provided
        if self.options.session_id:
            cmd.extend(["--resume", self.options.session_id])

        # Add plan mode if enabled (overrides permission mode if strictly enforced)
        if self.options.plan_mode:
            cmd.extend(["--permission-mode", "plan"])

        # Add system prompt if specified
        if self.options.system_prompt:
            cmd.extend(["--system-prompt", self.options.system_prompt])

        # Add model if specified
        if self.options.model:
            cmd.extend(["--model", self.options.model])

        if self.options.add_dirs:
            for directory in self.options.add_dirs:
                cmd.extend(["--add-dir", str(directory)])

        # Add allowed tools if specified
        if self.options.allowed_tools:
            tools_str = ",".join(self.options.allowed_tools)
            cmd.extend(["--allowedTools", tools_str])

        # Set up environment
        env = os.environ.copy()
        env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"

        if self.options.enable_file_checkpoint:
            env["CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING"] = "true"

        # Inject user-defined extra environment variables (including ANTHROPIC_API_KEY etc.)
        if self.options.extra_env:
            env.update(self.options.extra_env)

        kwargs = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # Start subprocess
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.options.cwd,
            **kwargs
        )

        self.is_connected = True

        # Start background task to read messages
        self._read_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        # Send initialization control_request
        await self._send_initialize_request()

        # Send initial prompt if provided
        if prompt:
            await self.send_message(prompt)

    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None, proceed_plan: bool = False) -> None:
        """
        Send a user message to Claude.

        Args:
            content: The message content to send
            parent_tool_use_id: Optional parent tool use ID for tool results
            proceed_plan: Optional flag to temporarily disable plan mode for this message
        """
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        # Format message according to Claude CLI stream-json format
        message = {
            "type": "user",
            "message": {
                "role": "user",
                "content": content
            }
        }

        if parent_tool_use_id:
            message["parent_tool_use_id"] = parent_tool_use_id

        if self._session_id:
            message["session_id"] = self._session_id

        await self._write_json(message)

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """Steering is not yet implemented for Claude CLI."""
        pass

    async def set_permission_mode(self, mode: str) -> None:
        """
        Change permission mode during conversation.

        Args:
            mode: The permission mode to set. Valid options:
                - 'default': CLI prompts for dangerous tools
                - 'acceptEdits': Auto-accept file edits
                - 'bypassPermissions': Allow all tools (use with caution)
                - 'plan': Planning mode
        """
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        request = {
            "subtype": "set_permission_mode",
            "mode": mode,
        }
        await self._send_control_request(request)

    async def set_model(self, model: Optional[str] = None) -> None:
        """
        Change the AI model during conversation.

        Args:
            model: The model to use, or None to use default.
        """
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        request = {
            "subtype": "set_model",
            "model": model,
        }
        await self._send_control_request(request)

    async def rewind_files(self, user_message_id: str) -> None:
        """Restore all files modified after the given user message back to their
        pre-message state.  Requires enable_file_checkpoint=True on connect."""
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        await self._send_control_request({
            "subtype": "rewind_files",
            "user_message_id": user_message_id,
        })

    async def rewind(self, user_message_id: str) -> str:
        """Rewind to the given user message: restore files then fork the session.

        Performs the full "both" rewind sequence:
          1. rewind_files — restores on-disk files via the live agent subprocess
          2. fork_session_for_rewind — creates a truncated JSONL fork with fresh UUIDs

        Returns the new session ID to resume from.

        Requires enable_file_checkpoint=True in AgentOptions.
        """
        if self.is_connected:
            await self.rewind_files(user_message_id)

        # Use the session id we were told to resume (options), falling back to
        # what the CLI echoed in the init message.  After a first rewind the CLI
        # may echo the root session id instead of the fork id, so options.session_id
        # is the reliable pointer to the JSONL file on disk.
        source_session_id = self.options.session_id or self._session_id
        if not source_session_id:
            raise RuntimeError("No session ID available for rewind fork")

        loop = asyncio.get_event_loop()
        new_session_id = await loop.run_in_executor(
            None,
            fork_session_for_rewind,
            source_session_id,
            user_message_id,
            self.options.cwd,
        )
        return new_session_id

    async def interrupt(self) -> None:
        """
        Interrupt the current conversation or turn.
        """
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        LOG.info("ClaudeCodeAgent: Sending interrupt control request")
        request = {
            "subtype": "interrupt"
        }
        await self._send_control_request(request)

    async def _send_control_request(self, request: Dict[str, Any]) -> None:
        """Send a control request to Claude CLI"""
        import uuid
        request_id = f"req_{uuid.uuid4().hex[:8]}"

        control_request = {
            "type": "control_request",
            "request_id": request_id,
            "request": request
        }

        await self._write_json(control_request)
        LOG.info(f"[ctrl] sent {request.get('subtype')} request_id={request_id} data={request}")

    async def _write_json(self, data: Dict[str, Any]) -> None:
        """Write a JSON message to the subprocess stdin"""
        if not self.process or not self.process.stdin:
            raise RuntimeError("Process not available")

        json_line = json.dumps(data) + "\n"
        self.process.stdin.write(json_line.encode("utf-8"))
        await self.process.stdin.drain()

    async def _send_initialize_request(self) -> None:
        """Send initialization control_request to Claude CLI"""
        import uuid
        request_id = f"req_init_{uuid.uuid4().hex[:8]}"

        init_request = {
            "type": "control_request",
            "request_id": request_id,
            "request": {
                "subtype": "initialize",
                "hooks": None
            }
        }

        await self._write_json(init_request)
        LOG.debug(f"Sent initialization control_request: {request_id}")

    async def _deny_disallowed_tool(self, request_id: str, tool_name: str) -> None:
        if tool_name == "AskUserQuestion":
            message = "This is an automated run. You must make the decision yourself. Do not use AskUserQuestion Tool."
        else:
            message = f"Tool '{tool_name}' is disallowed in this session."

        response_data = {
            "behavior": "deny",
            "message": message
        }
        await self._send_control_response(request_id, response_data)

    async def _handle_permission_request(self, data: Dict[str, Any]) -> None:
        """Handle permission request from Claude CLI"""
        request_id = data.get("request_id")
        request = data.get("request", {})
        tool_name = request.get("tool_name")
        input_data = request.get("input", {})
        suggestions = request.get("permission_suggestions", [])

        # Create context
        context = ToolPermissionContext(suggestions=suggestions)

        try:
            # Call the permission callback if available
            if not self._permission_callback:
                return None

            result = await self._permission_callback(tool_name, input_data, context)

            response_data = {}
            # Send response based on result
            if isinstance(result, PermissionResultAllow):
                response_data = {
                    "behavior": "allow",
                    "updatedInput": (result.updated_input if result.updated_input is not None
                        else input_data),
                }
            elif isinstance(result, PermissionResultDeny):
                response_data = {
                    "behavior": "deny",
                    "message": result.message
                }

            await self._send_control_response(
                request_id=request_id,
                response_data=response_data
            )
        except Exception as e:
            LOG.error(f"Error in permission callback: {e}")
            # Default to deny on error
            await self._send_control_error(
                request_id=request_id,
                error=f"Permission callback error: {str(e)}"
            )

    async def _send_control_response(
        self,
        request_id: str,
        response_data: Dict[str, Any]
    ) -> None:
        """Send a success control response to Claude CLI"""
        response = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response_data
            }
        }
        await self._write_json(response)

    async def _send_control_error(
        self,
        request_id: str,
        error: str
    ) -> None:
        """Send an error control response to Claude CLI"""
        response = {
            "type": "control_response",
            "response": {
                "subtype": "error",
                "request_id": request_id,
                "error": error
            }
        }
        await self._write_json(response)


    async def _read_messages(self) -> None:
        """Background task to continuously read messages from Claude CLI"""
        if not self.process or not self.process.stdout:
            return
        chunk_limit = 65536
        buffer = b""

        try:
            while self.is_connected:
                # Read chunks instead of lines to avoid buffer limits
                try:
                    chunk = await self.process.stdout.read(chunk_limit)
                except Exception:
                    break

                if not chunk:
                    break

                buffer += chunk

                # Process lines from buffer
                while b"\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                        LOG.debug(f"claude msg: {data}")
                        message = self._parse_message(data)
                        if message is not None:
                            await self._message_queue.put(message)
                    except json.JSONDecodeError:
                        LOG.error(f"claude non-json msg: {line[:200]}...")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"Reading messages error: {e}")
            error_msg = Message("error", content=str(e))
            await self._message_queue.put(error_msg)

    async def _read_stderr(self) -> None:
        """Background task to read stderr from Claude CLI"""
        if not self.process or not self.process.stderr:
            return

        try:
            while self.is_connected:
                line = await self.process.stderr.readline()
                if not line:
                    break

                line_str = line.decode("utf-8").strip()
                if line_str:
                    # Optionally log stderr to a file or ignore
                    LOG.error(f"claude stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            pass  # Silently ignore stderr errors

    def _parse_message(self, data: Dict[str, Any]) -> Optional[Message]:
        """Parse raw JSON data into a Message object"""
        msg_type = data.get("type", "unknown")
        msg_id = data.get("id") or data.get("uuid")

        # Handle system init message - extract session_id
        if msg_type == "system" and data.get("subtype") == "init":
            self._session_id = data.get("session_id")
            return Message(msg_type, content=data, msg_id=msg_id)

        # Handle control_request message for permission callbacks
        if msg_type == "control_request":
            request_id = data.get("request_id")
            request = data.get("request", {})
            subtype = request.get("subtype")

            if subtype == "can_use_tool":
                tool_name = request.get("tool_name")
                if self.options.disallowed_tools and tool_name in self.options.disallowed_tools:
                    asyncio.create_task(self._deny_disallowed_tool(request_id, tool_name))
                    return None
                # Schedule permission callback handling if callback exists
                if self._permission_callback:
                    asyncio.create_task(self._handle_permission_request(data))

            return Message(msg_type, content=data, msg_id=msg_id)

        # Handle assistant message from Claude CLI stream-json format
        if msg_type == "assistant":
            # Extract the nested message object
            message_data = data.get("message", {})
            content_blocks = message_data.get("content", [])

            # Parse content blocks
            blocks = []
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        blocks.append(TextBlock(block.get("text", "")))
                    else:
                        blocks.append(block)

            assistant_msg = AssistantMessage(
                content=blocks,
                msg_id=message_data.get("id")
            )
            return assistant_msg

        # Handle result message (final status)
        if msg_type == "result":
            return Message(msg_type, content=data.get("result"), msg_id=msg_id)

        # Handle control_response message
        if msg_type == "control_response":
            response = data.get("response", {})
            subtype = response.get("subtype")
            request_id = response.get("request_id")
            if subtype == "error":
                LOG.error(f"[ctrl] control_response ERROR request_id={request_id} error={response.get('error')!r}")
            else:
                LOG.debug(f"[ctrl] control_response {subtype} request_id={request_id}")
            return Message(msg_type, content=data, msg_id=msg_id)

        # Handle user echo messages (from --replay-user-messages) — expose uuid
        if msg_type == "user":
            inner = data.get("message", {})
            user_uuid = data.get("uuid") or inner.get("uuid")
            kwargs = {"uuid": user_uuid} if user_uuid else {}
            return Message(msg_type, content=data, msg_id=user_uuid, **kwargs)

        # Handle other message types
        content = data.get("content") or data.get("message")
        return Message(msg_type, content, msg_id, **data)

    async def receive_messages(self) -> AsyncIterator[Message]:
        """
        Receive messages from Claude as an async iterator.

        Yields:
            Message objects from Claude
        """
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        while self.is_connected:
            try:
                # Wait for next message with timeout
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=0.1
                )
                yield message

                # Stop if we receive a stop message
                if message.type == "stop":
                    break
            except asyncio.TimeoutError:
                # Check if process is still alive
                if self.process and self.process.returncode is not None:
                    break
                continue

    async def disconnect(self) -> None:
        """Disconnect from Claude and cleanup resources"""
        if not self.is_connected:
            return

        self.is_connected = False

        # Cancel read tasks
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if hasattr(self, '_stderr_task') and self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass

        # Terminate process
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def __aenter__(self):
        """Async context manager entry"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.disconnect()


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None
) -> AsyncIterator[Message]:
    """
    Query Claude Code for one-shot or unidirectional streaming interactions.

    This function is ideal for simple, stateless queries where you don't need
    bidirectional communication or conversation management.

    Args:
        prompt: The prompt to send to Claude
        options: Optional configuration (defaults to AgentOptions() if None)

    Yields:
        Messages from the conversation

    Example:
        async for message in query(prompt="What is 2 + 2?"):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)
    """
    if options is None:
        options = AgentOptions()

    client = ClaudeCodeAgent(options=options)

    try:
        await client.connect(prompt=prompt)

        async for message in client.receive_messages():
            yield message

            # Stop if we receive a stop/result message
            msg_type = getattr(message, 'type', None)
            if msg_type in ("stop", "result"):
                break
    finally:
        await client.disconnect()



# session file helpers
# refer to session_mutations in agent sdk
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")
_MAX_SANITIZED_LENGTH = 200
_TRANSCRIPT_TYPES = frozenset({"user", "assistant", "attachment", "system", "progress"})


def _validate_session_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))


def _simple_hash(s: str) -> str:
    """32-bit hash to base36, matching the CLI's directory naming (TS simpleHash)."""
    h = 0
    for ch in s:
        h = (h << 5) - h + ord(ch)
        h = h & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    h = abs(h)
    if h == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = []
    n = h
    while n > 0:
        out.append(digits[n % 36])
        n //= 36
    return "".join(reversed(out))


def _sanitize_path(name: str) -> str:
    sanitized = _SANITIZE_RE.sub("-", name)
    if len(sanitized) <= _MAX_SANITIZED_LENGTH:
        return sanitized
    h = _simple_hash(name)
    return f"{sanitized[:_MAX_SANITIZED_LENGTH]}-{h}"


def _get_projects_dir() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(unicodedata.normalize("NFC", config_dir)) / "projects"
    return Path(unicodedata.normalize("NFC", str(Path.home() / ".claude"))) / "projects"


def _canonicalize_path(d: str) -> str:
    try:
        resolved = os.path.realpath(d)
        return unicodedata.normalize("NFC", resolved)
    except OSError:
        return unicodedata.normalize("NFC", d)


def _find_project_dir(project_path: str) -> Optional[Path]:
    projects_dir = _get_projects_dir()
    exact = projects_dir / _sanitize_path(project_path)
    if exact.is_dir():
        return exact
    # Long-path fallback: hash suffix may differ between Bun and Python
    sanitized = _sanitize_path(project_path)
    if len(sanitized) <= _MAX_SANITIZED_LENGTH:
        return None
    prefix = sanitized[:_MAX_SANITIZED_LENGTH]
    try:
        for entry in projects_dir.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix + "-"):
                return entry
    except OSError:
        pass
    return None


def _find_session_file_with_dir(
    session_id: str,
    cwd: Optional[str] = None,
) -> Optional[tuple]:
    """Return (file_path, project_dir) or None."""
    file_name = f"{session_id}.jsonl"

    def _try(project_dir: Path):
        p = project_dir / file_name
        try:
            if p.stat().st_size > 0:
                return (p, project_dir)
        except OSError:
            pass
        return None

    if cwd:
        canonical = _canonicalize_path(cwd)
        project_dir = _find_project_dir(canonical)
        if project_dir is not None:
            result = _try(project_dir)
            if result:
                return result
        return None

    projects_dir = _get_projects_dir()
    try:
        for entry in projects_dir.iterdir():
            if not entry.is_dir():
                continue
            result = _try(entry)
            if result:
                return result
    except OSError:
        pass
    return None


def _parse_fork_transcript(content: bytes, session_id: str):
    """Parse JSONL bytes into (transcript_entries, content_replacements)."""
    transcript = []
    content_replacements = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type in _TRANSCRIPT_TYPES and isinstance(entry.get("uuid"), str):
            transcript.append(entry)
        elif (
            entry_type == "content-replacement"
            and entry.get("sessionId") == session_id
            and isinstance(entry.get("replacements"), list)
        ):
            content_replacements.extend(entry["replacements"])
    return transcript, content_replacements


def _build_fork_lines(
    transcript: list,
    content_replacements: list,
    session_id: str,
    up_to_message_id: Optional[str],
) -> tuple:
    """Produce (forked_session_id, jsonl_lines) for a transcript fork.

    Strips sidechains, drops progress entries, updates sessionId, stamps
    forkedFrom.  UUIDs are kept as-is so that file checkpoints (keyed by
    the original user-message UUID) remain reachable on subsequent rewinds.
    """
    transcript = [e for e in transcript if not e.get("isSidechain")]

    if not transcript:
        raise ValueError(f"Session {session_id} has no messages to fork")

    if up_to_message_id:
        cutoff = -1
        for i, entry in enumerate(transcript):
            if entry.get("uuid") == up_to_message_id:
                cutoff = i
                break
        if cutoff == -1:
            raise ValueError(
                f"Message {up_to_message_id} not found in session {session_id}"
            )
        transcript = transcript[: cutoff + 1]

    # Only write non-progress entries
    writable = [e for e in transcript if e.get("type") != "progress"]
    if not writable:
        raise ValueError(f"Session {session_id} has no messages to fork")

    forked_session_id = str(_uuid_mod.uuid4())
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lines = []

    for i, original in enumerate(writable):
        timestamp = now if i == len(writable) - 1 else original.get("timestamp", now)

        forked = {
            **original,
            "sessionId": forked_session_id,
            "timestamp": timestamp,
            "isSidechain": False,
            "forkedFrom": {"sessionId": session_id, "messageUuid": original["uuid"]},
        }
        for key in ("teamName", "agentName", "slug", "sourceToolAssistantUUID"):
            forked.pop(key, None)

        lines.append(json.dumps(forked, separators=(",", ":")))

    if content_replacements:
        lines.append(json.dumps({
            "type": "content-replacement",
            "sessionId": forked_session_id,
            "replacements": content_replacements,
            "uuid": str(_uuid_mod.uuid4()),
            "timestamp": now,
        }, separators=(",", ":")))

    lines.append(json.dumps({
        "type": "custom-title",
        "sessionId": forked_session_id,
        "customTitle": "Rewind fork",
        "uuid": str(_uuid_mod.uuid4()),
        "timestamp": now,
    }, separators=(",", ":")))

    return forked_session_id, lines


def fork_session_for_rewind(
    session_id: str,
    up_to_message_uuid: str,
    cwd: Optional[str] = None,
) -> str:
    """Fork a Claude session up to (inclusive) the given user message UUID.

    Remaps all message UUIDs and preserves the parentUuid chain, matching
    the behaviour of claude-agent-sdk-python fork_session(). Returns the
    new session ID.

    Raises:
        FileNotFoundError: If the session file cannot be found.
        ValueError: If up_to_message_uuid is not found in the transcript.
    """
    if not _validate_session_uuid(session_id):
        raise ValueError(f"Invalid session_id: {session_id}")
    if not _validate_session_uuid(up_to_message_uuid):
        raise ValueError(f"Invalid up_to_message_uuid: {up_to_message_uuid}")

    source = _find_session_file_with_dir(session_id, cwd)
    if source is None:
        raise FileNotFoundError(f"Session {session_id} not found")

    file_path, project_dir = source
    content = file_path.read_bytes()
    if not content:
        raise ValueError(f"Session {session_id} has no messages to fork")

    transcript, content_replacements = _parse_fork_transcript(content, session_id)
    forked_session_id, lines = _build_fork_lines(
        transcript, content_replacements, session_id, up_to_message_uuid
    )

    fork_path = project_dir / f"{forked_session_id}.jsonl"
    fd = os.open(fork_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)

    LOG.info(f"[rewind] forked {session_id} -> {forked_session_id} ({len(lines)} entries)")
    return forked_session_id


_LITE_BUF = 65536
_SKIP_PROMPT_RE = re.compile(
    r"^(?:<local-command-stdout>|<session-start-hook>|<tick>|<goal>|"
    r"\[Request interrupted by user[^\]]*\]|"
    r"\s*<ide_opened_file>[\s\S]*</ide_opened_file>\s*$|"
    r"\s*<ide_selection>[\s\S]*</ide_selection>\s*$)"
)
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>")


def _extract_json_str(text: str, key: str) -> Optional[str]:
    """Extract last occurrence of a JSON string field without full parse."""
    import json as _json
    patterns = [f'"{key}":"', f'"{key}": "']
    last = None
    for pat in patterns:
        pos = 0
        while True:
            idx = text.find(pat, pos)
            if idx < 0:
                break
            start = idx + len(pat)
            i = start
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    raw = text[start:i]
                    try:
                        last = _json.loads(f'"{raw}"') if "\\" in raw else raw
                    except Exception:
                        last = raw
                    break
                i += 1
            pos = i + 1
    return last


def _extract_first_prompt(head: str) -> str:
    """Extract first meaningful user prompt from JSONL head chunk."""
    import json as _json
    cmd_fallback = ""
    for line in head.splitlines():
        if '"type":"user"' not in line and '"type": "user"' not in line:
            continue
        if '"tool_result"' in line:
            continue
        if '"isMeta":true' in line or '"isMeta": true' in line:
            continue
        if '"isCompactSummary":true' in line or '"isCompactSummary": true' in line:
            continue
        try:
            entry = _json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text" and isinstance(blk.get("text"), str):
                    texts.append(blk["text"])
        for raw in texts:
            result = raw.replace("\n", " ").strip()
            if not result:
                continue
            m = _COMMAND_NAME_RE.search(result)
            if m:
                if not cmd_fallback:
                    cmd_fallback = m.group(1)
                continue
            if _SKIP_PROMPT_RE.match(result):
                continue
            return result[:200].rstrip() + ("…" if len(result) > 200 else "")
    return cmd_fallback


def _read_session_lite(path: Path) -> Optional[dict]:
    """Read head+tail of a session file. Returns dict with mtime, head, tail or None."""
    try:
        with open(path, "rb") as f:
            import os as _os
            st = _os.fstat(f.fileno())
            size = st.st_size
            if size == 0:
                return None
            mtime = st.st_mtime
            head_bytes = f.read(_LITE_BUF)
            head = head_bytes.decode("utf-8", errors="replace")
            if size <= _LITE_BUF:
                tail = head
            else:
                f.seek(max(0, size - _LITE_BUF))
                tail = f.read(_LITE_BUF).decode("utf-8", errors="replace")
            return {"mtime": mtime, "head": head, "tail": tail}
    except OSError:
        return None


def _parse_session_entry(session_id: str, lite: dict) -> Optional[dict]:
    """Parse session metadata from head/tail. Returns dict or None for sidechains/empty."""
    head, tail = lite["head"], lite["tail"]
    # Filter sidechain sessions (first line check)
    first_line = head.split("\n", 1)[0]
    if '"isSidechain":true' in first_line or '"isSidechain": true' in first_line:
        return None
    # Summary priority: customTitle > lastPrompt > aiTitle > first_prompt
    custom_title = (_extract_json_str(tail, "customTitle") or _extract_json_str(head, "customTitle"))
    ai_title = (_extract_json_str(tail, "aiTitle") or _extract_json_str(head, "aiTitle"))
    last_prompt = _extract_json_str(tail, "lastPrompt")
    first_prompt = _extract_first_prompt(head) or None
    summary = custom_title or last_prompt or ai_title or first_prompt
    if not summary:
        return None
    return {
        "session_id": session_id,
        "mtime": lite["mtime"],
        "summary": summary,
        "custom_title": custom_title,
        "first_prompt": first_prompt,
    }


def get_claude_session_tail(
    session_id: str,
    cwd: Optional[str] = None,
    history_limit: int = 0,
) -> Optional[dict]:
    """Return recent user/assistant conversation turns for a Claude session.

    Returns a dict with keys:
      "turns"    — recent turns in chronological order
      "prompt"   — last user text (kept for API compatibility)
      "response" — last assistant text response (kept for API compatibility)
    Returns None if the session file cannot be found.

    Skips tool_result-only user messages and meta/system messages, mirroring
    the pi agent's get_pi_session_tail() behaviour.
    """
    result = _find_session_file_with_dir(session_id, cwd)
    if result is None:
        return None
    fpath, _project_dir = result

    entries = []
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                entries.append(entry)
    except Exception as e:
        LOG.warning(f"get_claude_session_tail: error reading {fpath}: {e}")
        return None

    # Claude transcripts are append-only trees. Rewinds leave the abandoned
    # branch in the JSONL file, and sidechain/team-agent messages can be mixed
    # into the same transcript. Select the latest main-chain leaf and walk its
    # parentUuid chain so the displayed history matches the resumed context.
    by_uuid = {
        entry["uuid"]: entry
        for entry in entries
        if isinstance(entry.get("uuid"), str)
    }
    if by_uuid:
        parent_uuids = {
            entry["parentUuid"]
            for entry in entries
            if isinstance(entry.get("parentUuid"), str)
        }
        entry_index = {
            entry["uuid"]: i
            for i, entry in enumerate(entries)
            if entry.get("uuid") in by_uuid
        }
        leaf_candidates = []
        for terminal in entries:
            uid = terminal.get("uuid")
            if not isinstance(uid, str) or uid in parent_uuids:
                continue
            current = terminal
            seen = set()
            while current is not None:
                current_uid = current.get("uuid")
                if not isinstance(current_uid, str) or current_uid in seen:
                    break
                seen.add(current_uid)
                if (
                    current.get("type") in ("user", "assistant")
                    and not current.get("isSidechain")
                    and not current.get("teamName")
                    and not current.get("isMeta")
                ):
                    leaf_candidates.append(current)
                    break
                parent_uuid = current.get("parentUuid")
                current = by_uuid.get(parent_uuid) if isinstance(parent_uuid, str) else None

        if leaf_candidates:
            leaf = max(leaf_candidates, key=lambda item: entry_index.get(item.get("uuid"), -1))
            active_entries = []
            current = leaf
            seen = set()
            while current is not None:
                current_uid = current.get("uuid")
                if not isinstance(current_uid, str) or current_uid in seen:
                    break
                seen.add(current_uid)
                active_entries.append(current)
                parent_uuid = current.get("parentUuid")
                current = by_uuid.get(parent_uuid) if isinstance(parent_uuid, str) else None
            entries = list(reversed(active_entries))

    messages = []  # list of (role, text) on the active branch
    try:
        for entry in entries:
            if entry.get("isSidechain") or entry.get("teamName"):
                continue
            etype = entry.get("type")
            if etype == "user":
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                # skip tool_result-only and meta messages
                if entry.get("isMeta") or entry.get("isCompactSummary"):
                    continue
                texts = []
                has_tool_result = False
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "tool_result":
                            has_tool_result = True
                        elif blk.get("type") == "text":
                            t = blk.get("text", "")
                            if t:
                                texts.append(t)
                if not texts and has_tool_result:
                    continue
                text = "".join(texts).strip()
                if text and not _SKIP_PROMPT_RE.match(text):
                    messages.append(("user", text))
            elif etype == "assistant":
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                texts = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            t = blk.get("text", "")
                            if t:
                                texts.append(t)
                text = "".join(texts).strip()
                if text:
                    messages.append(("assistant", text))
    except Exception as e:
        LOG.warning(f"get_claude_session_tail: error reading {fpath}: {e}")
        return None

    turns = []
    current = None
    for role, text in messages:
        if role == "user":
            if current is not None:
                turns.append(current)
            current = {"prompt": text, "response": None}
        elif current is not None:
            response = current["response"]
            current["response"] = f"{response}\n\n{text}" if response else text
    if current is not None:
        turns.append(current)

    if history_limit > 0:
        turns = turns[-history_limit:]
    last_turn = turns[-1] if turns else {}
    return {
        "turns": turns,
        "prompt": last_turn.get("prompt"),
        "response": last_turn.get("response"),
    }


def list_sessions_for_cwd(cwd: Optional[str] = None) -> list:
    """Return session dicts for the given cwd (or all projects if None), sorted newest-first.

    Each dict: session_id (str), mtime (float), summary (str), custom_title (str|None),
    first_prompt (str|None).
    Skips sidechain sessions and metadata-only sessions with no extractable summary.
    """
    def _scan_dir(project_dir: Path) -> list:
        results = []
        try:
            for entry in project_dir.iterdir():
                if not entry.name.endswith(".jsonl"):
                    continue
                session_id = entry.name[:-6]
                if not _validate_session_uuid(session_id):
                    continue
                lite = _read_session_lite(entry)
                if lite is None:
                    continue
                info = _parse_session_entry(session_id, lite)
                if info is not None:
                    results.append(info)
        except OSError:
            pass
        return results

    results = []
    if cwd:
        canonical = _canonicalize_path(cwd)
        project_dir = _find_project_dir(canonical)
        if project_dir is None:
            return []
        results = _scan_dir(project_dir)
    else:
        projects_dir = _get_projects_dir()
        try:
            for proj_dir in projects_dir.iterdir():
                if proj_dir.is_dir():
                    results.extend(_scan_dir(proj_dir))
        except OSError:
            pass

    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results


if __name__ == "__main__":
    # Windows compatibility
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    async def simple_query_example():
        """Example using the simple query() function"""
        print("Simple Query Example")
        print("=" * 50)
        print()

        message_count = 0
        async for message in query(prompt="Tell me about you in one sentence"):
            message_count += 1

            if isinstance(message, AssistantMessage):
                print("\nClaude: ", end="")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="")
                print()

        if message_count == 0:
            print("[WARNING] No messages received from Claude CLI!")

    # Or run the simple query example:
    asyncio.run(simple_query_example())
