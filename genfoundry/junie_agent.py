"""
Junie agent (JetBrains) via the ACP client.

junie.jetbrains.com's own CLI docs only show a single-blob --output-format
json, no streaming mode -- that alone would be a poor architectural fit for
chatview's live message loop. But Junie also implements the Agent Client
Protocol (confirmed via github.com/JetBrains/junie's ACP Agent Registry:
its registry-nightly.json entry launches the binary with "--acp=true"),
which is a real bidirectional JSON-RPC streaming protocol -- see
genfoundry/acp_agent.py for the generic client. This is the path used here
instead of the single-blob print mode.
"""

import os
import sys
import logging
from typing import Optional, List

from .acp_agent import ACPAgent

LOG = logging.getLogger("TermMate")


def find_junie_cli() -> Optional[str]:
    """Search common default install locations for the junie CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "junie.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "junie"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found junie CLI at default location: {path_str}")
            return path_str
    return None


class JunieAgent(ACPAgent):
    """Client for Junie via ACP (--acp=true)."""

    def _default_cli_name(self) -> str:
        return "junie"

    def _find_cli(self) -> Optional[str]:
        return find_junie_cli()

    def _extra_launch_args(self) -> List[str]:
        return ["--acp=true"]
