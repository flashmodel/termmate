"""
Test the launch-arg construction for each ACP subclass added alongside
JunieAgent (GeminiACPAgent, GrokACPAgent, OpenCodeACPAgent, VibeACPAgent,
KimiACPAgent). The shared ACPAgent transport/protocol logic is already
covered by test_acp_junie_msg.py; this file only checks that each
subclass spawns the right binary with the right ACP-mode args, per each
vendor's confirmed launch command:
  gemini --acp
  grok agent stdio
  opencode acp [--cwd <dir>]
  vibe-acp                    (separate binary, no extra args)
  kimi acp
"""

import sys
import unittest
from unittest.mock import AsyncMock, patch

import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from genfoundry.gemini_acp_agent import GeminiACPAgent
from genfoundry.grok_acp_agent import GrokACPAgent
from genfoundry.opencode_acp_agent import OpenCodeACPAgent
from genfoundry.vibe_acp_agent import VibeACPAgent
from genfoundry.kimi_acp_agent import KimiACPAgent
from genfoundry.base_agent import AgentOptions


class FakeProcess:
    """Minimal fake -- just enough for connect()'s initialize/session/new handshake."""

    def __init__(self):
        self.stdin = None
        self.stdout = self
        self.stderr = self
        self.returncode = None

    async def read(self, n):
        import asyncio
        await asyncio.sleep(10)
        return b""

    async def readline(self):
        import asyncio
        await asyncio.sleep(10)
        return b""

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        pass


async def _connect_and_capture_argv(agent_cls, cli_path, cwd=None):
    opts = AgentOptions(cli_path=cli_path, cwd=cwd)
    with patch("shutil.which", return_value=cli_path):
        agent = agent_cls(options=opts)

    captured = []

    async def fake_exec(*argv, **kwargs):
        captured.append(argv)
        return FakeProcess()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=fake_exec)):
        with patch.object(agent, "_rpc_request", new=AsyncMock(return_value={"sessionId": "s1"})):
            await agent.connect()
    await agent.disconnect()
    return captured[0]


class TestACPVariantLaunchArgs(unittest.IsolatedAsyncioTestCase):

    async def test_gemini_acp_launch_args(self):
        argv = await _connect_and_capture_argv(GeminiACPAgent, "/usr/bin/gemini")
        self.assertEqual(list(argv), ["/usr/bin/gemini", "--acp"])

    async def test_grok_acp_launch_args(self):
        argv = await _connect_and_capture_argv(GrokACPAgent, "/usr/bin/grok")
        self.assertEqual(list(argv), ["/usr/bin/grok", "agent", "stdio"])

    async def test_opencode_acp_launch_args_with_cwd(self):
        argv = await _connect_and_capture_argv(OpenCodeACPAgent, "/usr/bin/opencode", cwd="/tmp/proj")
        self.assertEqual(list(argv), ["/usr/bin/opencode", "acp", "--cwd", "/tmp/proj"])

    async def test_opencode_acp_launch_args_defaults_cwd_to_os_getcwd(self):
        # AgentOptions.cwd defaults to os.getcwd() when unset (base_agent.py),
        # never None -- so --cwd is always present, just not caller-specified.
        argv = await _connect_and_capture_argv(OpenCodeACPAgent, "/usr/bin/opencode")
        self.assertEqual(list(argv)[:2], ["/usr/bin/opencode", "acp"])
        self.assertIn("--cwd", argv)

    async def test_vibe_acp_launch_args_no_extra_flags(self):
        argv = await _connect_and_capture_argv(VibeACPAgent, "/usr/bin/vibe-acp")
        self.assertEqual(list(argv), ["/usr/bin/vibe-acp"])

    async def test_kimi_acp_launch_args(self):
        argv = await _connect_and_capture_argv(KimiACPAgent, "/usr/bin/kimi")
        self.assertEqual(list(argv), ["/usr/bin/kimi", "acp"])


if __name__ == "__main__":
    unittest.main()
