"""
Pi Agent SDK Client - Standard Library Implementation
A standard library implementation of the Pi Agent SDK that calls the pi CLI.
"""

import asyncio
import json
import os
import shutil
import sys
import logging
from typing import Optional, Dict, Any, AsyncIterator, List, Callable, Union

# logger by package name
LOG = logging.getLogger("TermMate")

from .base_agent import MessageType, Message, TextBlock, AssistantMessage, \
    PermissionResultAllow, PermissionResultDeny, ToolPermissionContext, AgentOptions, BaseAgent


def find_pi_cli() -> Optional[str]:
    """Search common default install locations for the pi CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "pi.cmd"),
                os.path.join(appdata, "npm", "pi")
            ])
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "pi"),
            os.path.join(home, ".npm-global", "bin", "pi"),
            os.path.join(home, ".yarn", "bin", "pi"),
            os.path.join(home, ".bun", "bin", "pi"),
            "/usr/local/bin/pi",
            "/usr/bin/pi",
            "/opt/homebrew/bin/pi",           # macOS (Intel/Apple Silicon)
            "/home/linuxbrew/.linuxbrew/bin/pi",  # Linux Homebrew
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found pi CLI at default location: {path_str}")
            return path_str
    
    return None


def version_greater_or_equal(version_str: str, target_str: str) -> bool:
    def parse_version(v: str) -> List[int]:
        return [int(x) for x in v.split('.')]
    try:
        return parse_version(version_str) >= parse_version(target_str)
    except Exception:
        return False


class PiAgent(BaseAgent):
    """
    Client for bidirectional, interactive conversations with Pi CLI.
    """

    def __init__(self, options: Optional[AgentOptions] = None):
        """Initialize Pi SDK client"""
        self.options = options or AgentOptions()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._session_id: Optional[str] = None

        # Plan mode: mutable at runtime
        self.plan_mode: bool = getattr(self.options, "plan_mode", False)

        # Find pi executable
        cli_command = self.options.cli_path or "pi"
        self.cli_path = shutil.which(cli_command) or find_pi_cli() or cli_command
        if not self.cli_path:
            raise FileNotFoundError(
                "Pi CLI not found in PATH."
            )

    def _get_session_flag(self) -> str:
        """
        Determine the correct session flag based on the pi CLI version.
        Versions prior to 0.76.0 use --session.
        Version 0.76.0 and later added --session-id.
        """
        try:
            import re
            import subprocess
            v_args = {}
            if sys.platform == 'win32':
                v_args['creationflags'] = subprocess.CREATE_NO_WINDOW

            version_out = subprocess.check_output(
                [self.cli_path, "--version"],
                universal_newlines=True,
                stderr=subprocess.STDOUT,
                **v_args
            ).strip()
            
            match = re.search(r'(\d+\.\d+\.\d+)', version_out)
            if match:
                if not version_greater_or_equal(match.group(1), "0.76.0"):
                    return "--session"
        except Exception as e:
            LOG.error(f"Failed to check pi version: {e}")
            
        return "--session-id"

    async def connect(self, prompt: Optional[str] = None) -> None:
        """Connect to Pi with an optional initial prompt."""
        if self.is_connected:
            raise RuntimeError("Client is already connected")

        # Build command arguments for rpc mode
        cmd = [
            self.cli_path,
            "--mode", "rpc",
        ]

        # Add session flag if session_id is provided
        if self.options.session_id:
            flag = self._get_session_flag()
            cmd.extend([flag, self.options.session_id])

        # Load built-in plan mode extension if available
        ext_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "extensions", "pi-plan-mode")
        if os.path.exists(ext_dir):
            cmd.extend(["--extension", ext_dir])

        # Load termchat extension for tool permission support
        termchat_ext_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "extensions", "termchat")
        if os.path.exists(termchat_ext_dir):
            cmd.extend(["--extension", termchat_ext_dir])

        # Enable plan mode via flag if configured
        if getattr(self.options, "plan_mode", False):
            cmd.append("--plan")

        # Add system prompt if specified
        system_prompt = self.options.system_prompt or ""
        
        if self.options.add_dirs:
            dirs_str = "\n".join(f"- {d}" for d in self.options.add_dirs)
            system_prompt += (
                f"\n\nYou also have access to the following additional workspace directories:\n"
                f"{dirs_str}\n"
                f"You can read, write, search, or run shell commands in these directories "
                f"using their absolute paths."
            )

        LOG.debug(f"Pi System prompt: {system_prompt}")

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        # Add model if specified
        if self.options.model:
            model_arg = self.options.model
            # Convert legacy 'provider:modelId' format to pi's expected 'provider/modelId' format
            if ":" in model_arg and "/" not in model_arg:
                model_arg = model_arg.replace(":", "/", 1)
            cmd.extend(["--model", model_arg])

        # Set up environment
        env = os.environ.copy()

        if hasattr(self.options, "approve_mode") and self.options.approve_mode:
            env["PI_TERMMATE_APPROVE_MODE"] = self.options.approve_mode

        # Inject user-defined extra environment variables
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

        # pi does not need an initialization control_request

        # Send initial prompt if provided
        if prompt:
            await self.send_message(prompt)

        # Request available models for TermMate dropdown
        import uuid
        await self._write_json({
            "type": "get_available_models",
            "id": str(uuid.uuid4())
        })

        # Request state to capture sessionId for TermMate
        await self._write_json({
            "type": "get_state",
            "id": str(uuid.uuid4())
        })

    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None, proceed_plan: bool = False) -> None:
        """Send a user message to Pi."""
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        import uuid
        message = {
            "type": "prompt",
            "message": content,
            "id": str(uuid.uuid4())
        }
        
        LOG.debug(f"PiAgent sending prompt: id={message['id']}")

        if parent_tool_use_id:
            message["parent_tool_use_id"] = parent_tool_use_id

        if self._session_id:
            message["session_id"] = self._session_id

        await self._write_json(message)

    async def set_model(self, model: str) -> None:
        """Dynamically set the model for the pi process."""
        if not self.is_connected:
            return
            
        provider = "anthropic" # fallback
        model_id = model
        
        if "/" in model:
            provider, model_id = model.split("/", 1)
        elif ":" in model:
            provider, model_id = model.split(":", 1)
            
        import uuid
        request = {
            "type": "set_model",
            "provider": provider,
            "modelId": model_id,
            "id": str(uuid.uuid4())
        }
        await self._write_json(request)

    async def set_plan_mode(self, plan_mode: bool) -> None:
        """Dynamically set the plan mode for the pi process."""
        if not self.is_connected:
            return
            
        self.plan_mode = plan_mode
        import uuid
        message = {
            "type": "prompt",
            "message": "/plan" if plan_mode else "/plan exit",
            "id": str(uuid.uuid4())
        }
        if self._session_id:
            message["session_id"] = self._session_id
        await self._write_json(message)

    async def apply_model_update(self, model: Optional[str]) -> None:
        await self.set_model(model)

    async def apply_plan_mode_update(self, enabled: bool) -> None:
        await self.set_plan_mode(enabled)

    async def send_permission_response(
        self,
        request_id: str,
        response_data: Dict[str, Any],
        is_extension_ui: bool = False,
    ) -> None:
        # Pi has no control_request/response protocol of its own -- every
        # permission response goes out as extension_ui_response.
        if is_extension_ui:
            pi_response_data = response_data
        else:
            behavior = response_data.get("behavior", "deny")
            pi_response_data = {"confirmed": True} if behavior == "allow" else {"cancelled": True}
        await self._write_extension_ui_response(request_id, pi_response_data)

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        
        if proceed_plan:
            self.plan_mode = False
            # Send /plan implement as a prompt so that the extension parses the command
            import uuid
            message = {
                "type": "prompt",
                "message": "/plan implement",
                "id": str(uuid.uuid4())
            }
            if self._session_id:
                message["session_id"] = self._session_id
            await self._write_json(message)
            return

        request = {
            "type": "steer",
            "message": text
        }
        await self._write_json(request)

    async def interrupt(self) -> None:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        LOG.info("PiAgent: Sending abort command")
        request = {
            "type": "abort"
        }
        await self._write_json(request)

    async def _write_json(self, data: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Process not available")

        json_line = json.dumps(data) + "\n"
        self.process.stdin.write(json_line.encode("utf-8"))
        await self.process.stdin.drain()

    async def _read_messages(self) -> None:
        if not self.process or not self.process.stdout:
            return
        chunk_limit = 65536
        buffer = b""

        try:
            while self.is_connected:
                try:
                    chunk = await self.process.stdout.read(chunk_limit)
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
                        data = json.loads(line)
                        message = self._parse_message(data)
                        if message is not None:
                            if self.options.debug_agent_message:
                                LOG.info(f"pi received: {message}")
                            await self._message_queue.put(message)
                    except json.JSONDecodeError:
                        LOG.error(f"pi non-json msg: {line[:200]}...")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"Reading messages error: {e}")
            error_msg = Message("error", content=str(e))
            await self._message_queue.put(error_msg)

    async def _read_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return

        try:
            while self.is_connected:
                line = await self.process.stderr.readline()
                if not line:
                    break

                line_str = line.decode("utf-8").strip()
                if line_str:
                    LOG.error(f"pi stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            pass

    def _parse_message(self, data: Dict[str, Any]) -> Optional[Message]:
        msg_type = data.get("type", "unknown")
        msg_id = data.get("id") or data.get("uuid")
        
        LOG.debug(f"PiAgent parse_message: type={msg_type}, id={msg_id}")

        def extract_error(err_str: str) -> str:
            if not err_str:
                return "Unknown error"
            
            extracted = err_str
            for _ in range(3):
                try:
                    if isinstance(extracted, str):
                        parsed = json.loads(extracted)
                    else:
                        parsed = extracted
                        
                    if isinstance(parsed, dict):
                        if "error" in parsed and isinstance(parsed["error"], dict):
                            extracted = parsed["error"].get("message", extracted)
                        elif "message" in parsed:
                            extracted = parsed["message"]
                        else:
                            break
                    else:
                        break
                except Exception:
                    break
                    
            return str(extracted).strip() if extracted else err_str

        if msg_type == "auto_retry_start":
            attempt = data.get("attempt")
            max_attempts = data.get("maxAttempts")
            delay = data.get("delayMs", 0) / 1000.0
            err_msg = extract_error(data.get("errorMessage", ""))
            msg = f"\n⚠️ Request failed ({err_msg}). Retrying (attempt {attempt}/{max_attempts}) in {delay}s...\n"
            return Message("text_delta", content=msg, msg_id=msg_id)

        if msg_type == "auto_retry_end":
            success = data.get("success")
            if not success:
                err = extract_error(data.get("finalError", ""))
                return Message("error", content=f"Auto retry failed after {data.get('attempt')} attempts.\n{err}", msg_id=msg_id)
            return None

        if msg_type == "response":
            if not data.get("success"):
                return Message("error", content=data.get("error", "Unknown RPC error"), msg_id=msg_id)
            
            if data.get("command") == "get_available_models":
                models_data = data.get("data", {}).get("models", [])
                term_models = []
                for m in models_data:
                    provider = m.get("provider", "unknown")
                    model_id = m.get("id", "unknown")
                    name = m.get("name") or model_id
                    desc = m.get("description", "")
                    
                    term_models.append({
                        "displayName": f"[{provider}]{name}",
                        "description": desc,
                        "value": f"{provider}/{model_id}"
                    })
                
                return Message("models_update", content={"models": term_models}, msg_id=msg_id)

            if data.get("command") == "get_state":
                state_data = data.get("data", {})
                session_id = state_data.get("sessionId")
                if session_id:
                    self._session_id = session_id
                    # Emit system init message so TermMate persists the session ID
                    return Message("system", content={"subtype": "init", "session_id": session_id}, msg_id=msg_id)
                return None

        if msg_type == "extension_ui_request":
            return Message(msg_type, content=data, msg_id=msg_id)

        if msg_type == "message_update":
            assistant_event = data.get("assistantMessageEvent", {})
            event_type = assistant_event.get("type")
            if event_type == "text_delta":
                delta_text = assistant_event.get("delta", "")
                if delta_text:
                    return Message("text_delta", content=delta_text, msg_id=msg_id)
            elif event_type == "text_end":
                return Message("text_end", content="", msg_id=msg_id)
            elif event_type == "thinking_start":
                return Message("thinking_start", content="", msg_id=msg_id)
            elif event_type == "thinking_delta":
                delta_text = assistant_event.get("delta", "")
                return Message("thinking_delta", content=delta_text, msg_id=msg_id)
            return None

        if msg_type == "message_end":
            message_data = data.get("message", {})
            role = message_data.get("role")
            
            if role == "assistant":
                content_blocks = message_data.get("content", [])

                blocks = []
                if isinstance(content_blocks, list):
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "toolCall":
                            new_block = block.copy()
                            new_block["type"] = "tool_use"
                            blocks.append(new_block)
                        # We omit text blocks here because they are streamed via text_delta

                assistant_msg = AssistantMessage(
                    content=blocks,
                    msg_id=message_data.get("id")
                )
                return assistant_msg

        if msg_type == "agent_end":
            err = data.get("errorMessage")
            if err:
                LOG.info(f"PiAgent ended with error: {err}")
                # We do not emit Message("error") here to avoid double-printing errors 
                # (since auto_retry_start and auto_retry_end handle them nicely).
                pass
            else:
                LOG.info("PiAgent ended successfully.")
            return Message("result", content={"success": True}, msg_id=msg_id)

        content = data.get("content") or data.get("message")
        return Message(msg_type, content, msg_id, **data)

    async def receive_messages(self) -> AsyncIterator[Message]:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        while self.is_connected:
            try:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=0.1
                )
                yield message
            except asyncio.TimeoutError:
                if self.process and self.process.returncode is not None:
                    break
                continue

    async def disconnect(self) -> None:
        if not self.is_connected:
            return

        self.is_connected = False

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

        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()


def list_pi_sessions(cwd: Optional[str] = None) -> list:
    """Return Pi session dicts for the given cwd, sorted newest-first.

    Each dict: session_id (str), mtime (float), summary (str).
    cwd=None returns sessions from all projects.
    """
    import glob as _glob

    sessions_root = os.path.join(os.path.expanduser("~"), ".pi", "agent", "sessions")
    if not os.path.isdir(sessions_root):
        return []

    if cwd:
        # Match Pi's encoding exactly: strip one leading slash/backslash, then replace
        # all slashes, backslashes, and colons (Windows drive letters) with dashes.
        import re as _re
        sanitized = _re.sub(r"^[/\\]", "", cwd)
        sanitized = _re.sub(r"[/\\:]", "-", sanitized)
        dir_name = f"--{sanitized}--"
        search_dirs = [os.path.join(sessions_root, dir_name)]
    else:
        search_dirs = [
            os.path.join(sessions_root, d)
            for d in os.listdir(sessions_root)
            if os.path.isdir(os.path.join(sessions_root, d))
        ]

    results = []
    for session_dir in search_dirs:
        if not os.path.isdir(session_dir):
            continue
        for fpath in _glob.glob(os.path.join(session_dir, "*.jsonl")):
            try:
                session_id = None
                first_message = None  # first user message text
                session_name = None   # user-set name via /session rename (session_info entries)
                last_activity_ms = None  # last message timestamp in ms
                mtime = os.path.getmtime(fpath)
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            entry = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        etype = entry.get("type")
                        if etype == "session" and not session_id:
                            session_id = entry.get("id")
                        elif etype == "session_info":
                            # Latest session_info name wins (Pi iterates all and takes last)
                            name = (entry.get("name") or "").strip()
                            session_name = name if name else None
                        elif etype == "message":
                            msg = entry.get("message", {})
                            role = msg.get("role")
                            if role in ("user", "assistant"):
                                # Track latest activity timestamp (ms epoch in message, or ISO in entry)
                                ts = msg.get("timestamp")
                                if isinstance(ts, (int, float)) and ts > 0:
                                    if last_activity_ms is None or ts > last_activity_ms:
                                        last_activity_ms = ts
                                else:
                                    entry_ts = entry.get("timestamp")
                                    if isinstance(entry_ts, str):
                                        try:
                                            import datetime as _dt
                                            t = _dt.datetime.fromisoformat(
                                                entry_ts.replace("Z", "+00:00")
                                            ).timestamp() * 1000
                                            if last_activity_ms is None or t > last_activity_ms:
                                                last_activity_ms = t
                                        except Exception:
                                            pass
                            if first_message is None and role == "user":
                                for block in msg.get("content", []):
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        text = block.get("text", "").strip()
                                        if text:
                                            first_message = text
                                            break
                if session_id:
                    if last_activity_ms is not None:
                        mtime = last_activity_ms / 1000.0
                    # Pi picker: prefer session_info name, fall back to first user message
                    summary = session_name or first_message or ""
                    results.append({
                        "session_id": session_id,
                        "mtime": mtime,
                        "summary": summary,
                    })
            except Exception as e:
                LOG.warning(f"list_pi_sessions: skipping {fpath}: {e}")

    results.sort(key=lambda s: s["mtime"], reverse=True)
    return results


def get_pi_session_tail(session_id: str, cwd: Optional[str] = None) -> Optional[dict]:
    """Return the last user prompt and assistant text response for a pi session.

    Returns a dict with keys:
      "prompt"   — last user text (str or None)
      "response" — last assistant text response (str or None)
    Returns None if the session file cannot be found.
    """
    import glob as _glob
    import re as _re

    sessions_root = os.path.join(os.path.expanduser("~"), ".pi", "agent", "sessions")
    if not os.path.isdir(sessions_root):
        return None

    if cwd:
        sanitized = _re.sub(r"^[/\\]", "", cwd)
        sanitized = _re.sub(r"[/\\:]", "-", sanitized)
        dir_name = f"--{sanitized}--"
        search_dirs = [os.path.join(sessions_root, dir_name)]
    else:
        search_dirs = [
            os.path.join(sessions_root, d)
            for d in os.listdir(sessions_root)
            if os.path.isdir(os.path.join(sessions_root, d))
        ]

    for session_dir in search_dirs:
        if not os.path.isdir(session_dir):
            continue
        for fpath in _glob.glob(os.path.join(session_dir, "*.jsonl")):
            try:
                found_id = None
                messages = []  # list of (role, text) tuples in order
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            entry = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        etype = entry.get("type")
                        if etype == "session" and not found_id:
                            found_id = entry.get("id")
                        elif etype == "message":
                            msg = entry.get("message", {})
                            role = msg.get("role")
                            if role == "user":
                                text = ""
                                for block in msg.get("content", []):
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        text += block.get("text", "")
                                messages.append(("user", text.strip()))
                            elif role == "assistant":
                                text = ""
                                for block in msg.get("content", []):
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        text += block.get("text", "")
                                messages.append(("assistant", text.strip()))

                if found_id == session_id:
                    # Find the last user message and the assistant response that follows it
                    last_prompt = None
                    last_response = None
                    for i in range(len(messages) - 1, -1, -1):
                        role, text = messages[i]
                        if role == "assistant" and last_response is None and text:
                            last_response = text
                        elif role == "user" and last_prompt is None and text:
                            last_prompt = text
                            break
                    return {"prompt": last_prompt, "response": last_response}
            except Exception as e:
                LOG.warning(f"get_pi_session_tail: skipping {fpath}: {e}")

    return None


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None
) -> AsyncIterator[Message]:
    if options is None:
        options = AgentOptions()

    client = PiAgent(options=options)

    try:
        await client.connect(prompt=prompt)

        async for message in client.receive_messages():
            yield message

            msg_type = getattr(message, 'type', None)
            if msg_type in ("stop", "result"):
                break
    finally:
        await client.disconnect()
