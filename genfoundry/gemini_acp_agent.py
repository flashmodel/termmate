"""
Gemini CLI via ACP -- confirmed via Deepwiki against google-gemini/gemini-cli
source (packages/cli/src/config/config.ts, gemini.tsx): `gemini --acp`
(the older `--experimental-acp` flag is deprecated in favor of this).

Preferred over gemini_agent.py's spawn-per-turn headless mode where
available: real streaming + a genuine bidirectional permission channel
instead of the "no live approval" limitation spawn-per-turn agents have.
The headless adapter is kept as a fallback under the "gemini-headless"
provider key.
"""

import os
import sys
import logging
from typing import Optional, List

from .acp_agent import ACPAgent

LOG = logging.getLogger("TermMate")


def find_gemini_acp_cli() -> Optional[str]:
    """Search common default install locations for the gemini CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "gemini.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [os.path.join(home, ".local", "bin", "gemini")]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found gemini CLI at default location: {path_str}")
            return path_str
    return None


class GeminiACPAgent(ACPAgent):
    """Client for Gemini CLI via ACP (--acp)."""

    def _default_cli_name(self) -> str:
        return "gemini"

    def _find_cli(self) -> Optional[str]:
        return find_gemini_acp_cli()

    def _extra_launch_args(self) -> List[str]:
        return ["--acp"]
