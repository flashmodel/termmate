"""
OpenCode Agent - Server API Implementation.

This adapter starts ``opencode serve`` on an operating-system-assigned port
and talks to it through the HTTP/SSE server API.  It deliberately uses only
the Python standard library so it can run inside Sublime Text without extra
packages.
"""

import asyncio
import base64
import difflib
import json
import logging
import os
import queue as thread_queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .base_agent import AgentOptions, BaseAgent, Message, MessageType


LOG = logging.getLogger("TermMate")

_SERVER_URL_RE = re.compile(
    r"opencode server listening on\s+(https?://[^\s]+)", re.IGNORECASE
)

_TOOL_NAMES = {
    "bash": "Bash",
    "edit": "Edit",
    "write": "Write",
    "apply_patch": "Edit",
    "patch": "Edit",
    "read": "Read",
    "glob": "Glob",
    "grep": "Grep",
    "list": "Glob",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Task",
    "skill": "Skill",
    "question": "AskUserQuestion",
    "external_directory": "ExternalDirectory",
    "doom_loop": "DoomLoop",
}


def find_opencode_cli() -> Optional[str]:
    """Search common default install locations for the OpenCode CLI."""
    candidates: List[str] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "opencode.cmd"),
                os.path.join(appdata, "npm", "opencode"),
            ])
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".opencode", "bin", "opencode"),
            os.path.join(home, ".local", "bin", "opencode"),
            os.path.join(home, ".npm-global", "bin", "opencode"),
            os.path.join(home, ".yarn", "bin", "opencode"),
            os.path.join(home, ".bun", "bin", "opencode"),
            "/usr/local/bin/opencode",
            "/usr/bin/opencode",
            "/opt/homebrew/bin/opencode",
            "/home/linuxbrew/.linuxbrew/bin/opencode",
        ]

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            LOG.info("Found OpenCode CLI at default location: %s", path)
            return path
    return None


class OpenCodeHTTPError(RuntimeError):
    """An error response returned by the OpenCode server."""

    def __init__(self, method: str, url: str, status: Optional[int], detail: str):
        prefix = f"OpenCode {method} {url}"
        if status is not None:
            prefix += f" returned HTTP {status}"
        super().__init__(f"{prefix}: {detail}")
        self.status = status


class OpenCodeAgent(BaseAgent):
    """A persistent OpenCode client using REST requests and an SSE event stream."""

    def __init__(self, options: Optional[AgentOptions] = None):
        super().__init__(options)

        cli_command = self.options.cli_path or "opencode"
        self.cli_path = shutil.which(cli_command) or find_opencode_cli() or cli_command

        self.server_url: Optional[str] = None

        self._session_id: Optional[str] = self.options.session_id
        self.plan_mode = self.options.plan_mode
        self.available_models: List[Dict[str, Any]] = []

        self._is_connected = False
        self._turn_active = False
        self._turn_plan_mode = False
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._uses_session_status = False
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_task: Optional[asyncio.Task] = None

        self._server_process: Optional[asyncio.subprocess.Process] = None
        self._server_log_task: Optional[asyncio.Task] = None

        self._sse_thread: Optional[threading.Thread] = None
        self._sse_stop = threading.Event()
        self._sse_ready = threading.Event()
        self._sse_response = None
        self._sse_response_lock = threading.Lock()

        self._text_cache: Dict[str, str] = {}
        self._part_types: Dict[str, str] = {}
        self._message_roles: Dict[str, str] = {}
        self._user_message_ids = set()
        self._awaiting_user_message_id = False
        self._terminal_tools = set()
        self._permission_sessions: Dict[str, str] = {}
        self._seen_permissions = set()
        self._diff_cache: Dict[str, Any] = {}
        self._plan_text = ""

    @property
    def thread_id(self) -> Optional[str]:
        """Compatibility with AgentThread.session_id."""
        return self._session_id

    def _runtime_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(self.options.extra_env)

        inline: Dict[str, Any] = {}
        raw_inline = env.get("OPENCODE_CONFIG_CONTENT")
        if raw_inline:
            try:
                value = json.loads(raw_inline)
                if isinstance(value, dict):
                    inline = value
            except (TypeError, ValueError):
                LOG.warning("Ignoring invalid OPENCODE_CONFIG_CONTENT")

        permission: Dict[str, Any] = {"*": "ask"}
        for tool in self.options.allowed_tools:
            key = self._permission_key(tool)
            if key:
                permission[key] = "allow"

        approve_mode = self.options.approve_mode or "allow-edit"
        if approve_mode in ("allow-edit", "accept-all"):
            # OpenCode groups Edit, Write, ApplyPatch, and Patch under the
            # single `edit` permission key.
            permission["edit"] = "allow"

        if self.options.add_dirs:
            permission["external_directory"] = {
                os.path.join(os.path.abspath(path), "**"): "allow"
                for path in self.options.add_dirs
            }

        if "AskUserQuestion" in self.options.disallowed_tools:
            permission["question"] = "deny"

        inline["permission"] = permission
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
        return env

    @staticmethod
    def _permission_key(tool: str) -> Optional[str]:
        normalized = tool.replace("_", "").replace("-", "").lower()
        aliases = {
            "bash": "bash",
            "edit": "edit",
            "write": "edit",
            "applypatch": "edit",
            "read": "read",
            "glob": "glob",
            "grep": "grep",
            "webfetch": "webfetch",
            "websearch": "websearch",
            "task": "task",
            "skill": "skill",
            "todowrite": "todowrite",
            "todoread": "todowrite",
            "askuserquestion": "question",
        }
        return aliases.get(normalized)

    async def connect(self, prompt: Optional[str] = None) -> None:
        if self._is_connected:
            raise RuntimeError("Client is already connected")

        self._loop = asyncio.get_running_loop()
        env = self._runtime_env()

        self.server_url = await self._start_server(env)

        try:
            await self._finish_connect(env, prompt)
        except Exception:
            await self.disconnect()
            raise

    async def _finish_connect(
        self, env: Dict[str, str], prompt: Optional[str]
    ) -> None:
        await self._http("GET", "/global/health", include_directory=False)

        self._is_connected = True
        self._event_task = asyncio.create_task(self._process_events())
        self._start_sse_thread(env)

        ready = await self._loop.run_in_executor(
            None, lambda: self._sse_ready.wait(timeout=5.0)
        )
        if not ready:
            await self.disconnect()
            raise RuntimeError("Timed out connecting to the OpenCode event stream")

        # Do not create an empty server-side session merely because the chat
        # view connected.  A fresh session is created lazily when the first
        # prompt is sent.  Existing sessions still need to be validated here
        # so resume failures are reported during connection.
        if self._session_id:
            session = await self._http(
                "GET", f"/session/{quote(self._session_id, safe='')}"
            )
            session = self._unwrap(session)
            if not isinstance(session, dict) or not session.get("id"):
                raise RuntimeError("OpenCode did not return a valid session")

            self._session_id = session["id"]
            await self._message_queue.put(Message(
                "thread_started", content={"session_id": self._session_id}
            ))

        asyncio.create_task(self._fetch_models())

        if prompt:
            await self.send_message(prompt)

    async def _start_server(self, env: Dict[str, str]) -> str:
        cmd = [
            self.cli_path,
            "serve",
            "--hostname=127.0.0.1",
            "--port=0",
        ]
        LOG.info("Starting OpenCode server: %s", " ".join(cmd))

        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            # Sublime may terminate plugin_host without completing
            # plugin_unloaded(). Keep the server behind a pipe watchdog so
            # losing the parent still sends SIGTERM to the managed group.
            cmd = [
                "/bin/sh",
                "-c",
                (
                    'exec 3<&0; "$@" </dev/null & child=$!; '
                    '(cat <&3 >/dev/null; kill -TERM -$$ 2>/dev/null) & watcher=$!; '
                    'wait "$child"; status=$?; '
                    'kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null; '
                    'exit "$status"'
                ),
                "termmate-opencode-wrapper",
                *cmd,
            ]
            kwargs["stdin"] = asyncio.subprocess.PIPE
            kwargs["start_new_session"] = True

        self._server_process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self.options.cwd,
            env=env,
            **kwargs,
        )

        try:
            url = await asyncio.wait_for(self._read_server_url(), timeout=10.0)
        except Exception:
            await self._terminate_server()
            raise

        self._server_log_task = asyncio.create_task(self._drain_server_output())
        LOG.info("OpenCode server ready at %s", url)
        return url.rstrip("/")

    async def _read_server_url(self) -> str:
        process = self._server_process
        if not process or not process.stdout:
            raise RuntimeError("OpenCode server stdout is unavailable")

        output: List[str] = []
        while True:
            raw = await process.stdout.readline()
            if not raw:
                code = await process.wait()
                detail = "".join(output[-50:]).strip()
                raise RuntimeError(
                    f"OpenCode server exited with code {code} before startup"
                    + (f": {detail}" if detail else "")
                )

            line = raw.decode("utf-8", errors="replace")
            output.append(line)
            match = _SERVER_URL_RE.search(line)
            if match:
                return match.group(1)

    async def _drain_server_output(self) -> None:
        process = self._server_process
        if not process or not process.stdout:
            return
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                if self.options.debug_agent_message:
                    LOG.debug(
                        "opencode server: %s",
                        raw.decode("utf-8", errors="replace").rstrip(),
                    )
        except asyncio.CancelledError:
            pass

    def _auth_headers(self, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        source = env or {**os.environ, **self.options.extra_env}
        password = source.get("OPENCODE_SERVER_PASSWORD")
        if not password:
            return {}
        username = source.get("OPENCODE_SERVER_USERNAME", "opencode")
        token = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _build_url(
        self,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        include_directory: bool = True,
    ) -> str:
        if not self.server_url:
            raise RuntimeError("OpenCode server is not available")

        params = dict(query or {})
        if include_directory and self.options.cwd:
            params.setdefault("directory", self.options.cwd)
        suffix = "?" + urlencode(params, doseq=True) if params else ""
        return self.server_url + path + suffix

    async def _http(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        include_directory: bool = True,
        timeout: float = 30.0,
    ) -> Any:
        loop = self._loop or asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._http_sync(
                method, path, body, query, include_directory, timeout
            ),
        )

    def _http_sync(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]],
        query: Optional[Dict[str, Any]],
        include_directory: bool,
        timeout: float,
    ) -> Any:
        url = self._build_url(path, query, include_directory)
        headers = {"Accept": "application/json", **self._auth_headers()}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = self._error_text(parsed)
            except (TypeError, ValueError):
                pass
            raise OpenCodeHTTPError(method, url, error.code, detail) from error
        except URLError as error:
            raise OpenCodeHTTPError(method, url, None, str(error.reason)) from error

    @staticmethod
    def _unwrap(value: Any) -> Any:
        if isinstance(value, dict) and set(value).issubset({"data", "error"}):
            if value.get("error"):
                raise RuntimeError(OpenCodeAgent._error_text(value["error"]))
            return value.get("data")
        return value

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
            return str(error.get("message") or error.get("name") or error)
        return str(error)

    def _start_sse_thread(self, env: Dict[str, str]) -> None:
        self._sse_stop.clear()
        self._sse_ready.clear()
        self._sse_thread = threading.Thread(
            target=self._sse_worker,
            args=(env,),
            name="TermMate-OpenCode-SSE",
            daemon=True,
        )
        self._sse_thread.start()

    def _sse_worker(self, env: Dict[str, str]) -> None:
        reconnect_delay = 0.25
        while not self._sse_stop.is_set():
            url = self._build_url("/event")
            headers = {
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                **self._auth_headers(env),
            }
            request = Request(url, headers=headers, method="GET")
            try:
                response = urlopen(request, timeout=60.0)
                with self._sse_response_lock:
                    self._sse_response = response
                self._sse_ready.set()
                reconnect_delay = 0.25
                self._consume_sse(response)
            except Exception as error:
                if not self._sse_stop.is_set():
                    LOG.warning("OpenCode SSE disconnected: %s", error)
            finally:
                with self._sse_response_lock:
                    self._sse_response = None

            if not self._sse_stop.wait(reconnect_delay):
                reconnect_delay = min(reconnect_delay * 2, 5.0)

    def _consume_sse(self, response: Any) -> None:
        data_lines: List[str] = []
        while not self._sse_stop.is_set():
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if data_lines:
                    self._submit_sse_data("\n".join(data_lines))
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if data_lines:
            self._submit_sse_data("\n".join(data_lines))

    def _submit_sse_data(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            LOG.debug("Ignoring invalid OpenCode SSE data: %s", raw[:200])
            return
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._event_queue.put_nowait, event)

    async def _process_events(self) -> None:
        try:
            while self._is_connected:
                event = await self._event_queue.get()
                try:
                    await self._dispatch_event(event)
                except Exception as error:
                    LOG.error(
                        "Failed to process OpenCode event: %s", error, exc_info=True
                    )
                    await self._message_queue.put(
                        Message(MessageType.ERROR.value, content=str(error))
                    )
        except asyncio.CancelledError:
            pass

    def _event_payload(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload", event)
        return payload if isinstance(payload, dict) else {}

    def _belongs_to_session(self, properties: Dict[str, Any]) -> bool:
        session_id = properties.get("sessionID") or properties.get("sessionId")
        for key in ("part", "info", "permission"):
            nested = properties.get(key)
            if isinstance(nested, dict):
                session_id = session_id or nested.get("sessionID") or nested.get("sessionId")
        return not session_id or session_id == self._session_id

    @staticmethod
    def _message_id(properties: Dict[str, Any]) -> str:
        part = properties.get("part")
        if isinstance(part, dict):
            return str(part.get("messageID") or part.get("messageId") or "")
        return str(properties.get("messageID") or properties.get("messageId") or "")

    def _is_user_part(self, properties: Dict[str, Any]) -> bool:
        message_id = self._message_id(properties)
        return bool(
            message_id
            and (
                message_id in self._user_message_ids
                or self._message_roles.get(message_id) == "user"
            )
        )

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        payload = self._event_payload(event)
        event_type = payload.get("type", "")
        properties = payload.get("properties", {})
        if not isinstance(properties, dict):
            LOG.warning(
                "OpenCode event has invalid properties: type=%s payload=%r",
                event_type,
                payload,
            )
            return

        if not self._belongs_to_session(properties):
            return

        if event_type == "message.part.delta":
            if self._is_user_part(properties):
                return
            if properties.get("field") in (None, "text"):
                part_id = str(properties.get("partID") or "")
                delta = properties.get("delta", "")
                if delta:
                    self._text_cache[part_id] = self._text_cache.get(part_id, "") + delta
                    if self._part_types.get(part_id) == "reasoning":
                        await self._message_queue.put(Message(
                            MessageType.THINKING.value,
                            content=delta,
                            msg_id=part_id,
                        ))
                    else:
                        await self._emit_text_delta(delta, part_id)
            return

        if event_type == "message.part.updated":
            if self._is_user_part(properties):
                return
            await self._handle_part_updated(properties)
            return

        if event_type == "message.updated":
            info = properties.get("info", {})
            if isinstance(info, dict):
                message_id = str(info.get("id") or "")
                role = info.get("role")
                if message_id and role:
                    self._message_roles[message_id] = str(role)
                if message_id and role == "user":
                    self._user_message_ids.add(message_id)
                    if self._awaiting_user_message_id:
                        self._awaiting_user_message_id = False
                        await self._message_queue.put(Message(
                            "user_message_id",
                            content={"message_id": message_id},
                            msg_id=message_id,
                        ))
                if info.get("error"):
                    error_text = self._error_text(info["error"])
                    await self._message_queue.put(Message(
                        MessageType.ERROR.value,
                        content=error_text,
                    ))
            return

        if event_type in ("permission.asked", "permission.updated"):
            await self._handle_permission(properties)
            return

        if event_type == "session.diff":
            await self._handle_session_diff(properties.get("diff", []))
            return

        if event_type == "session.error":
            error = properties.get("error") or properties
            error_text = self._error_text(error)
            if self._turn_active:
                LOG.error(
                    "OpenCode conversation failed [session_id=%s, error=%r]",
                    self._session_id,
                    error_text,
                )
            await self._message_queue.put(Message(
                MessageType.ERROR.value, content=error_text
            ))
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()
            return

        # Newer OpenCode servers publish session.status(idle) followed by a
        # compatibility session.idle event.  Once the status protocol has
        # been observed, use it exclusively: waiting only for the legacy
        # event can deadlock a queued prompt if that event is missed, while
        # accepting both can let the trailing legacy event finish a new turn.
        turn_idle = False
        if event_type == "session.status":
            self._uses_session_status = True
            status = properties.get("status", {})
            turn_idle = isinstance(status, dict) and status.get("type") == "idle"
        elif event_type == "session.idle":
            turn_idle = not self._uses_session_status

        if turn_idle:
            if self._turn_active:
                LOG.info(
                    "OpenCode conversation succeeded [session_id=%s]",
                    self._session_id,
                )
            await self._finish_turn()

    async def _finish_turn(self) -> None:
        if not self._turn_active:
            return
        if self._awaiting_user_message_id:
            self._awaiting_user_message_id = False
        self._turn_active = False
        if self._turn_plan_mode and self._plan_text:
            await self._message_queue.put(Message(
                MessageType.PLAN_DELTA.value, content=self._plan_text
            ))
            self._plan_text = ""
        self._turn_plan_mode = False
        self._turn_done.set()
        await self._message_queue.put(Message(MessageType.STOP.value))

    async def _handle_part_updated(self, properties: Dict[str, Any]) -> None:
        part = properties.get("part", {})
        if not isinstance(part, dict):
            return
        part_type = part.get("type")
        part_id = str(part.get("id") or properties.get("partID") or "")
        if part_id and part_type:
            self._part_types[part_id] = part_type

        if part_type == "text":
            if part.get("ignored"):
                return
            full_text = part.get("text", "")
            previous = self._text_cache.get(part_id, "")
            if full_text:
                delta = full_text[len(previous):] if full_text.startswith(previous) else full_text
            else:
                delta = properties.get("delta") or ""
            self._text_cache[part_id] = full_text or previous + delta
            await self._emit_text_delta(delta or "", part_id)
            return

        if part_type == "reasoning":
            text = part.get("text", "")
            previous = self._text_cache.get(part_id, "")
            if text:
                delta = text[len(previous):] if text.startswith(previous) else text
            else:
                delta = properties.get("delta") or ""
            self._text_cache[part_id] = text or previous + delta
            if delta:
                await self._message_queue.put(Message(
                    MessageType.THINKING.value, content=delta, msg_id=part_id
                ))
            return

        if part_type == "tool":
            state = part.get("state", {})
            if not isinstance(state, dict):
                return
            status = state.get("status")
            # Running and pending tool states are intentionally not surfaced.
            if status not in ("completed", "error") or part_id in self._terminal_tools:
                return
            self._terminal_tools.add(part_id)
            await self._emit_completed_tool(part, state)

    async def _emit_text_delta(self, delta: str, msg_id: Optional[str]) -> None:
        if not delta:
            return
        if self._turn_plan_mode:
            self._plan_text += delta
        else:
            await self._message_queue.put(Message(
                MessageType.TEXT.value, content=delta, msg_id=msg_id
            ))

    async def _emit_completed_tool(
        self, part: Dict[str, Any], state: Dict[str, Any]
    ) -> None:
        raw_name = str(part.get("tool") or "tool")
        normalized = raw_name.lower()
        input_data = state.get("input", {})
        if not isinstance(input_data, dict):
            input_data = {"value": input_data}

        if normalized in ("bash", "shell", "command"):
            content = {
                "name": "command_execution",
                "command": input_data.get("command") or input_data.get("cmd") or state.get("title"),
                "output": state.get("output") or state.get("error"),
                "status": state.get("status"),
            }
        else:
            content = {
                "name": raw_name,
                "input": input_data,
                "title": state.get("title"),
                "output": state.get("output"),
                "error": state.get("error"),
                "metadata": state.get("metadata", {}),
                "status": state.get("status"),
            }
        await self._message_queue.put(Message(
            MessageType.TOOL_USE.value, content=content, msg_id=part.get("id")
        ))

    async def _handle_permission(self, properties: Dict[str, Any]) -> None:
        # Older servers may wrap the request in a ``permission`` object.
        # Current servers use ``permission`` as the string permission kind
        # (for example "bash") on the request itself.  Treat it as a wrapper
        # only when the value is actually a mapping.
        nested = properties.get("permission")
        permission = nested if isinstance(nested, dict) else properties
        permission_id = str(
            permission.get("id")
            or permission.get("requestID")
            or permission.get("permissionID")
            or ""
        )
        if not permission_id or permission_id in self._seen_permissions:
            return
        self._seen_permissions.add(permission_id)

        session_id = str(
            permission.get("sessionID")
            or properties.get("sessionID")
            or self._session_id
            or ""
        )
        self._permission_sessions[permission_id] = session_id
        permission_type = str(
            permission.get("type") or permission.get("permission") or "tool"
        )
        tool_name = _TOOL_NAMES.get(permission_type.lower(), permission_type)
        input_data = dict(permission.get("metadata") or {})
        patterns = permission.get("patterns") or permission.get("pattern")
        input_data.update({
            "title": permission.get("title", ""),
            "pattern": patterns,
            "permission_type": permission_type,
        })

        await self._message_queue.put(Message(
            "control_request",
            content={
                "request_id": permission_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": tool_name,
                    "input": input_data,
                },
            },
        ))

    async def send_approval_response(
        self, permission_id: str, response_data: Dict[str, Any]
    ) -> None:
        session_id = self._permission_sessions.pop(
            permission_id, self._session_id or ""
        )
        behavior = response_data.get("behavior", "deny")
        response = "once" if behavior == "allow" else "reject"
        body = {"reply": response}
        if response == "reject" and response_data.get("message"):
            body["message"] = response_data["message"]
        try:
            await self._http(
                "POST",
                f"/permission/{quote(str(permission_id), safe='')}/reply",
                body=body,
            )
        except OpenCodeHTTPError as error:
            if error.status not in (404, 405):
                raise
            # Compatibility with pre-v1 permission routes.
            await self._http(
                "POST",
                "/session/{}/permissions/{}".format(
                    quote(session_id, safe=""), quote(str(permission_id), safe="")
                ),
                body={"response": response},
            )

    async def _handle_session_diff(self, diffs: Any) -> None:
        if not isinstance(diffs, list):
            return
        changes = []
        for item in diffs:
            if not isinstance(item, dict) or not item.get("file"):
                continue
            path = item["file"]
            before = item.get("before", "")
            after = item.get("after", "")
            signature = (before, after)
            if self._diff_cache.get(path) == signature:
                continue
            self._diff_cache[path] = signature
            diff_text = "".join(difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=path,
                tofile=path,
            ))
            changes.append({
                "path": path,
                "oldText": before,
                "newText": after,
                "diff": diff_text,
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "kind": {"type": "update"},
            })
        if changes:
            await self._message_queue.put(Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "fileChange",
                    "changes": changes,
                    "filenames": [os.path.basename(c["path"]) for c in changes],
                    "status": "completed",
                },
            ))

    async def _fetch_models(self) -> None:
        try:
            result = self._unwrap(await self._http("GET", "/provider"))
            if not isinstance(result, dict):
                return
            connected = set(result.get("connected") or [])
            providers = result.get("all") or result.get("providers") or []
            models = []
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                provider_id = provider.get("id")
                if not provider_id or (connected and provider_id not in connected):
                    continue
                provider_name = provider.get("name") or provider_id
                raw_models = provider.get("models") or {}
                if isinstance(raw_models, dict):
                    iterator = raw_models.items()
                elif isinstance(raw_models, list):
                    iterator = ((m.get("id"), m) for m in raw_models if isinstance(m, dict))
                else:
                    continue
                for model_id, model in iterator:
                    if not model_id:
                        continue
                    model = model if isinstance(model, dict) else {}
                    models.append({
                        "displayName": model.get("name") or model_id,
                        "description": provider_name,
                        "value": f"{provider_id}/{model_id}",
                    })
            if models:
                self.available_models = models
                await self._message_queue.put(Message(
                    "models_update", content={"models": models}
                ))
        except Exception as error:
            LOG.warning("Failed to fetch OpenCode models: %s", error)

    def set_model(self, model: str) -> None:
        self.options.model = model

    async def set_plan_mode(self, plan_mode: bool) -> None:
        self.plan_mode = plan_mode
        if not plan_mode:
            self._plan_text = ""

    def _model_payload(self) -> Optional[Dict[str, str]]:
        model = self.options.model
        if not model or "/" not in model:
            return None
        provider_id, model_id = model.split("/", 1)
        if not provider_id or not model_id:
            return None
        return {"providerID": provider_id, "modelID": model_id}

    async def _ensure_session(self) -> str:
        """Create the server-side session on demand for the first prompt."""
        if self._session_id:
            return self._session_id

        session = await self._http("POST", "/session", body={})
        session = self._unwrap(session)
        if not isinstance(session, dict) or not session.get("id"):
            raise RuntimeError("OpenCode did not return a valid session")

        self._session_id = session["id"]
        await self._message_queue.put(Message(
            "thread_started", content={"session_id": self._session_id}
        ))
        return self._session_id

    async def send_message(
        self,
        content: str,
        parent_tool_use_id: Optional[str] = None,
        proceed_plan: bool = False,
    ) -> None:
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        # prompt_async returns as soon as OpenCode accepts the prompt, while
        # the session's generation loop keeps running.  Submitting another
        # prompt before session.idle can persist the user message without
        # starting a new generation, so serialize turns at the adapter edge.
        while self._turn_active:
            await self._turn_done.wait()
        if not self._is_connected:
            raise RuntimeError("Client disconnected while waiting for the previous turn.")

        session_id = await self._ensure_session()

        agent_name = "build" if proceed_plan or not self.plan_mode else "plan"
        body: Dict[str, Any] = {
            "agent": agent_name,
            "parts": [{"type": "text", "text": content}],
        }
        model = self._model_payload()
        if model:
            body["model"] = model
        if self.options.system_prompt:
            body["system"] = self.options.system_prompt

        disabled = {}
        for tool in self.options.disallowed_tools:
            key = self._permission_key(tool)
            if key:
                disabled[key] = False
        if disabled:
            body["tools"] = disabled

        self._plan_text = ""
        self._turn_plan_mode = agent_name == "plan"
        self._turn_active = True
        self._turn_done.clear()
        self._awaiting_user_message_id = True
        await self._message_queue.put(Message(
            "turn_started",
            content={},
        ))
        try:
            await self._http(
                "POST",
                f"/session/{quote(session_id, safe='')}/prompt_async",
                body=body,
            )
        except Exception as error:
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()
            await self._message_queue.put(
                Message(MessageType.ERROR.value, content=str(error))
            )
            raise

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        await self.send_message(text, proceed_plan=proceed_plan)

    async def interrupt(self) -> None:
        if self._is_connected and self._session_id:
            await self._http(
                "POST", f"/session/{quote(self._session_id, safe='')}/abort", body={}
            )
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()

    async def new_session(self) -> str:
        """Create and switch to a fresh session without reconnecting the client."""
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        if self._turn_active:
            await self.interrupt()

        session = await self._http("POST", "/session", body={})
        session = self._unwrap(session)
        if not isinstance(session, dict) or not session.get("id"):
            raise RuntimeError("OpenCode did not return a valid session")

        # Switch the event filter before clearing the old session's caches so
        # any late SSE events from it are ignored by _belongs_to_session().
        self._session_id = session["id"]
        self.options.session_id = None

        self._turn_active = False
        self._turn_plan_mode = False
        self._turn_done.set()
        self._uses_session_status = False
        self._awaiting_user_message_id = False
        self._text_cache.clear()
        self._part_types.clear()
        self._message_roles.clear()
        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._permission_sessions.clear()
        self._seen_permissions.clear()
        self._diff_cache.clear()
        self._plan_text = ""

        await self._message_queue.put(Message(
            "thread_started", content={"session_id": self._session_id}
        ))
        return self._session_id

    async def rewind(self, message_id: str) -> Optional[str]:
        if not self._is_connected or not self._session_id:
            raise RuntimeError("Client is not connected or has no active session")
        await self._http(
            "POST",
            f"/session/{quote(self._session_id, safe='')}/revert",
            body={"messageID": message_id},
        )
        self._turn_active = False
        self._turn_plan_mode = False
        self._awaiting_user_message_id = False
        self._turn_done.set()
        self._text_cache.clear()
        self._part_types.clear()
        self._message_roles.clear()
        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._diff_cache.clear()
        return self._session_id

    async def receive_messages(self) -> AsyncIterator[Message]:
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        while self._is_connected:
            try:
                yield await asyncio.wait_for(self._message_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if self._server_process and self._server_process.returncode is not None:
                    break

    async def _terminate_server(self) -> None:
        process = self._server_process
        if not process:
            return
        if process.returncode is None:
            if sys.platform == "win32":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await killer.wait()
                try:
                    exit_code = await asyncio.wait_for(process.wait(), timeout=2.0)
                    LOG.info(
                        "OpenCode server process exited: pid=%s returncode=%s",
                        process.pid,
                        exit_code,
                    )
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timed out waiting for OpenCode server process to exit; "
                        "killing: pid=%s",
                        process.pid,
                    )
                    process.kill()
                    await process.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    exit_code = await asyncio.wait_for(process.wait(), timeout=3.0)
                    LOG.info(
                        "OpenCode server process exited: pid=%s returncode=%s",
                        process.pid,
                        exit_code,
                    )
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timed out waiting for OpenCode server process to exit; "
                        "killing: pid=%s",
                        process.pid,
                    )
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
        self._server_process = None

    def terminate_server_now(self) -> None:
        """Synchronously signal the managed server during plugin unload."""
        process = self._server_process
        if not process or process.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                os.kill(process.pid, signal.SIGTERM)
                return
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    async def disconnect(self) -> None:
        self._is_connected = False
        self._turn_active = False
        self._turn_plan_mode = False
        self._awaiting_user_message_id = False
        self._turn_done.set()

        self._sse_stop.set()
        with self._sse_response_lock:
            response = self._sse_response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

        if self._sse_thread and self._sse_thread.is_alive():
            loop = self._loop or asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._sse_thread.join(timeout=2.0))
        self._sse_thread = None

        if self._event_task and not self._event_task.done():
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        self._event_task = None

        # Keep consuming stdout until the server exits.  In particular, the
        # Windows Proactor loop needs to observe EOF and finish its pipe-close
        # callbacks before the owning AgentThread closes the event loop.
        # Cancelling this task first leaves the subprocess transport to be
        # finalized after loop.close(), which produces "Event loop is closed"
        # warnings from BaseSubprocessTransport.__del__.
        server_log_task = self._server_log_task
        await self._terminate_server()
        if server_log_task:
            try:
                await asyncio.wait_for(server_log_task, timeout=2.0)
            except asyncio.TimeoutError:
                LOG.warning("Timed out draining OpenCode server output")
            except asyncio.CancelledError:
                pass
            except Exception as error:
                LOG.debug("OpenCode server output drain failed: %s", error)
        self._server_log_task = None

        # Let pipe connection_lost callbacks queued by EOF run while the event
        # loop is still alive.
        await asyncio.sleep(0)

        self._permission_sessions.clear()
        self._seen_permissions.clear()


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None,
) -> AsyncIterator[Message]:
    """Run a one-shot OpenCode query."""
    client = OpenCodeAgent(options=options or AgentOptions())
    try:
        await client.connect(prompt=prompt)
        async for message in client.receive_messages():
            yield message
            if getattr(message, "type", None) == MessageType.STOP.value:
                break
    finally:
        await client.disconnect()


class _SyncOpenCodeServer:
    """Small synchronous server owner used by the session picker helpers."""

    def __init__(
        self,
        cwd: Optional[str],
        extra_env: Optional[Dict[str, str]],
        cli_path: Optional[str] = None,
    ):
        self.cwd = cwd or os.getcwd()
        self.extra_env = extra_env or {}
        self.cli_path = cli_path
        self.url = ""
        self.process: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None

    def __enter__(self):
        cli_command = self.cli_path or "opencode"
        cli = shutil.which(cli_command)
        if not cli and not self.cli_path:
            cli = find_opencode_cli()
        if not cli:
            raise FileNotFoundError(f"OpenCode CLI not found: {cli_command}")

        env = os.environ.copy()
        env.update(self.extra_env)
        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True

        self.process = subprocess.Popen(
            [cli, "serve", "--hostname=127.0.0.1", "--port=0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=self.cwd,
            env=env,
            **kwargs,
        )
        lines: thread_queue.Queue = thread_queue.Queue()

        def read_output():
            if not self.process or not self.process.stdout:
                return
            for line in self.process.stdout:
                lines.put(line)

        self._reader = threading.Thread(target=read_output, daemon=True)
        self._reader.start()

        deadline = time.monotonic() + 10.0
        output = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None and lines.empty():
                break
            try:
                line = lines.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except thread_queue.Empty:
                continue
            output.append(line)
            match = _SERVER_URL_RE.search(line)
            if match:
                self.url = match.group(1).rstrip("/")
                return self

        self.close()
        raise RuntimeError(
            "Timed out starting OpenCode server"
            + (": " + "".join(output[-20:]).strip() if output else "")
        )

    def close(self):
        if self.process and self.process.poll() is None:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2.0)
        self.process = None

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _sync_auth_headers(extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    source = {**os.environ, **(extra_env or {})}
    password = source.get("OPENCODE_SERVER_PASSWORD")
    if not password:
        return {}
    username = source.get("OPENCODE_SERVER_USERNAME", "opencode")
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _sync_request(
    base_url: str,
    path: str,
    cwd: Optional[str],
    extra_env: Optional[Dict[str, str]],
) -> Any:
    query_string = "?" + urlencode({"directory": cwd}) if cwd else ""
    url = base_url + path + query_string
    request = Request(
        url,
        headers={"Accept": "application/json", **_sync_auth_headers(extra_env)},
        method="GET",
    )
    with urlopen(request, timeout=15.0) as response:
        raw = response.read()
    value = json.loads(raw.decode("utf-8")) if raw else None
    return OpenCodeAgent._unwrap(value)


def list_opencode_sessions(
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cli_path: Optional[str] = None,
) -> list:
    """Return OpenCode sessions for a workspace, newest first."""
    try:
        with _SyncOpenCodeServer(cwd, extra_env, cli_path) as server:
            sessions = _sync_request(server.url, "/session", cwd, extra_env)
            result = []
            for session in sessions if isinstance(sessions, list) else []:
                if not isinstance(session, dict) or not session.get("id"):
                    continue
                updated = (session.get("time") or {}).get("updated") or 0
                # OpenCode timestamps are milliseconds; tolerate seconds from
                # older versions and test servers.
                mtime = float(updated)
                if mtime > 100000000000:
                    mtime /= 1000.0
                result.append({
                    "session_id": session["id"],
                    "summary": session.get("title") or session["id"][:8],
                    "mtime": mtime,
                })
            return sorted(result, key=lambda item: item["mtime"], reverse=True)
    except Exception as error:
        LOG.warning("list_opencode_sessions failed: %s", error)
        return []


def get_opencode_session_info(
    session_id: str,
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cli_path: Optional[str] = None,
) -> Optional[dict]:
    """Return unified metadata and the last turn for an OpenCode session."""
    try:
        with _SyncOpenCodeServer(cwd, extra_env, cli_path) as server:
            sid = quote(session_id, safe="")
            session = _sync_request(server.url, f"/session/{sid}", cwd, extra_env)
            messages = _sync_request(
                server.url, f"/session/{sid}/message", cwd, extra_env
            )
            if not isinstance(session, dict):
                return None

            prompt = None
            response = None
            entries = messages if isinstance(messages, list) else []
            for entry in reversed(entries):
                if not isinstance(entry, dict):
                    continue
                info = entry.get("info") or {}
                text = "".join(
                    part.get("text", "")
                    for part in entry.get("parts", [])
                    if isinstance(part, dict) and part.get("type") == "text"
                    and not part.get("ignored")
                ).strip()
                if info.get("role") == "assistant" and response is None and text:
                    response = text
                elif info.get("role") == "user" and text:
                    prompt = text
                    break

            updated = (session.get("time") or {}).get("updated") or 0
            mtime = float(updated)
            if mtime > 100000000000:
                mtime /= 1000.0
            return {
                "summary": session.get("title"),
                "mtime": mtime,
                "prompt": prompt,
                "response": response,
            }
    except Exception as error:
        LOG.warning("get_opencode_session_info failed: %s", error)
        return None
