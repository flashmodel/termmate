import os
import shutil
import subprocess
import sys
import threading

import sublime

from ..genfoundry.claude_agent import find_claude_cli
from ..genfoundry.codex_agent import find_codex_cli
from ..genfoundry.pi_agent import find_pi_cli
from ..genfoundry.grok_agent import find_grok_cli
from ..genfoundry.kimi_agent import find_kimi_cli
from ..genfoundry.qwen_agent import find_qwen_cli
from ..genfoundry.gemini_agent import find_gemini_cli
from ..genfoundry.opencode_agent import find_opencode_cli
from .chatpanel import LoadingAnimation

AGENT_CLI_NAME = {"claude": "claude", "codex": "codex", "pi": "pi", "grok": "grok", "kimi": "kimi", "qwen": "qwen", "gemini": "gemini", "opencode": "opencode"}
AGENT_FIND_FN  = {"claude": find_claude_cli, "codex": find_codex_cli, "pi": find_pi_cli, "grok": find_grok_cli, "kimi": find_kimi_cli, "qwen": find_qwen_cli, "gemini": find_gemini_cli, "opencode": find_opencode_cli}
AGENT_LABEL    = {"claude": "Claude Code",   "codex": "Codex",        "pi": "Pi Agent",  "grok": "Grok Build", "kimi": "Kimi CLI", "qwen": "Qwen Code", "gemini": "Gemini CLI", "opencode": "OpenCode"}
AGENT_DOCS_URL = {
    "claude": "https://code.claude.com/docs/en/setup",
    "codex":  "https://developers.openai.com/codex/cli",
    "pi":     "https://pi.dev/",
    "grok":   "https://github.com/xai-org/grok-build",
    "kimi":   "https://github.com/moonshotai/kimi-cli",
    "qwen":   "https://github.com/QwenLM/qwen-code",
    "gemini": "https://github.com/google-gemini/gemini-cli",
    "opencode": "https://opencode.ai/docs",
}


def find_existing_cli(agent, settings=None):
    if settings is not None:
        custom = settings.get(f"{agent}_command")
        if custom and shutil.which(custom):
            return shutil.which(custom)
    return shutil.which(AGENT_CLI_NAME[agent]) or AGENT_FIND_FN[agent]()


def get_agent_install_info(agent):
    home   = os.path.expanduser("~")
    is_win = sys.platform == "win32"
    display = AGENT_LABEL.get(agent, agent)

    if agent == "claude":
        if is_win:
            return display, "npm install -g @anthropic-ai/claude-code", True, {}
        local_bin = os.path.join(home, ".local", "bin")
        return (
            display,
            "curl -fsSL https://claude.ai/install.sh | bash",
            True,
            {"PATH": local_bin + os.pathsep + os.environ.get("PATH", "")},
        )

    if agent == "codex":
        local_bin = os.path.join(home, ".local", "bin")
        if is_win:
            return (
                display,
                'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"',
                True,
                {},
            )
        return (
            display,
            "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
            True,
            {
                "PATH": local_bin + os.pathsep + os.environ.get("PATH", ""),
                "CODEX_NON_INTERACTIVE": "1",
            },
        )

    if agent == "pi":
        if is_win:
            cmd = (
                'powershell -ExecutionPolicy ByPass -c "'
                'if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {'
                ' winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements;'
                ' $env:PATH = [System.Environment]::GetEnvironmentVariable(\'PATH\',\'Machine\') + \';\' + [System.Environment]::GetEnvironmentVariable(\'PATH\',\'User\')'
                ' };'
                ' npm install -g --ignore-scripts --min-release-age=0 @earendil-works/pi-coding-agent'
                '"'
            )
            return display, cmd, True, {}
        local_bin = os.path.join(home, ".local", "bin")
        return (
            display,
            "curl -fsSL https://pi.dev/install.sh | sh",
            True,
            {
                "PATH": local_bin + os.pathsep + os.environ.get("PATH", ""),
                "PI_NON_INTERACTIVE": "1",
                "CI": "1",
            },
        )

    return display, None, False, {}


def _docs_phantom_html(agent):
    url = AGENT_DOCS_URL.get(agent, "")
    label = AGENT_LABEL.get(agent, agent)
    return (
        "<body style='margin:0;padding:4px 0'>"
        f"<a href='{url}' style='color:var(--bluish);text-decoration:underline;'>"
        f"View {label} documentation</a></body>"
    )


def _add_docs_phantom(view, agent):
    url = AGENT_DOCS_URL.get(agent)
    if not url:
        return
    view.add_phantom(
        "docs_link",
        sublime.Region(view.size(), view.size()),
        _docs_phantom_html(agent),
        sublime.LAYOUT_BLOCK,
    )


class _InstallView:
    """Plain-text scratch view that streams install output."""

    def __init__(self, window, title, subtitle, cmd, agent):
        self._agent = agent
        view = window.new_file()
        view.set_name("TermMate Install")
        view.set_scratch(True)
        view.settings().set("word_wrap", True)
        view.settings().set("line_numbers", False)
        view.settings().set("gutter", False)
        self._view = view
        self._loading = LoadingAnimation(view)
        self._write(f"{title}\n{subtitle}\n\n{cmd}\n\n")
        self._loading.start(lambda: sublime.Region(view.size(), view.size()), "Installing…")

    def _write(self, text):
        self._view.run_command("append", {"characters": text, "force": True, "scroll_to_end": True})

    def append_log(self, line):
        self._write(line)

    def set_status(self, text, show_docs=False):
        self._loading.stop()
        if text:
            self._write(f"\n{text}")
        if show_docs:
            _add_docs_phantom(self._view, self._agent)


def _new_scratch_view(window, title):
    view = window.new_file()
    view.set_name("TermMate Install")
    view.set_scratch(True)
    view.settings().set("word_wrap", True)
    view.settings().set("line_numbers", False)
    view.settings().set("gutter", False)
    return view


def run_install(window, agent, on_success):
    display_name, cmd, supported, extra_env = get_agent_install_info(agent)

    if not supported:
        view = _new_scratch_view(window, f"Install {display_name}")
        view.run_command("append", {
            "characters": (
                f"Install {display_name}\nWindows — manual setup required\n\n"
                f"{display_name} does not have a Windows installer yet.\n"
                "Please refer to the official documentation for manual setup instructions.\n"
            ),
            "force": True,
        })
        _add_docs_phantom(view, agent)
        return

    existing = find_existing_cli(agent)

    if existing and not os.access(existing, os.W_OK):
        cmd_display = cmd if sys.platform == "win32" else f"{cmd}"
        note = (
            "You may be asked to allow an administrator prompt."
            if sys.platform == "win32"
            else "You may be asked for your password."
        )
        view = _new_scratch_view(window, f"Update {display_name}")
        view.run_command("append", {
            "characters": (
                f"Update {display_name}\n\n"
                f"Installed at: {existing}\n\n"
                f"This installation is owned by an administrator and cannot be updated automatically.\n"
                f"To update, run in a terminal:\n\n  {cmd_display}\n\n{note}\n"
            ),
            "force": True,
        })
        _add_docs_phantom(view, agent)
        return

    if existing:
        title    = f"Update {display_name}"
        subtitle = f"Updating from {existing}"
    else:
        title    = f"Install {display_name}"
        subtitle = "Installing — this may take a moment"

    sheet = _InstallView(window, title, subtitle, cmd, agent)

    def _worker():
        env = os.environ.copy()
        env.update(extra_env)
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                **kwargs,
            )
            for line in proc.stdout:
                sublime.set_timeout(lambda l=line: sheet.append_log(l), 0)
            proc.wait()
            if proc.returncode == 0:
                sublime.set_timeout(
                    lambda: sheet.set_status(f"Done — {display_name} installed successfully."), 0
                )
                sublime.set_timeout(
                    lambda: on_success(agent, display_name, lambda t: sheet.append_log(t)), 0
                )
            else:
                sublime.set_timeout(
                    lambda: sheet.set_status(f"Install failed (exit code {proc.returncode}).", show_docs=True), 0
                )
        except Exception as exc:
            sublime.set_timeout(
                lambda: sheet.set_status(f"Error: {exc}", show_docs=True), 0
            )

    threading.Thread(target=_worker, daemon=True).start()


def get_agent_list_items(settings):
    """Return ListInputItems for all agents, annotated with install status."""
    is_win = sys.platform == "win32"
    items = []
    for agent in AGENT_FIND_FN:
        existing = find_existing_cli(agent, settings)
        if is_win:
            location = "%APPDATA%\\npm"
        else:
            location = "~/.local/bin"
        status = f"installed: {existing}" if existing else location
        items.append(sublime.ListInputItem(AGENT_LABEL[agent], agent, annotation=status))
    return items
