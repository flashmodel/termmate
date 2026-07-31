"""
jcode agent (1jehuang/jcode) via the spawn-per-turn adapter.

Each turn is `jcode --quiet --no-update --no-selfdev [--resume <name>] run
--ndjson "<prompt>"`, resumed next turn via `--resume <session/name>`. See
genfoundry/spawn_per_turn_agent.py for why this needs a different BaseAgent
shape than Claude Code / Codex / Kimi.

Event field shapes confirmed via Deepwiki against jcode's own
mock_gateway.py test fixture (real fixture data, not fabricated) --
docs/WRAPPERS.md only listed event type names, not their fields, so this
was checked before writing any parsing code:
  {"type":"text_delta","text":"..."}
  {"type":"tool_start","id":"...","name":"bash"}
  {"type":"tool_input","delta":"{\"command\":"}         # incremental JSON, not assembled here
  {"type":"tool_exec","id":"...","name":"bash"}          # tool actually running -- used as the "show tool_use block" trigger
  {"type":"tool_done","id":"...","name":"bash","output":"...","error":null}
  {"type":"tokens","input":120,"output":240}
  {"type":"error","session_id":"...","provider":"...","model":"...","message":"..."}
  {"type":"done","session_id":"...","provider":"...","model":"...","text":"...","usage":{...}}

tool_start/tool_input/tool_done/tokens are not rendered (no dedicated
bare-tool-progress renderer in ClaudeMessageProcessor -- consistent with
the same choice made for Gemini's tool_result and Kimi's role=="tool").

No --model is set by default; -m/--model is confirmed real but only worth
passing when options.model is set. No tool-allowlist/yolo-equivalent flag
is documented anywhere seen for jcode -- JCODE_EXTRA_ARGS is the escape
hatch once you know your build's flags.
"""

import os
import sys
import logging
from typing import Optional, Dict, Any, List

from .base_agent import Message, MessageType, TextBlock, AssistantMessage
from .spawn_per_turn_agent import SpawnPerTurnAgent

LOG = logging.getLogger("TermMate")


def find_jcode_cli() -> Optional[str]:
    """Search common default install locations for the jcode CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "jcode.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "jcode"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found jcode CLI at default location: {path_str}")
            return path_str
    return None


class JCodeAgent(SpawnPerTurnAgent):
    """Client for jcode via spawn-per-turn --ndjson."""

    def _default_cli_name(self) -> str:
        return "jcode"

    def _find_cli(self) -> Optional[str]:
        return find_jcode_cli()

    def _build_argv(self, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        argv = [self.cli_path, "--quiet", "--no-update", "--no-selfdev"]
        if resume_session_id:
            argv.extend(["--resume", resume_session_id])
        if self.options.model:
            argv.extend(["--model", self.options.model])
        if self.options.cwd:
            argv.extend(["-C", str(self.options.cwd)])
        argv.extend(["run", "--ndjson", prompt])
        extra_args = (self.options.extra_env or {}).get("JCODE_EXTRA_ARGS")
        if extra_args:
            argv.extend(extra_args.split())
        return argv

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        event_type = data.get("type")
        session_id = data.get("session_id")
        if session_id:
            self._session_id = session_id

        if event_type == "text_delta":
            text = data.get("text", "")
            if not text:
                return []
            return [AssistantMessage(content=[TextBlock(text)])]

        if event_type == "tool_exec":
            block = {"type": "tool_use", "id": data.get("id"), "name": data.get("name"), "input": {}}
            return [AssistantMessage(content=[block])]

        if event_type == "error":
            return [Message("error", content=data.get("message", "jcode error"))]

        if event_type == "done":
            return [Message("result", content=data.get("text"))]

        # start/connection_phase/connection_type/tool_start/tool_input/tool_done/tokens:
        # no UI mapping yet.
        LOG.debug(f"jcode event not yet mapped to UI: {event_type}")
        return []
