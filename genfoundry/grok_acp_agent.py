"""
Grok Build via ACP -- confirmed via Deepwiki against xai-org/grok-build
source and its own ACP e2e tests (crates/codegen/xai-grok-test-support/
src/acp_client.rs): `grok agent stdio`. Explicitly distinct from Grok's
"Headless Mode" (-p/--single, used by grok_agent.py's spawn-per-turn
adapter) per Grok's own docs -- these are two different features on the
same binary, not two names for the same thing.

Preferred over grok_agent.py's spawn-per-turn headless mode where
available: real streaming + a genuine bidirectional permission channel.
The headless adapter is kept as a fallback under the "grok-headless"
provider key.
"""

import os
import sys
import logging
from typing import Optional, List

from .acp_agent import ACPAgent

LOG = logging.getLogger("TermMate")


def find_grok_acp_cli() -> Optional[str]:
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


class GrokACPAgent(ACPAgent):
    """Client for Grok Build via ACP (`grok agent stdio`)."""

    def _default_cli_name(self) -> str:
        return "grok"

    def _find_cli(self) -> Optional[str]:
        return find_grok_acp_cli()

    def _extra_launch_args(self) -> List[str]:
        return ["agent", "stdio"]
