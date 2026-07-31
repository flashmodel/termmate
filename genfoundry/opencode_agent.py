"""
OpenCode agent (anomalyco/opencode) via the spawn-per-turn adapter.

Each turn is `opencode run "<prompt>" --format json`, resumed next turn via
`-s/--session <session_id>` (or `-c/--continue` for the most recent one --
not used here since we track the ID ourselves). See genfoundry/
spawn_per_turn_agent.py for why this needs a different BaseAgent shape than
Claude Code / Codex / Kimi.

opencode.ai/docs itself doesn't publish the JSON event schema ("raw JSON
events", no example lines). The literal shapes below are sourced from
OpenCode's own test suite (packages/opencode/test/run-process.test.ts, via
Deepwiki) -- real assertions from their own tests, not fabricated, but also
not a live capture the way Grok/Qwen/Gemini's fixtures are:
  {"type":"text","timestamp":...,"sessionID":"...",
   "part":{"type":"text","text":"..."}}
  {"type":"tool_use","timestamp":...,"sessionID":"...",
   "part":{"type":"tool","tool":"bash",
           "state":{"status":"completed","input":{...},"output":"..."}}}
  {"type":"error","timestamp":...,"sessionID":"...",
   "error":{"message":"...","code":"..."}}
  {"type":"step_finish","timestamp":...,"sessionID":"...",
   "part":{"type":"step-finish","reason":"..."}}

Permission control is config-file-based (opencode.json's "permission" key --
confirmed tool names: read, edit, glob, grep, bash, task, skill, lsp,
question, webfetch, websearch, external_directory, doom_loop), not a CLI
flag, except --auto ("auto-approve permissions that are not explicitly
denied" -- dangerous, matches the other providers' --yolo). --auto is not
passed by default; without it and without a config file granting explicit
allows, opencode's default "ask" permission mode has no headless answer
channel here, so tool calls will likely block. Set OPENCODE_EXTRA_ARGS
(e.g. "--auto") once you've decided that tradeoff, or configure
opencode.json's permission rules directly.
"""

import os
import sys
import logging
from typing import Optional, Dict, Any, List

from .base_agent import Message, MessageType, TextBlock, AssistantMessage
from .spawn_per_turn_agent import SpawnPerTurnAgent

LOG = logging.getLogger("TermMate")


def find_opencode_cli() -> Optional[str]:
    """Search common default install locations for the opencode CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "opencode.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "opencode"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found opencode CLI at default location: {path_str}")
            return path_str
    return None


class OpenCodeAgent(SpawnPerTurnAgent):
    """Client for OpenCode via spawn-per-turn `run --format json`."""

    def _default_cli_name(self) -> str:
        return "opencode"

    def _find_cli(self) -> Optional[str]:
        return find_opencode_cli()

    def _build_argv(self, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        argv = [self.cli_path, "run", prompt, "--format", "json"]
        if resume_session_id:
            argv.extend(["--session", resume_session_id])
        if self.options.model:
            argv.extend(["--model", self.options.model])
        if self.options.cwd:
            argv.extend(["--dir", str(self.options.cwd)])
        extra_args = (self.options.extra_env or {}).get("OPENCODE_EXTRA_ARGS")
        if extra_args:
            argv.extend(extra_args.split())
        return argv

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        session_id = data.get("sessionID")
        if session_id:
            self._session_id = session_id

        event_type = data.get("type")
        part = data.get("part", {}) or {}

        if event_type == "text":
            text = part.get("text", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(text)])]

        if event_type == "tool_use":
            state = part.get("state", {}) or {}
            block = {
                "type": "tool_use",
                "id": part.get("callID"),
                "name": part.get("tool"),
                "input": state.get("input", {}),
            }
            return [AssistantMessage(content=[block])]

        if event_type == "error":
            error = data.get("error", {}) or {}
            return [Message("error", content=error.get("message", "OpenCode error"))]

        if event_type == "step_finish":
            return [Message("result", content=part.get("reason"))]

        # step_start and anything else: no UI mapping yet.
        LOG.debug(f"opencode event not yet mapped to UI: {event_type}")
        return []
