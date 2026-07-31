"""
Gemini CLI agent (google-gemini/gemini-cli) via the spawn-per-turn adapter.

No --input-format flag exists for gemini; each turn is a fresh
`gemini -p "<prompt>" -o stream-json`, resumed next turn via
`-r/--resume <session_id>`. See genfoundry/spawn_per_turn_agent.py for why
this needs a different BaseAgent shape than Claude Code / Codex / Kimi.

Event schema confirmed via a real, successful live capture (not
billing-blocked like Grok/Qwen were) -- flat shape, distinct from Claude/
Qwen's nested envelope:
  {"type":"init","session_id":"...","model":"auto"}
  {"type":"message","role":"user","content":"..."}
  {"type":"tool_use","tool_name":"read_file","tool_id":"...","parameters":{...}}
  {"type":"tool_result","tool_id":"...","status":"error"|"success","output":"..."}
  {"type":"message","role":"assistant","content":"...","delta":true}
  {"type":"result","status":"success","stats":{...}}
See genfoundry/tests/test_gemini_msg.py for the exact captured fixture.

--skip-trust is passed by default (not a permission loosener -- it bypasses
an interactive-only "trust this workspace?" prompt that headless runs can
never answer, and without it gemini refuses to start at all in a directory
it hasn't seen before). No --yolo/tool-allowlist flag is set by default;
see GrokAgent's docstring for why guessing tool names is worse than not
setting the flag. Set GEMINI_EXTRA_ARGS to add -y/--yolo once you've
decided you want it.
"""

import os
import sys
import logging
from typing import Optional, Dict, Any, List

from .base_agent import Message, MessageType, TextBlock, AssistantMessage
from .spawn_per_turn_agent import SpawnPerTurnAgent

LOG = logging.getLogger("TermMate")


def find_gemini_cli() -> Optional[str]:
    """Search common default install locations for the gemini CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "gemini.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "gemini"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found gemini CLI at default location: {path_str}")
            return path_str
    return None


class GeminiAgent(SpawnPerTurnAgent):
    """Client for Gemini CLI via spawn-per-turn stream-json."""

    def _default_cli_name(self) -> str:
        return "gemini"

    def _find_cli(self) -> Optional[str]:
        return find_gemini_cli()

    def _build_argv(self, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        argv = [self.cli_path, "-p", prompt, "-o", "stream-json", "--skip-trust"]
        if resume_session_id:
            argv.extend(["-r", resume_session_id])
        if self.options.model:
            argv.extend(["-m", self.options.model])
        extra_args = (self.options.extra_env or {}).get("GEMINI_EXTRA_ARGS")
        if extra_args:
            argv.extend(extra_args.split())
        return argv

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        event_type = data.get("type")

        if event_type == "init":
            session_id = data.get("session_id")
            if session_id:
                self._session_id = session_id
            return []

        if event_type == "message":
            if data.get("role") != "assistant":
                # role == "user" is just an echo of our own input.
                return []
            text = data.get("content", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(text)])]

        if event_type == "tool_use":
            block = {
                "type": "tool_use",
                "id": data.get("tool_id"),
                "name": data.get("tool_name"),
                "input": data.get("parameters", {}),
            }
            return [AssistantMessage(content=[block])]

        if event_type == "tool_result":
            # No dedicated bare-tool-result renderer in ClaudeMessageProcessor
            # (same choice as KimiAgent's role=="tool" case) -- the tool_use
            # header above is what's user-visible; skip rather than invent
            # new UI-processor code for one field.
            LOG.debug(f"gemini tool_result (not rendered): {data.get('status')}")
            return []

        if event_type == "result":
            return [Message("result", content=data.get("status"))]

        LOG.debug(f"gemini event not yet mapped to UI: {event_type}")
        return []
