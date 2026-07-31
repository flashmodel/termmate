"""
OpenCode via ACP -- confirmed via Deepwiki against anomalyco/opencode
source (packages/opencode/src/cli/cmd/acp.ts): `opencode acp [--cwd <dir>]`,
an ndjson-over-stdio ACP server distinct from `opencode run --format json`
(the spawn-per-turn mode opencode_agent.py uses).

Preferred over opencode_agent.py's spawn-per-turn mode where available:
real streaming + a genuine bidirectional permission channel, replacing the
config-file-only permission story spawn-per-turn OpenCode has today. The
spawn-per-turn adapter (and MimoAgent, which subclasses it) is kept as a
fallback under the "opencode-headless" provider key.
"""

import os
import sys
import logging
from typing import Optional, List

from .acp_agent import ACPAgent

LOG = logging.getLogger("TermMate")


def find_opencode_acp_cli() -> Optional[str]:
    """Search common default install locations for the opencode CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "opencode.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [os.path.join(home, ".local", "bin", "opencode")]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found opencode CLI at default location: {path_str}")
            return path_str
    return None


class OpenCodeACPAgent(ACPAgent):
    """Client for OpenCode via ACP (`opencode acp`)."""

    def _default_cli_name(self) -> str:
        return "opencode"

    def _find_cli(self) -> Optional[str]:
        return find_opencode_acp_cli()

    def _extra_launch_args(self) -> List[str]:
        args = ["acp"]
        if self.options.cwd:
            args.extend(["--cwd", str(self.options.cwd)])
        return args
