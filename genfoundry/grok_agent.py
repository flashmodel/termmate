"""
Grok Build agent (xai-org/grok-build) via the spawn-per-turn adapter.

Grok Build has no persistent bidirectional stdin protocol -- each turn is a
fresh `grok -p "<prompt>" --output-format streaming-json` invocation, resumed
on the next turn with `-r/--resume <session_id>`. See genfoundry/
spawn_per_turn_agent.py for why this needs a different BaseAgent shape than
Claude Code / Codex.

Event schema (confirmed via https://deepwiki.com/xai-org/grok-build, not
guessed): `text`, `thought`, `end` (session_id, usage, stopReason), `error`,
plus assorted lifecycle events (max_turns_reached, auto_compact_*, ...) that
are not yet surfaced in chatview and are safely ignored here.

Tool-call events are NOT in the confirmed schema (the Deepwiki query that
sourced this file didn't surface a tool_use/tool_result event name), so
per-tool rendering is not implemented -- text/thought/result/error only.
Extend _parse_event once a real transcript with tool calls is available.

No --tools/--yolo/--permission-mode flags are passed by default: guessing
Grok's internal tool names wrong could silently disable tool use or (worse)
silently grant more than intended. Headless runs get whatever Grok's own
default permission behavior is; set `grok_extra_args` in TermMate settings
to add `--yolo` or `--allow`/`--deny <RULE>` once you know what you want.
"""

import os
import sys
import logging
from typing import Optional, Dict, Any, List

from .base_agent import Message, MessageType, TextBlock, AssistantMessage
from .spawn_per_turn_agent import SpawnPerTurnAgent

LOG = logging.getLogger("TermMate")


def find_grok_cli() -> Optional[str]:
    """Search common default install locations for the grok CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "grok.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "grok"),
            os.path.join(home, ".grok", "bin", "grok"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found grok CLI at default location: {path_str}")
            return path_str
    return None


class GrokAgent(SpawnPerTurnAgent):
    """Client for Grok Build via spawn-per-turn streaming-json."""

    def _default_cli_name(self) -> str:
        return "grok"

    def _find_cli(self) -> Optional[str]:
        return find_grok_cli()

    def _build_argv(self, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        argv = [self.cli_path, "-p", prompt, "--output-format", "streaming-json"]
        if resume_session_id:
            argv.extend(["-r", resume_session_id])
        if self.options.model:
            argv.extend(["-m", self.options.model])
        if self.options.cwd:
            argv.extend(["--cwd", str(self.options.cwd)])
        if self.options.system_prompt:
            argv.extend(["--rules", self.options.system_prompt])
        extra_args = (self.options.extra_env or {}).get("GROK_EXTRA_ARGS")
        if extra_args:
            argv.extend(extra_args.split())
        return argv

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        event_type = data.get("type")

        if event_type == "text":
            text = data.get("data", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(text)])]

        if event_type == "thought":
            text = data.get("data", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(f"\U0001f4ad {text}")])]

        if event_type == "end":
            session_id = data.get("sessionId")
            if session_id:
                self._session_id = session_id
            return [Message("result", content=data.get("stopReason"), msg_id=data.get("requestId"))]

        if event_type == "error":
            return [Message("error", content=data.get("message", "Grok error"))]

        # Unrecognized/unhandled lifecycle event (max_turns_reached, auto_compact_*,
        # image_compressed, ...) -- log and skip rather than guess a rendering.
        LOG.debug(f"grok event not yet mapped to UI: {event_type}")
        return []
