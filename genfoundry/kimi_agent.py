"""
Kimi CLI agent (moonshotai/kimi-cli) -- persistent, bidirectional.

Unlike Grok/Qwen/Gemini, Kimi keeps one long-lived process alive for the
whole session: `kimi --print --input-format=stream-json
--output-format=stream-json`, fed one JSON line per turn on stdin, reading
JSON lines back on stdout until the stream is closed. This is architecturally
closer to Claude Code / Codex than to the spawn-per-turn family (see
genfoundry/spawn_per_turn_agent.py), so this class implements BaseAgent
directly rather than going through that adapter.

Wire protocol (confirmed via https://github.com/moonshotai/kimi-cli/blob/
main/docs/en/customization/print-mode.md -- plain OpenAI-chat-message shape,
NOT Claude Code's {type, message: {...}} envelope):

  in:  {"role": "user", "content": "..."}
  out: {"role": "assistant", "content": "..."}
       {"role": "assistant", "content": "...", "tool_calls": [
           {"type": "function", "id": "tc_1",
            "function": {"name": "Shell", "arguments": "{\"command\":\"ls\"}"}}
       ]}
       {"role": "tool", "tool_call_id": "tc_1", "content": "..."}

There is no documented "turn complete" event: a turn ends when an assistant
message arrives with no tool_calls (the CLI runs tool_calls -> tool results
-> another assistant message automatically; docs only show that loop ending
on a plain text reply). That's used here as the completion heuristic.

Messages are shaped to match what ClaudeMessageProcessor already renders
(AssistantMessage with TextBlock / tool_use dict content, plus a trailing
"result" message to flush/stop the loading indicator) so no new UI processor
class is needed -- Kimi falls through to the existing Claude renderer in
chatview.py, the same choice made for Grok.

No --model/--yolo flags are passed by default: the only flags confirmed for
plain "kimi" (not the separately-documented "kimi-agent" wire-mode binary,
which may have a different flag surface) are --print, --input-format,
--output-format, and --work-dir. Set KIMI_EXTRA_ARGS in TermMate's env
setting to add more once you've verified them against your installed
version, rather than risk this file guessing wrong.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Optional, Dict, Any, AsyncIterator, List

from .base_agent import BaseAgent, AgentOptions, Message, MessageType, TextBlock, AssistantMessage

LOG = logging.getLogger("TermMate")


def find_kimi_cli() -> Optional[str]:
    """Search common default install locations for the kimi CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "kimi.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "kimi"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found kimi CLI at default location: {path_str}")
            return path_str
    return None


class KimiAgent(BaseAgent):
    """Client for Kimi CLI's persistent stream-json print mode."""

    def __init__(self, options: Optional[AgentOptions] = None):
        self.options = options or AgentOptions()
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_connected = False
        self._read_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue = asyncio.Queue()

        import shutil
        cli_command = self.options.cli_path or "kimi"
        self.cli_path = shutil.which(cli_command) or find_kimi_cli() or cli_command
        if not self.cli_path:
            raise FileNotFoundError("Kimi CLI not found. Please install it first.")

    async def connect(self, prompt: Optional[str] = None) -> None:
        if self.is_connected:
            raise RuntimeError("Client is already connected")

        cmd = [
            self.cli_path,
            "--print",
            "--input-format=stream-json",
            "--output-format=stream-json",
        ]
        if self.options.cwd:
            cmd.extend(["--work-dir", str(self.options.cwd)])
        extra_args = (self.options.extra_env or {}).get("KIMI_EXTRA_ARGS")
        if extra_args:
            cmd.extend(extra_args.split())

        env = os.environ.copy()
        if self.options.extra_env:
            env.update(self.options.extra_env)

        kwargs = {}
        if sys.platform == "win32":
            import subprocess
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self.process = await asyncio.create_subprocess_exec(
            *cmd,
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

        if prompt:
            await self.send_message(prompt)

    async def send_message(
        self, content: str, parent_tool_use_id: Optional[str] = None, proceed_plan: bool = False
    ) -> None:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        await self._write_json({"role": "user", "content": content})

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """No distinct steering wire message is documented; send as a normal turn."""
        await self.send_message(text)

    async def interrupt(self) -> None:
        """No documented mid-turn cancel message. Coarsest possible interrupt:
        terminate the process. Unlike the spawn-per-turn providers this also
        ends the session (Kimi's state lives in this one long-running process,
        not a resumable session ID), so the caller must reconnect."""
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.is_connected = False
        await self._message_queue.put(Message(MessageType.STOP.value, content="interrupted"))

    async def receive_messages(self) -> AsyncIterator[Message]:
        if not self.is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        while self.is_connected:
            try:
                message = await asyncio.wait_for(self._message_queue.get(), timeout=0.1)
                yield message
            except asyncio.TimeoutError:
                if self.process and self.process.returncode is not None:
                    break
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

    # ---- internals ---------------------------------------------------

    async def _write_json(self, data: Dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            return
        line = json.dumps(data) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        role = data.get("role")

        if role == "assistant":
            blocks: List[Any] = []
            content = data.get("content")
            if content:
                blocks.append(TextBlock(content))

            tool_calls = data.get("tool_calls") or []
            for call in tool_calls:
                func = call.get("function", {})
                try:
                    tool_input = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_input = {"_raw_arguments": func.get("arguments", "")}
                blocks.append({
                    "type": "tool_use",
                    "id": call.get("id"),
                    "name": func.get("name"),
                    "input": tool_input,
                })

            messages: List[Message] = [AssistantMessage(content=blocks)]
            if not tool_calls:
                # No further tool_calls queued -- this is the turn's final message.
                messages.append(Message("result", content=None))
            return messages

        if role == "tool":
            # Tool results are echoed back by Kimi for our own visibility;
            # ClaudeMessageProcessor doesn't have a dedicated renderer for a
            # bare tool-result line, so surface it as a system-ish message
            # rather than invent new UI-processor code for one field.
            content = data.get("content", "")
            return [Message("assistant_tool_result", content=content, msg_id=data.get("tool_call_id"))]

        LOG.debug(f"kimi event with unrecognized role: {role}")
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
                        LOG.error(f"kimi non-json msg: {line[:200]}...")
                        continue
                    LOG.debug(f"kimi msg: {data}")
                    try:
                        for message in self._parse_event(data):
                            await self._message_queue.put(message)
                    except Exception as e:
                        LOG.error(f"kimi event parse error: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.error(f"kimi reading messages error: {e}")
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
                    LOG.error(f"kimi stderr: {line_str}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
