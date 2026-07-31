"""
Kimi CLI via ACP -- Kimi's own docs state it "supports Agent Client
Protocol out of the box" via `kimi acp`. Note: this requires having
already logged in interactively once (`/login` inside a normal `kimi`
session) -- ACP mode itself has no login flow.

Preferred over kimi_agent.py's persistent print-mode adapter where
available: kimi_agent.py already got the "persistent, bidirectional"
architecture right (it was the one bespoke adapter in this codebase that
didn't need the spawn-per-turn workaround), but its OpenAI-chat-shaped
wire protocol has no permission-request mechanism at all -- tool calls
just happen. ACP adds the missing bidirectional approval channel on top
of the same persistent-process shape. The print-mode adapter is kept as a
fallback under the "kimi-headless" provider key.
"""

import os
import sys
import logging
from typing import Optional, List

from .acp_agent import ACPAgent

LOG = logging.getLogger("TermMate")


def find_kimi_acp_cli() -> Optional[str]:
    """Search common default install locations for the kimi CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "kimi.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [os.path.join(home, ".local", "bin", "kimi")]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found kimi CLI at default location: {path_str}")
            return path_str
    return None


class KimiACPAgent(ACPAgent):
    """Client for Kimi CLI via ACP (`kimi acp`)."""

    def _default_cli_name(self) -> str:
        return "kimi"

    def _find_cli(self) -> Optional[str]:
        return find_kimi_acp_cli()

    def _extra_launch_args(self) -> List[str]:
        return ["acp"]
