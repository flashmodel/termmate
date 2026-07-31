"""
Generic Agent Client Protocol (ACP) client -- https://agentclientprotocol.com,
a standardized JSON-RPC-over-stdio protocol between editors and coding
agents (Zed-led, analogous to LSP for language servers). Any ACP-compliant
CLI can be driven through this class, not just one vendor's bespoke format --
see JunieAgent below for the first concrete user.

Transport pattern (id-correlated requests via futures, fire-and-forget
notifications, dispatch on incoming id/method combination) is modeled on
codex_agent.py's JSON-RPC plumbing, which already solves this problem well
for this codebase -- not reinvented here.

Confirmed via Context7 docs (agentclientprotocol/agent-client-protocol):
  Client -> Agent: initialize {protocolVersion, capabilities, info}
  Client -> Agent: session/new {cwd, additionalDirectories, mcpServers}
                    -> {sessionId, modes, configOptions}
  Client -> Agent: session/prompt {sessionId, prompt: ContentBlock[]}
                    -> {stopReason}          # this response IS turn completion
  Agent  -> Client: session/update notifications (method, no id) during the
                    turn -- sessionUpdate discriminator: agent_message_chunk,
                    agent_thought_chunk, tool_call, tool_call_update, plan, ...
  Agent  -> Client: session/request_permission (method + id, needs a reply) --
                    {sessionId, toolCall, options: [{optionId, kind}, ...]}
                    kind in allow_once/allow_always/reject_once/reject_always
  Client -> Agent: session/cancel notification (interrupt) -- MUST then
                    answer any still-pending request_permission with the
                    "cancelled" outcome

Permission requests are wired into the SAME control_request/can_use_tool
shape claude_agent.py already produces, so chatview's existing permission
phantom UI renders them with no new UI code -- this is the one provider in
this whole batch that gets a real interactive approve/deny, because ACP is
the one protocol that actually has a bidirectional channel for it.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Optional, Dict, Any, AsyncIterator, List

from .base_agent import BaseAgent, AgentOptions, Message, MessageType, TextBlock, AssistantMessage

LOG = logging.getLogger("TermMate")


class ACPAgent(BaseAgent):
    """Generic ACP client. Subclasses set the CLI name/launch args."""

    def __init__(self, options: Optional[AgentOptions] = None):
        self.options = options or AgentOptions()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._session_id: Optional[str] = None
        self._rpc_id = 0
        self._pending_responses: Dict[int, asyncio.Future] = {}
        self._pending_permission_options: Dict[Any, List[Dict[str, Any]]] = {}
        self._turn_in_flight = False

        import shutil
        cli_command = self.options.cli_path or self._default_cli_name()
        self.cli_path = shutil.which(cli_command) or self._find_cli() or cli_command
        if not self.cli_path:
            raise FileNotFoundError(f"{self._default_cli_name()} CLI not found. Please install it first.")

    # ---- subclass hooks ----------------------------------------------

    def _default_cli_name(self) -> str:
        raise NotImplementedError

    def _find_cli(self) -> Optional[str]:
        return None

    def _extra_launch_args(self) -> List[str]:
        """Flags needed to put the CLI into ACP mode (e.g. Junie's --acp=true)."""
        return []

    # ---- BaseAgent interface -------------------------------------------

    async def connect(self, prompt: Optional[str] = None) -> None:
        if self.is_connected:
            raise RuntimeError("Client is already connected")

        argv = [self.cli_path] + self._extra_launch_args()
        env = os.environ.copy()
        if self.options.extra_env:
            env.update(self.options.extra_env)

        kwargs = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.options.cwd,
            **kwargs,
        )
        self.is_connected = True
        self._read_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._read_stderr())

        await self._rpc_request("initialize", {
            "protocolVersion": 1,
            "capabilities": {},
            "info": {"name": "TermMate", "version": "1.0"},
        })
        session_result = await self._rpc_request("session/new", {
            "cwd": str(self.options.cwd) if self.options.cwd else os.getcwd(),
            "additionalDirectories": [str(d) for d in (self.options.add_dirs or [])],
            "mcpServers": [],
        })
        self._session_id = (session_result or {}).get("sessionId")

        if prompt:
            await self.send_message(prompt)

    async def send_message(
        self, content: str, parent_tool_use_id: Optional[str] = None, proceed_plan: bool = False
    ) -> None:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        if self._turn_in_flight:
            raise RuntimeError("A prompt turn is already in progress; interrupt() before sending another.")
        self._turn_in_flight = True
        asyncio.create_task(self._run_prompt_turn([{"type": "text", "text": content}]))

    async def _run_prompt_turn(self, prompt_blocks: List[Dict[str, Any]]) -> None:
        try:
            result = await self._rpc_request(
                "session/prompt",
                {"sessionId": self._session_id, "prompt": prompt_blocks},
                timeout=600.0,
            )
            stop_reason = (result or {}).get("stopReason")
            await self._message_queue.put(Message("result", content=stop_reason))
        except Exception as e:
            await self._message_queue.put(Message("error", content=str(e)))
        finally:
            self._turn_in_flight = False

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """No distinct ACP steering method; sends a new prompt turn."""
        await self.send_message(text)

    async def interrupt(self) -> None:
        if self._session_id:
            await self._rpc_notify("session/cancel", {"sessionId": self._session_id})
        # Per spec: a client sending session/cancel MUST answer any pending
        # session/request_permission with the "cancelled" outcome.
        for request_id in list(self._pending_permission_options.keys()):
            self._pending_permission_options.pop(request_id, None)
            await self._write_json({"id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
        await self._message_queue.put(Message(MessageType.STOP.value, content="interrupted"))

    async def send_permission_response(
        self,
        request_id: str,
        response_data: Dict[str, Any],
        is_extension_ui: bool = False,
    ) -> None:
        if is_extension_ui:
            # termchat extension's own UI protocol, not native ACP permission
            # flow -- shared across all providers, same as the other agents.
            self._pending_permission_options.pop(request_id, None)
            await self._write_extension_ui_response(request_id, response_data)
            return

        options = self._pending_permission_options.pop(request_id, [])
        behavior = response_data.get("behavior", "deny")
        wanted_kind = "allow_once" if behavior == "allow" else "reject_once"
        chosen = next((o for o in options if o.get("kind") == wanted_kind), None)
        if chosen is None:
            # Fall back to any allow/reject option if the exact "once" kind isn't offered.
            fallback_kind = "allow_always" if behavior == "allow" else "reject_always"
            chosen = next((o for o in options if o.get("kind") == fallback_kind), None)
        if chosen is None and options:
            chosen = options[0]

        if chosen is None:
            await self._write_json({"id": request_id, "result": {"outcome": {"outcome": "cancelled"}}})
            return
        await self._write_json({
            "id": request_id,
            "result": {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}},
        })

    async def receive_messages(self) -> AsyncIterator[Message]:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        while self.is_connected:
            try:
                message = await asyncio.wait_for(self._message_queue.get(), timeout=0.1)
                yield message
            except asyncio.TimeoutError:
                continue

    async def disconnect(self) -> None:
        if not self.is_connected:
            return
        self.is_connected = False
        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()
        for task in (self._read_task, self._stderr_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    # ---- JSON-RPC transport --------------------------------------------

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    async def _write_json(self, data: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            return
        raw = json.dumps(data) + "\n"
        self.process.stdin.write(raw.encode("utf-8"))
        await self.process.stdin.drain()

    async def _rpc_request(self, method: str, params: Dict[str, Any], timeout: float = 30.0) -> Any:
        rid = self._next_id()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[rid] = future
        await self._write_json({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_responses.pop(rid, None)
            LOG.error(f"ACP RPC timeout for {method} (id={rid})")
            return None

    async def _rpc_notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._write_json(msg)

    async def _dispatch(self, data: Dict[str, Any]) -> None:
        # Response to one of our requests.
        if "id" in data and "method" not in data:
            rid = data["id"]
            future = self._pending_responses.pop(rid, None)
            if future and not future.done():
                if "error" in data:
                    LOG.error(f"ACP RPC error [{rid}]: {data['error']}")
                    future.set_result(None)
                else:
                    future.set_result(data.get("result"))
            return

        method = data.get("method", "")
        params = data.get("params", {}) or {}

        # Request FROM the agent that needs a reply.
        if "id" in data and method:
            if method == "session/request_permission":
                await self._handle_permission_request(data["id"], params)
            else:
                await self._write_json({
                    "id": data["id"],
                    "error": {"code": -32601, "message": f"Method not implemented: {method}"},
                })
            return

        # Notification from the agent.
        if method == "session/update":
            for message in self._parse_session_update(params):
                await self._message_queue.put(message)
        else:
            LOG.debug(f"acp notification not yet mapped to UI: {method}")

    async def _handle_permission_request(self, request_id: Any, params: Dict[str, Any]) -> None:
        options = params.get("options", [])
        self._pending_permission_options[request_id] = options
        tool_call = params.get("toolCall", {}) or {}
        tool_name = tool_call.get("title") or tool_call.get("toolCallId") or "tool"
        await self._message_queue.put(Message(
            "control_request",
            content={
                "request": {"subtype": "can_use_tool", "tool_name": tool_name, "input": tool_call},
                "request_id": request_id,
            },
            msg_id=request_id,
        ))

    def _parse_session_update(self, params: Dict[str, Any]) -> List[Message]:
        kind = params.get("sessionUpdate")

        if kind == "agent_message_chunk":
            text = (params.get("content") or {}).get("text", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(text)])]

        if kind == "agent_thought_chunk":
            text = (params.get("content") or {}).get("text", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(f"\U0001f4ad {text}")])]

        if kind == "tool_call":
            block = {
                "type": "tool_use",
                "id": params.get("toolCallId"),
                "name": params.get("title") or params.get("kind"),
                "input": {},
            }
            return [AssistantMessage(content=[block])]

        # tool_call_update/plan/available_commands_update/current_mode_update/
        # config_option_update/session_info_update/usage_update: no UI mapping yet.
        LOG.debug(f"acp session/update kind not yet mapped to UI: {kind}")
        return []

    async def _read_messages(self) -> None:
        if not self.process or not self.process.stdout:
            return
        buffer = b""
        try:
            while self.is_connected:
                chunk = await self.process.stdout.read(65536)
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
                    except json.JSONDecodeError:
                        LOG.error(f"acp non-json msg: {line[:200]}...")
                        continue
                    LOG.debug(f"acp msg: {data}")
                    try:
                        await self._dispatch(data)
                    except Exception as e:
                        LOG.error(f"acp dispatch error: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"acp reading messages error: {e}")
            await self._message_queue.put(Message("error", content=str(e)))

    async def _read_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            while self.is_connected:
                line = await self.process.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    LOG.error(f"acp stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
