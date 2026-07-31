"""
Qwen Code agent (qwenlm/qwen-code) via the spawn-per-turn adapter.

Qwen has no persistent bidirectional stdin (no --input-format flag exists);
each turn is a fresh `qwen -p "<prompt>" --output-format stream-json`,
resumed next turn via `--resume <session_id>`. See genfoundry/
spawn_per_turn_agent.py for why this needs a different BaseAgent shape than
Claude Code / Codex / Kimi.

Event schema confirmed live (genfoundry/tests/test_qwen_msg.py fixtures are
lifted from an actual `qwen -p ... --output-format stream-json` run) and via
Context7 docs: the envelope is structurally identical to Claude Code's own
stream-json protocol --
  {"type":"system","subtype":"init","session_id": "..."}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}
  {"type":"result","subtype":"success","result":"..."}
-- including Anthropic-shaped tool_use content blocks, so this reuses the
same block-parsing shape as claude_agent.py rather than reinventing it.

Confirmed permission primitives (packages/cli/src/config/config.ts):
--allowed-tools (bypass confirmation for named tools), --approval-mode yolo,
--max-tool-calls. No tool-allowlist is passed by default -- see
GrokAgent's docstring for why guessing tool names is worse than not
setting the flag. Set QWEN_EXTRA_ARGS to add these once you know the
tool names your qwen build actually registers.
"""

import os
import sys
import logging
from typing import Optional, Dict, Any, List

from .base_agent import Message, MessageType, TextBlock, AssistantMessage
from .spawn_per_turn_agent import SpawnPerTurnAgent

LOG = logging.getLogger("TermMate")


def find_qwen_cli() -> Optional[str]:
    """Search common default install locations for the qwen CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "qwen.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "qwen"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found qwen CLI at default location: {path_str}")
            return path_str
    return None


class QwenAgent(SpawnPerTurnAgent):
    """Client for Qwen Code via spawn-per-turn stream-json."""

    def _default_cli_name(self) -> str:
        return "qwen"

    def _find_cli(self) -> Optional[str]:
        return find_qwen_cli()

    def _build_argv(self, prompt: str, resume_session_id: Optional[str]) -> List[str]:
        argv = [self.cli_path, "-p", prompt, "--output-format", "stream-json"]
        if resume_session_id:
            argv.extend(["--resume", resume_session_id])
        if self.options.model:
            argv.extend(["--model", self.options.model])
        extra_args = (self.options.extra_env or {}).get("QWEN_EXTRA_ARGS")
        if extra_args:
            argv.extend(extra_args.split())
        return argv

    def _parse_event(self, data: Dict[str, Any]) -> List[Message]:
        msg_type = data.get("type")

        if msg_type == "system" and data.get("subtype") == "init":
            session_id = data.get("session_id")
            if session_id:
                self._session_id = session_id
            return []

        if msg_type == "assistant":
            message_data = data.get("message", {})
            content_blocks = message_data.get("content", [])
            blocks: List[Any] = []
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        blocks.append(TextBlock(block.get("text", "")))
                    else:
                        blocks.append(block)
            return [AssistantMessage(content=blocks, msg_id=message_data.get("id"))]

        if msg_type == "result":
            if data.get("session_id"):
                self._session_id = data["session_id"]
            return [Message("result", content=data.get("result"), msg_id=data.get("uuid"))]

        LOG.debug(f"qwen event not yet mapped to UI: {msg_type}")
        return []
