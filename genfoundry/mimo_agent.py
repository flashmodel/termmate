"""
MiMo Code agent (Xiaomi) via the spawn-per-turn adapter.

MiMo Code's CLI is a confirmed OpenCode fork -- mimo.xiaomi.com/mimocode's
own docs show an identical surface to opencode's: `mimo run [message]
--format json`, `-c/--continue`, `-s/--session <id>`, `--fork`. Rather than
duplicate genfoundry/opencode_agent.py's argv/event-parsing logic, this
subclasses it and only overrides the CLI binary name and locator.

If MiMo's actual JSON event schema turns out to diverge from OpenCode's
(unconfirmed beyond the shared flag surface -- MiMo's own docs don't show
example output lines any more than OpenCode's do), override _parse_event
here rather than in the shared base.
"""

import os
import sys
import logging
from typing import Optional

from .opencode_agent import OpenCodeAgent

LOG = logging.getLogger("TermMate")


def find_mimo_cli() -> Optional[str]:
    """Search common default install locations for the mimo CLI."""
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "npm", "mimo.cmd"))
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "mimo"),
        ]
    for path_str in candidates:
        if os.path.isfile(path_str) and os.access(path_str, os.X_OK):
            LOG.info(f"Found mimo CLI at default location: {path_str}")
            return path_str
    return None


class MimoAgent(OpenCodeAgent):
    """Client for MiMo Code -- an OpenCode fork with the same CLI surface."""

    def _default_cli_name(self) -> str:
        return "mimo"

    def _find_cli(self) -> Optional[str]:
        return find_mimo_cli()

    def _build_argv(self, prompt, resume_session_id):
        # Same shape as OpenCodeAgent._build_argv, but reads MIMO_EXTRA_ARGS
        # instead of OPENCODE_EXTRA_ARGS -- these are separate processes and
        # sharing one env key would leak flags meant for one into the other.
        argv = [self.cli_path, "run", prompt, "--format", "json"]
        if resume_session_id:
            argv.extend(["--session", resume_session_id])
        if self.options.model:
            argv.extend(["--model", self.options.model])
        if self.options.cwd:
            argv.extend(["--dir", str(self.options.cwd)])
        extra_args = (self.options.extra_env or {}).get("MIMO_EXTRA_ARGS")
        if extra_args:
            argv.extend(extra_args.split())
        return argv
