"""
Mistral Vibe via ACP -- confirmed via Deepwiki against mistralai/mistral-vibe
source (vibe/acp/agent.py, scripts/install.sh): a SEPARATE entry-point
binary, `vibe-acp`, not a flag on `vibe`. It starts a JSON-RPC 2.0 server
implementing ACP; no additional CLI flags are needed.

Preferred over vibe_agent.py's spawn-per-turn mode where available: real
streaming, a genuine bidirectional permission channel (vibe_agent.py could
only observe Vibe's PublicCallbackEntry approval requests, never answer
them -- ACP's session/request_permission actually can be answered), and no
more need for the "assistant message = turn complete" heuristic vibe_agent.py
had to use in the absence of an explicit completion event. The spawn-per-turn
adapter is kept as a fallback under the "vibe-headless" provider key.
"""

import os
import sys
import logging
from typing import Optional, List

from .acp_agent import ACPAgent

LOG = logging.getLogger("TermMate")


def find_vibe_acp_cli() -> Optional[str]:
    """Search common default install locations for the vibe-acp CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "vibe-acp.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [os.path.join(home, ".local", "bin", "vibe-acp")]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found vibe-acp CLI at default location: {path_str}")
            return path_str
    return None


class VibeACPAgent(ACPAgent):
    """Client for Mistral Vibe via ACP (the separate `vibe-acp` binary)."""

    def _default_cli_name(self) -> str:
        return "vibe-acp"

    def _find_cli(self) -> Optional[str]:
        return find_vibe_acp_cli()
