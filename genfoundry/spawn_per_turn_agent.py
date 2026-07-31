"""
Shared base for "spawn-per-turn" CLI agents.

Some agent CLIs (Grok Build, Gemini CLI, Qwen Code, OpenCode, ...) have no
persistent bidirectional stdin protocol the way Claude Code / Codex / Kimi do.
Each turn is a fresh process: `<cli> -p "<prompt>" --output-format <fmt>`,
optionally resumed on the next turn via a `--resume <session_id>`-style flag.
The process streams NDJSON to stdout and exits when the turn is done.

This class adapts that shape to the BaseAgent contract (connect / send_message
/ receive_messages / steer / interrupt / disconnect) so chatview.py can drive
these providers the same way it drives the persistent ones: it maintains a
single internal message queue that each turn's subprocess drains into, and
receive_messages() yields from that queue across turns/process boundaries
without ever looking like the "connection" dropped.

Subclasses implement:
  - _find_cli() -> Optional[str]           locate the CLI executable
  - _build_argv(prompt, resume_session_id) -> List[str]   this turn's argv
  - _parse_event(data: dict) -> List[Message]              translate one JSON
        event from this provider's own schema into 0+ generic Message objects

Not supported here (no interactive backstop once a turn starts, since there's
no persistent stdin to send a mid-turn response on):
  - live interrupt of a specific tool call (interrupt() kills the whole
    subprocess -- the coarsest possible interrupt)
  - send_permission_response() -- these providers must be spawned with their
    own CLI-level policy (tool allowlist / auto-approve flag) set in advance
"""

import asyncio
import logging
import os
import sys
import json
from typing import Optional, Dict, Any, AsyncIterator, List

from .base_agent import BaseAgent, Message, MessageType, AgentOptions

LOG = logging.getLogger("TermMate")


class SpawnPerTurnAgent(BaseAgent):
    """BaseAgent implementation for CLIs with no persistent bidirectional stdin."""

    def __init__(self, options: Optional[AgentOptions] = None):
        self.options = options or AgentOptions()
        self.is_connected = False
        self.process: Optional[asyncio.subprocess.Process] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._session_id: Optional[str] = None
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None

        cli_command = self.options.cli_path or self._default_cli_name()
        self.cli_path = cli_command
        if os.path.isabs(cli_command) or os.sep in cli_command:
            found = cli_command if os.path.isfile(cli_command) else None
        else:
            import shutil
            found = shutil.which(cli_command) or self._find_cli()
        if not found:
            raise FileNotFoundError(
                f"{self._default_cli_name()} CLI not found. Install it first."
            )
        self.cli_path = found

    # ---- subclass hooks ----------------------------------------------

    def _default_cli_name(self) -> str:
        raise NotImplementedError

    def _find_cli(self) -> Optional[str]:
        """Search common default install locations. Override to add more."""
        return None

    def _build_argv(self, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        raise NotImplementedError

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        raise NotImplementedError

    # ---- BaseAgent interface -------------------------------------------

    async def connect(self, prompt: Optional[str] = None) -> None:
        if self.is_connected:
            raise RuntimeError("Client is already connected")
        self.is_connected = True
        if self.options.session_id:
            self._session_id = self.options.session_id
        if prompt:
            await self.send_message(prompt)

    async def send_message(
        self, content: str, parent_tool_use_id: Optional[str] = None, proceed_plan: bool = False
    ) -> None:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        if self.process and self.process.returncode is None:
            raise RuntimeError(
                f"{self._default_cli_name()} turn already in progress; "
                "wait for it to finish or interrupt() before sending another message."
            )

        argv = self._build_argv(content, self._session_id)

        env = os.environ.copy()
        if self.options.extra_env:
            env.update(self.options.extra_env)

        kwargs = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.options.cwd,
            **kwargs,
        )
        self._read_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """No persistent stdin to steer mid-turn; treat as the next turn."""
        await self.send_message(text)

    async def interrupt(self) -> None:
        """Coarsest possible interrupt: kill the in-flight turn's subprocess."""
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            await self._message_queue.put(Message(MessageType.STOP.value, content="interrupted"))

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

    # ---- internals -------------------------------------------------------

    async def _read_messages(self) -> None:
        if not self.process or not self.process.stdout:
            return
        buffer = b""
        try:
            while True:
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
                        LOG.error(f"{self._default_cli_name()} non-json msg: {line[:200]}...")
                        continue
                    LOG.debug(f"{self._default_cli_name()} msg: {data}")
                    try:
                        for message in self._parse_event(data):
                            await self._message_queue.put(message)
                    except Exception as e:
                        LOG.error(f"{self._default_cli_name()} event parse error: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"{self._default_cli_name()} reading messages error: {e}")
            await self._message_queue.put(Message("error", content=str(e)))
        finally:
            await self._message_queue.put(Message(MessageType.STOP.value, content="turn_complete"))

    async def _read_stderr(self) -> None:
        if not self.process or not self.process.stderr:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    LOG.error(f"{self._default_cli_name()} stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
