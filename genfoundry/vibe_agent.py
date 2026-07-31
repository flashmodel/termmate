"""
Vibe Code agent (mistralai/mistral-vibe, Mistral's official CLI) via the
spawn-per-turn adapter.

Each turn is `vibe --prompt "<prompt>" --output streaming`, resumed next
turn via `--resume <session_id>`. See genfoundry/spawn_per_turn_agent.py
for why this needs a different BaseAgent shape than Claude Code / Codex /
Kimi. --resume/--continue require session logging enabled in Vibe's own
config -- if that's off, resume silently has nothing to attach to.

Event shape confirmed via Deepwiki against mistral-vibe's own test suite
(test_streaming_output_uses_public_history_entries and others -- real
test assertions, not fabricated): each line is a raw `PublicHistoryEntry`
object (no wrapper envelope), one of:
  {"type":"message","session_id":"...","role":"user"|"assistant"|"system",
   "content":[{"text":"..."}], ...}
  {"type":"reasoning","text":"...","summary":[...]}
  {"type":"effect","title":"...","detail":...,
   "state":{...}}                      # tool calls/results
  {"type":"callback","callback_id":"...","title":"...",
   "state":{...}}                      # approval requests -- see below
  {"type":"notice","level":"info"|"warning"|"error","message":"...","detail":...}

Vibe has a genuine approval-request mechanism (PublicCallbackEntry with
OpenCallbackState/AnsweredCallbackState -- confirmed via Deepwiki, "if the
client denies the callback, tool execution is skipped"), unlike most of
the other spawn-per-turn providers. But no CLI-level wire protocol for
*answering* a callback in --output streaming mode was found (the request/
response API is documented at the Python-library level, not the CLI's
stdin), so callbacks are observed but not answerable here -- same
no-live-approval limitation as the rest of this family. Use
--enabled-tools/--disabled-tools (glob/regex) or --auto-approve/--yolo via
VIBE_EXTRA_ARGS to set policy upfront instead.

No explicit turn-complete event exists in this schema (entries are only
emitted once individually completed, not as a stream with a final
marker). Following the same heuristic already used for KimiAgent in this
codebase: an assistant `message` entry is treated as the completion
signal. This is an approximation -- if effects/reasoning follow the final
assistant text in a given turn, the loading indicator may clear slightly
early (cosmetic only; no message is dropped).
"""

import os
import sys
import logging
from typing import Optional, Dict, Any, List

from .base_agent import Message, MessageType, TextBlock, AssistantMessage
from .spawn_per_turn_agent import SpawnPerTurnAgent

LOG = logging.getLogger("TermMate")


def find_vibe_cli() -> Optional[str]:
    """Search common default install locations for the vibe CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "vibe.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "vibe"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found vibe CLI at default location: {path_str}")
            return path_str
    return None


class VibeAgent(SpawnPerTurnAgent):
    """Client for Mistral's Vibe Code CLI via spawn-per-turn streaming output."""

    def _default_cli_name(self) -> str:
        return "vibe"

    def _find_cli(self) -> Optional[str]:
        return find_vibe_cli()

    def _build_argv(self, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        argv = [self.cli_path, "--prompt", prompt, "--output", "streaming"]
        if resume_session_id:
            argv.extend(["--resume", resume_session_id])
        extra_args = (self.options.extra_env or {}).get("VIBE_EXTRA_ARGS")
        if extra_args:
            argv.extend(extra_args.split())
        return argv

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        session_id = data.get("session_id")
        if session_id:
            self._session_id = session_id

        entry_type = data.get("type")

        if entry_type == "message":
            role = data.get("role")
            if role != "assistant":
                return []
            content_blocks = data.get("content") or []
            text = "".join(
                block.get("text", "") for block in content_blocks
                if isinstance(block, dict) and block.get("text")
            )
            if not text:
                return []
            # Heuristic completion signal -- see module docstring.
            return [AssistantMessage(content=[TextBlock(text)]), Message("result", content=None)]

        if entry_type == "reasoning":
            text = data.get("text", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(f"\U0001f4ad {text}")])]

        if entry_type == "effect":
            detail = data.get("detail")
            block = {
                "type": "tool_use",
                "name": data.get("title"),
                "input": detail if isinstance(detail, dict) else {"detail": detail},
            }
            return [AssistantMessage(content=[block])]

        if entry_type == "notice":
            if data.get("level") == "error":
                return [Message("error", content=data.get("message", "Vibe error"))]
            return []

        if entry_type == "callback":
            # No answerable wire protocol found for --output streaming; observe only.
            LOG.debug(f"vibe callback entry (not answerable here): {data.get('title')}")
            return []

        LOG.debug(f"vibe entry type not yet mapped to UI: {entry_type}")
        return []
