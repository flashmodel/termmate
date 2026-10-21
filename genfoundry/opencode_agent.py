"""
OpenCode Agent - Server API Implementation.

This adapter starts ``opencode serve`` on an operating-system-assigned port
and talks to it through the HTTP/SSE server API.  It deliberately uses only
the Python standard library so it can run inside Sublime Text without extra
packages.
"""

import asyncio
import base64
import difflib
import json
import logging
import os
import queue as thread_queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .base_agent import AgentOptions, BaseAgent, Message, MessageType


LOG = logging.getLogger("TermMate")

_SERVER_URL_RE = re.compile(
    r"(?:opencode\s+)?server listening on\s+(https?://[^\s]+)", re.IGNORECASE
)
_SERVER_PASSWORD_RE = re.compile(
    r"server password\s+([^\s]+)", re.IGNORECASE
)
_VERSION_RE = re.compile(
    r"(?:opencode\s+)?v?(\d+\.\d+\.\d+)", re.IGNORECASE
)


def parse_opencode_version_output(
    output: str,
) -> Tuple[Optional[int], Optional[str]]:
    """Parse version from 'opencode --version' output."""
    m = _VERSION_RE.search(output or "")
    if m:
        ver_str = m.group(1)
        major = int(ver_str.split(".")[0])
        return major, ver_str
    return None, None


def detect_opencode_version_info(
    cli_path: Optional[str] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Execute '--version' and return (major_version, version_str)."""
    cli = cli_path or "opencode"
    cmd = [cli, "--version"]
    try:
        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=3.0,
            check=False,
            **kwargs,
        )
        output = (result.stdout or "") + " " + (result.stderr or "")
        major, ver_str = parse_opencode_version_output(output)
        if major:
            proto_ver = 2 if major >= 2 else 1
            LOG.info(
                "Detected OpenCode CLI version %s: using v%d protocol",
                ver_str or f"v{major}",
                proto_ver,
            )
        return major, ver_str
    except Exception as err:
        LOG.debug("detect_opencode_version failed for %s: %s", cmd, err)
    return None, None


def detect_opencode_version(cli_path: Optional[str] = None) -> Optional[int]:
    """Detect OpenCode major version (1 or 2) by executing '--version'."""
    major, _ = detect_opencode_version_info(cli_path)
    return major


def get_termmate_opencode_state_dir() -> str:
    """Return the private XDG_STATE_HOME directory used by TermMate for OpenCode."""
    base = os.path.join(tempfile.gettempdir(), "termmate_cache")
    state_dir = os.path.join(base, "TermMate", "opencode_state")
    try:
        os.makedirs(state_dir, exist_ok=True)
    except Exception:
        pass
    return state_dir


def find_opencode_v2_password(
    extra_env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Search TermMate private state and default locations for OpenCode password."""
    source = {**os.environ, **(extra_env or {})}
    home = os.path.expanduser("~")
    termmate_state = get_termmate_opencode_state_dir()
    candidates = [
        os.path.join(termmate_state, "opencode", "service.json"),
    ]
    custom_state = source.get("XDG_STATE_HOME")
    if custom_state:
        candidates.append(os.path.join(custom_state, "opencode", "service.json"))

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        localappdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(os.path.join(appdata, "opencode", "service.json"))
        if localappdata:
            candidates.append(os.path.join(localappdata, "opencode", "service.json"))
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
        state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
        candidates.extend([
            os.path.join(config_home, "opencode", "service.json"),
            os.path.join(state_home, "opencode", "service.json"),
            os.path.join(home, ".config", "opencode", "service.json"),
            os.path.join(home, ".local", "state", "opencode", "service.json"),
        ])
    for path_candidate in candidates:
        try:
            if os.path.isfile(path_candidate):
                with open(path_candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data.get("password"):
                        return str(data["password"])
        except Exception:
            pass
    return None


def find_opencode_v2_service(
    extra_env: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, str]]:
    """Discover a running OpenCode v2 service endpoint and password."""
    source = {**os.environ, **(extra_env or {})}
    explicit_url = source.get("OPENCODE_SERVER_URL") or source.get("OPENCODE_URL")
    explicit_password = (
        source.get("OPENCODE_SERVER_PASSWORD")
        or source.get("OPENCODE_PASSWORD")
    )
    url = explicit_url
    password = explicit_password

    # Always prioritize TermMate private state directory
    termmate_state = get_termmate_opencode_state_dir()
    state_candidates = [
        os.path.join(termmate_state, "opencode", "service.json"),
    ]
    custom_state = source.get("XDG_STATE_HOME")
    if custom_state:
        state_candidates.append(os.path.join(custom_state, "opencode", "service.json"))

    home = os.path.expanduser("~")
    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA")
        appdata = os.environ.get("APPDATA")
        if localappdata:
            state_candidates.append(os.path.join(localappdata, "opencode", "service.json"))
        if appdata:
            state_candidates.append(os.path.join(appdata, "opencode", "service.json"))
    else:
        state_candidates.extend([
            os.path.join(home, ".local", "state", "opencode", "service.json"),
        ])

    for path_candidate in state_candidates:
        try:
            if os.path.isfile(path_candidate):
                with open(path_candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        if not url and data.get("url"):
                            url = str(data["url"]).rstrip("/")
                            if not explicit_password and data.get("password"):
                                password = str(data["password"])
        except Exception:
            pass

    if not password:
        password = find_opencode_v2_password(extra_env)

    if url:
        return {"url": url, "password": password or ""}
    return None


def probe_opencode_service(
    url: str, password: Optional[str] = None, timeout: float = 1.0
) -> bool:
    """Check if an OpenCode service endpoint is alive and responsive."""
    headers = {"Accept": "application/json"}
    if password:
        token = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    for check_path in ("/api/info", "/global/health"):
        req = Request(url.rstrip("/") + check_path, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", getattr(resp, "code", 200))
                if status == 200:
                    return True
        except Exception:
            continue
    return False


_ACTIVE_OPENCODE_SERVER: Optional[Dict[str, Any]] = None
_ACTIVE_CLIENTS: set = set()
_IDLE_SHUTDOWN_TIMER: Optional[threading.Timer] = None
_DEFAULT_IDLE_SHUTDOWN_DELAY = 180.0
_SUBLIME_SERVER_PROCESS: Optional[subprocess.Popen] = None
_SUBLIME_SERVER_JOB_HANDLE: Optional[int] = None


def get_opencode_idle_timeout() -> float:
    """Return idle shutdown delay in seconds from settings, or default 180s."""
    try:
        import sublime
        settings = sublime.load_settings("TermMate.sublime-settings")
        val = settings.get("opencode_idle_timeout", _DEFAULT_IDLE_SHUTDOWN_DELAY)
        if isinstance(val, (int, float)):
            return float(val)
    except Exception:
        pass
    return _DEFAULT_IDLE_SHUTDOWN_DELAY


def cancel_idle_shutdown_timer() -> None:
    """Cancel any pending OpenCode idle shutdown timer."""
    global _IDLE_SHUTDOWN_TIMER
    timer = _IDLE_SHUTDOWN_TIMER
    _IDLE_SHUTDOWN_TIMER = None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass


def schedule_idle_shutdown_timer(
    cli_path: Optional[str] = None, delay: Optional[float] = None
) -> None:
    """Schedule OpenCode idle shutdown if no clients are active."""
    global _IDLE_SHUTDOWN_TIMER
    cancel_idle_shutdown_timer()
    if _ACTIVE_CLIENTS:
        return

    timeout = get_opencode_idle_timeout() if delay is None else delay
    if timeout < 0:
        return
    if timeout == 0:
        stop_sublime_opencode_server(cli_path)
        return

    def _on_idle():
        if _ACTIVE_CLIENTS:
            return
        LOG.info("OpenCode server idle for %.0fs; stopping server", timeout)
        stop_sublime_opencode_server(cli_path)

    timer = threading.Timer(timeout, _on_idle)
    timer.daemon = True
    _IDLE_SHUTDOWN_TIMER = timer
    timer.start()
    LOG.debug("Scheduled OpenCode idle shutdown in %.0fs", timeout)


def get_active_opencode_server() -> Optional[Dict[str, Any]]:
    """Return the active in-process OpenCode server endpoint, if any."""
    active = _ACTIVE_OPENCODE_SERVER
    if active:
        return dict(active)
    return None


def set_active_opencode_server(
    url: Optional[str],
    password: Optional[str] = None,
    version: int = 2,
) -> None:
    """Record or clear the active in-process OpenCode server endpoint."""
    global _ACTIVE_OPENCODE_SERVER
    if url:
        _ACTIVE_OPENCODE_SERVER = {
            "url": url.rstrip("/"),
            "password": password or "",
            "version": version,
        }
    else:
        _ACTIVE_OPENCODE_SERVER = None


def _poll_opencode_service(
    env: Dict[str, str], timeout: float = 0.0, interval: float = 0.05
) -> Optional[Dict[str, str]]:
    """Check or poll until an OpenCode service is discovered and responsive."""
    deadline = time.monotonic() + timeout
    while True:
        svc = find_opencode_v2_service(env)
        if svc and probe_opencode_service(svc["url"], svc.get("password")):
            return svc
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def ensure_opencode_v2_service(
    cli_path: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    timeout: float = 6.0,
) -> Optional[Dict[str, str]]:
    """Ensure a shared OpenCode v2 background service is running and return its endpoint."""
    global _SUBLIME_SERVER_PROCESS, _SUBLIME_SERVER_JOB_HANDLE
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    if "XDG_STATE_HOME" not in env:
        env["XDG_STATE_HOME"] = get_termmate_opencode_state_dir()

    # 1. Return immediately if already running
    svc = _poll_opencode_service(env)
    if svc:
        return svc

    # 2. Validate CLI executable & version
    cli = find_opencode_cli(cli_path)
    if not cli or detect_opencode_version(cli) == 1:
        return None

    # 3. Spawn 'opencode serve --service' if not already starting
    existing_proc = _SUBLIME_SERVER_PROCESS
    if existing_proc is None or existing_proc.poll() is not None:
        cmd = [cli, "serve", "--service"]
        kwargs: Dict[str, Any] = {}
        job_handle = None

        if sys.platform == "win32":
            job_handle = _create_win32_job_object()
            if not job_handle:
                LOG.warning("Failed to create the OpenCode Windows Job Object")
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | 0x00000004  # CREATE_SUSPENDED
            )
        else:
            cmd = [
                "/bin/sh",
                "-c",
                (
                    'exec 3<&0; "$@" </dev/null & child=$!; '
                    '(cat <&3 >/dev/null; kill -TERM -$$ 2>/dev/null) & watcher=$!; '
                    'wait "$child"; status=$?; '
                    'kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null; '
                    'exit "$status"'
                ),
                "termmate-opencode-wrapper",
                *cmd,
            ]
            kwargs["stdin"] = subprocess.PIPE
            kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                **kwargs,
            )
        except Exception as error:
            if job_handle:
                _close_win32_handle(job_handle)
            LOG.warning("Failed to start OpenCode v2 service: %s", error)
            return None

        if sys.platform == "win32" and job_handle:
            assigned = _assign_process_to_job_object(job_handle, proc)
            resumed = assigned and _resume_win32_process(proc)
            if not resumed:
                _close_win32_handle(job_handle)
                if proc.poll() is None:
                    proc.kill()
                return None

        _SUBLIME_SERVER_PROCESS = proc
        _SUBLIME_SERVER_JOB_HANDLE = job_handle

    svc = _poll_opencode_service(env, timeout=timeout)
    if svc:
        return svc

    stop_sublime_opencode_server(cli_path)
    return None


def get_sublime_opencode_server(
    cli_path: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Get or start the Sublime Text private OpenCode server singleton.

    Checks in-memory cache first, then private service.json,
    then starts the private daemon via ensure_opencode_v2_service.
    """
    global _ACTIVE_OPENCODE_SERVER
    active = _ACTIVE_OPENCODE_SERVER
    if active and probe_opencode_service(active["url"], active.get("password")):
        return dict(active)

    env = dict(extra_env or {})
    env["XDG_STATE_HOME"] = get_termmate_opencode_state_dir()

    svc = ensure_opencode_v2_service(cli_path, env)
    if svc:
        info = {
            "url": svc["url"].rstrip("/"),
            "password": svc.get("password") or "",
            "version": 2,
        }
        _ACTIVE_OPENCODE_SERVER = info
        return dict(info)

    return None


def stop_sublime_opencode_server(
    cli_path: Optional[str] = None,
) -> None:
    """Stop the Sublime Text private OpenCode server singleton and clean up."""
    global _SUBLIME_SERVER_PROCESS, _SUBLIME_SERVER_JOB_HANDLE
    cancel_idle_shutdown_timer()
    _ACTIVE_CLIENTS.clear()
    proc, _SUBLIME_SERVER_PROCESS = _SUBLIME_SERVER_PROCESS, None
    job_handle, _SUBLIME_SERVER_JOB_HANDLE = _SUBLIME_SERVER_JOB_HANDLE, None

    set_active_opencode_server(None)

    if proc:
        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if sys.platform != "win32" and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
        except Exception:
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

    if job_handle:
        _close_win32_handle(job_handle)

    cli = find_opencode_cli(cli_path)
    if cli:
        env = os.environ.copy()
        env["XDG_STATE_HOME"] = get_termmate_opencode_state_dir()
        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            p = subprocess.Popen(
                [cli, "service", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                **kwargs,
            )
            p.communicate(timeout=3.0)
        except Exception as err:
            LOG.debug("stop_sublime_opencode_server failed: %s", err)

    service_file = os.path.join(
        get_termmate_opencode_state_dir(), "opencode", "service.json"
    )
    if os.path.isfile(service_file):
        try:
            os.remove(service_file)
        except OSError:
            pass


_TOOL_NAMES = {
    "bash": "Bash",
    "shell": "Bash",
    "edit": "Edit",
    "write": "Write",
    "apply_patch": "Edit",
    "patch": "Edit",
    "read": "Read",
    "glob": "Glob",
    "grep": "Grep",
    "list": "Glob",
    "webfetch": "WebFetch",
    "websearch": "WebSearch",
    "task": "Task",
    "skill": "Skill",
    "question": "AskUserQuestion",
    "external_directory": "ExternalDirectory",
    "doom_loop": "DoomLoop",
}


def find_opencode_cli(cli_path: Optional[str] = None) -> Optional[str]:
    """Find the OpenCode CLI from custom path, system PATH, or default install locations."""
    if cli_path:
        return shutil.which(cli_path) or (
            cli_path if os.path.isfile(cli_path) and os.access(cli_path, os.X_OK) else None
        )

    found = shutil.which("opencode")
    if found:
        return found

    candidates: List[str] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "npm", "opencode.cmd"),
                os.path.join(appdata, "npm", "opencode"),
            ])
    else:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".opencode", "bin", "opencode"),
            os.path.join(home, ".local", "bin", "opencode"),
            os.path.join(home, ".npm-global", "bin", "opencode"),
            os.path.join(home, ".yarn", "bin", "opencode"),
            os.path.join(home, ".bun", "bin", "opencode"),
            "/usr/local/bin/opencode",
            "/usr/bin/opencode",
            "/opt/homebrew/bin/opencode",
            "/home/linuxbrew/.linuxbrew/bin/opencode",
        ]

    for path in candidates:
        try:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                LOG.info("Found OpenCode CLI at default location: %s", path)
                return path
        except Exception:
            pass
    return None


def _win32_kernel32():
    """Return kernel32 with pointer-sized signatures used by process cleanup."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    return kernel32


def _close_win32_handle(handle: Optional[int]) -> bool:
    if not handle or sys.platform != "win32":
        return False
    try:
        return bool(_win32_kernel32().CloseHandle(handle))
    except Exception as err:
        LOG.warning("Failed to close Win32 handle: %s", err)
        return False


def _create_win32_job_object() -> Optional[int]:
    """Create a Windows Job Object configured to kill all processes on close."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = _win32_kernel32()

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        res = kernel32.SetInformationJobObject(
            h_job,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            kernel32.CloseHandle(h_job)
            return None
        return h_job
    except Exception as err:
        LOG.warning("Failed to create Win32 Job Object: %s", err)
        return None


def _assign_process_to_job_object(h_job: int, process: Any) -> bool:
    """Assign a process (asyncio process, Popen, or PID) to a Windows Job Object."""
    if not h_job or sys.platform != "win32":
        return False
    try:
        kernel32 = _win32_kernel32()

        h_proc = None
        need_close = False
        pid = None

        if isinstance(process, int):
            pid = process
        elif hasattr(process, "pid") and process.pid is not None:
            pid = process.pid

        if hasattr(process, "_handle") and process._handle:
            h_proc = process._handle
        elif pid is not None:
            PROCESS_SET_QUOTA = 0x0100
            PROCESS_TERMINATE = 0x0001
            h_proc = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid
            )
            need_close = True

        if not h_proc:
            return False

        try:
            res = kernel32.AssignProcessToJobObject(h_job, h_proc)
            return bool(res)
        finally:
            if need_close and h_proc:
                kernel32.CloseHandle(h_proc)
    except Exception as err:
        LOG.warning("Failed to assign process to Job Object: %s", err)
        return False


def _resume_win32_process(process: Any) -> bool:
    """Resume every initial thread of a process created with CREATE_SUSPENDED."""
    if sys.platform != "win32" or not getattr(process, "pid", None):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        kernel32 = _win32_kernel32()
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return False

        resumed = 0
        try:
            entry = THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                    if thread:
                        try:
                            if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                                resumed += 1
                        finally:
                            kernel32.CloseHandle(thread)
                found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return resumed > 0
    except Exception as err:
        LOG.warning("Failed to resume suspended OpenCode process: %s", err)
        return False


class OpenCodeHTTPError(RuntimeError):
    """An error response returned by the OpenCode server."""

    def __init__(self, method: str, url: str, status: Optional[int], detail: str):
        prefix = f"OpenCode {method} {url}"
        if status is not None:
            prefix += f" returned HTTP {status}"
        super().__init__(f"{prefix}: {detail}")
        self.status = status


class OpenCodeAgent(BaseAgent):
    """A persistent OpenCode client supporting both v1 and v2 API protocols.

    Protocol Architecture & Message Flow:
    * OpenCode v2:
      - Service: Shared background daemon (`opencode serve --service`).
      - REST API: Prefixed with `/api` (`POST /api/session`, `/api/session/{id}/prompt`).
      - SSE Stream: `GET /api/event`.
      - Event Model: Fine-grained session events:
        - `session.text.delta`: Streaming assistant text deltas.
        - `session.reasoning.delta`: Streaming reasoning/thought deltas.
        - `session.inbox.enqueued` / `session.inbox.delivered`: Prompt admission.
        - `session.tool.called` / `progress` / `success` / `failed`: Tool lifecycle.
        - `session.execution.succeeded` / `interrupted` / `failed`: Turn completion.

    * OpenCode v1:
      - Service: Ephemeral per-session process (`opencode serve --port <port>`).
      - REST API: Root-level endpoints (`POST /session`, `/session/{id}/prompt_async`).
      - SSE Stream: `GET /event`.
      - Event Model: Part-based hierarchical events:
        - `message.part.delta`: Streaming text / reasoning deltas on a part.
        - `message.part.updated`: Part updates (final text, reasoning, tools).
        - `message.updated`: Message-level metadata (roles, error status).
        - `session.diff`: File modification diffs.
        - `session.status` / `session.idle`: Turn completion signaling.

    * Cross-Protocol / Normalization:
      - `permission.asked` / `permission.updated`: Tool authorization requests.
      - Normalization: Translates all events into TermMate standard `Message` objects
        (`MessageType.TEXT`, `TOOL_USE`, `THINKING`, `PLAN_DELTA`, `ERROR`, `STOP`).
    """

    def __init__(
        self,
        options: Optional[AgentOptions] = None,
        version: Optional[int] = None,
    ):
        super().__init__(options)

        self.cli_path = (
            find_opencode_cli(self.options.cli_path)
            or self.options.cli_path
            or "opencode"
        )

        self.version: int = version or getattr(self.options, "version", None) or 0
        self.version_str: Optional[str] = None
        if self.version:
            self.version_str = f"v{self.version}"
        elif self.cli_path:
            detected, ver_str = detect_opencode_version_info(self.cli_path)
            if detected:
                self.version = detected
                self.version_str = ver_str

        self.server_url: Optional[str] = None
        self._server_password: Optional[str] = None

        self._session_id: Optional[str] = self.options.session_id
        self.plan_mode = self.options.plan_mode
        self.available_models: List[Dict[str, Any]] = []

        self._is_connected = False
        self._turn_active = False
        self._turn_plan_mode = False
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._uses_session_status = False
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_task: Optional[asyncio.Task] = None

        self._server_process: Optional[asyncio.subprocess.Process] = None
        self._server_log_task: Optional[asyncio.Task] = None
        self._job_handle: Optional[int] = None

        self._sse_thread: Optional[threading.Thread] = None
        self._sse_stop = threading.Event()
        self._sse_ready = threading.Event()
        self._sse_response = None
        self._sse_response_lock = threading.Lock()

        self._text_cache: Dict[str, str] = {}
        self._part_types: Dict[str, str] = {}
        self._message_roles: Dict[str, str] = {}
        self._user_message_ids = set()
        self._awaiting_user_message_id = False
        self._terminal_tools = set()
        self._tool_calls: Dict[str, Dict[str, Any]] = {}
        self._permission_sessions: Dict[str, str] = {}
        self._seen_permissions = set()
        self._diff_cache: Dict[str, Any] = {}
        self._plan_text = ""

    @property
    def thread_id(self) -> Optional[str]:
        """Compatibility with AgentThread.session_id."""
        return self._session_id

    def _runtime_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(self.options.extra_env)

        inline: Dict[str, Any] = {}
        raw_inline = env.get("OPENCODE_CONFIG_CONTENT")
        if raw_inline:
            try:
                value = json.loads(raw_inline)
                if isinstance(value, dict):
                    inline = value
            except (TypeError, ValueError):
                LOG.warning("Ignoring invalid OPENCODE_CONFIG_CONTENT")

        permission: Dict[str, Any] = {"*": "ask"}
        for tool in self.options.allowed_tools:
            key = self._permission_key(tool)
            if key:
                permission[key] = "allow"

        approve_mode = self.options.approve_mode or "allow-edit"
        if approve_mode in ("allow-edit", "accept-all"):
            # OpenCode groups Edit, Write, ApplyPatch, and Patch under the
            # single `edit` permission key.
            permission["edit"] = "allow"

        if self.options.add_dirs:
            permission["external_directory"] = {
                os.path.join(os.path.abspath(path), "**"): "allow"
                for path in self.options.add_dirs
            }

        if "AskUserQuestion" in self.options.disallowed_tools:
            permission["question"] = "deny"

        inline["permission"] = permission
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
        return env

    @staticmethod
    def _permission_key(tool: str) -> Optional[str]:
        normalized = tool.replace("_", "").replace("-", "").lower()
        aliases = {
            "bash": "bash",
            "edit": "edit",
            "write": "edit",
            "applypatch": "edit",
            "read": "read",
            "glob": "glob",
            "grep": "grep",
            "webfetch": "webfetch",
            "websearch": "websearch",
            "task": "task",
            "skill": "skill",
            "todowrite": "todowrite",
            "todoread": "todowrite",
            "askuserquestion": "question",
            "question": "question",
        }
        return aliases.get(normalized)

    async def connect(self, prompt: Optional[str] = None) -> None:
        if self._is_connected:
            raise RuntimeError("Client is already connected")

        self._loop = asyncio.get_running_loop()
        env = self._runtime_env()

        protocol = "v2" if self.version >= 2 else "v1"
        if self.version_str:
            LOG.info(
                "Connecting to OpenCode %s (using %s protocol)",
                self.version_str,
                protocol,
            )
        is_explicit_v1 = self.version == 1
        if self.cli_path and not is_explicit_v1:
            ver = detect_opencode_version(self.cli_path)
            if ver == 1:
                is_explicit_v1 = True

        if not is_explicit_v1:
            loop = self._loop or asyncio.get_running_loop()
            server_info = await loop.run_in_executor(
                None, lambda: get_sublime_opencode_server(self.cli_path, env)
            )
            if server_info:
                self.server_url = server_info["url"]
                self._server_password = server_info.get("password") or ""
                if self.version == 0:
                    self.version = 2
                    self.version_str = "v2"
                LOG.info("Reusing running OpenCode v2 service at %s", self.server_url)

        if not self.server_url:
            self.server_url = await self._start_server(env)

        set_active_opencode_server(
            self.server_url, self._server_password, version=self.version
        )

        cancel_idle_shutdown_timer()
        _ACTIVE_CLIENTS.add(self)

        try:
            await self._finish_connect(env, prompt)
        except Exception:
            await self.disconnect()
            raise

    async def _finish_connect(
        self, env: Dict[str, str], prompt: Optional[str]
    ) -> None:
        if self.version >= 2:
            await self._http("GET", "/api/info", include_directory=False)
        else:
            await self._http("GET", "/global/health", include_directory=False)

        self._is_connected = True
        self._event_task = asyncio.create_task(self._process_events())
        self._start_sse_thread(env)

        ready = await self._loop.run_in_executor(
            None, lambda: self._sse_ready.wait(timeout=5.0)
        )
        if not ready:
            await self.disconnect()
            raise RuntimeError("Timed out connecting to the OpenCode event stream")

        # Do not create an empty server-side session merely because the chat
        # view connected.  A fresh session is created lazily when the first
        # prompt is sent.  Existing sessions still need to be validated here
        # so resume failures are reported during connection.
        if self._session_id:
            path = (
                f"/api/session/{quote(self._session_id, safe='')}"
                if self.version >= 2
                else f"/session/{quote(self._session_id, safe='')}"
            )
            session = await self._http("GET", path)
            session = self._unwrap(session)
            if not isinstance(session, dict) or not session.get("id"):
                raise RuntimeError("OpenCode did not return a valid session")

            self._session_id = session["id"]
            await self._message_queue.put(Message(
                "thread_started", content={"session_id": self._session_id}
            ))

        asyncio.create_task(self._fetch_models())

        if prompt:
            await self.send_message(prompt)

    async def _start_server(self, env: Dict[str, str]) -> str:
        if not env.get("OPENCODE_SERVER_PASSWORD") and not env.get("OPENCODE_PASSWORD"):
            token = secrets.token_urlsafe(24)
            env["OPENCODE_SERVER_PASSWORD"] = token
            env["OPENCODE_PASSWORD"] = token
            self._server_password = token
        elif not self._server_password:
            self._server_password = (
                env.get("OPENCODE_SERVER_PASSWORD")
                or env.get("OPENCODE_PASSWORD")
            )

        cmd = [
            self.cli_path,
            "serve",
            "--hostname=127.0.0.1",
            "--port=0",
        ]
        LOG.info("Starting OpenCode server: %s", " ".join(cmd))

        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            # The process must not run before it belongs to the kill-on-close
            # Job.  Otherwise a fast .cmd launcher can create children which
            # escape the Job before AssignProcessToJobObject is called.
            self._job_handle = _create_win32_job_object()
            if not self._job_handle:
                raise RuntimeError("Failed to create the OpenCode Windows Job Object")
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | 0x00000004  # CREATE_SUSPENDED
            )
        else:
            # Sublime may terminate plugin_host without completing
            # plugin_unloaded(). Keep the server behind a pipe watchdog so
            # losing the parent still sends SIGTERM to the managed group.
            cmd = [
                "/bin/sh",
                "-c",
                (
                    'exec 3<&0; "$@" </dev/null & child=$!; '
                    '(cat <&3 >/dev/null; kill -TERM -$$ 2>/dev/null) & watcher=$!; '
                    'wait "$child"; status=$?; '
                    'kill "$watcher" 2>/dev/null; wait "$watcher" 2>/dev/null; '
                    'exit "$status"'
                ),
                "termmate-opencode-wrapper",
                *cmd,
            ]
            kwargs["stdin"] = asyncio.subprocess.PIPE
            kwargs["start_new_session"] = True

        try:
            self._server_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.options.cwd,
                env=env,
                **kwargs,
            )
        except Exception:
            if sys.platform == "win32":
                _close_win32_handle(self._job_handle)
                self._job_handle = None
            raise

        if sys.platform == "win32":
            assigned = _assign_process_to_job_object(
                self._job_handle, self._server_process
            )
            resumed = assigned and _resume_win32_process(self._server_process)
            if not resumed:
                # Closing an assigned Job terminates its process tree.  If
                # assignment failed, explicitly terminate the still-suspended
                # root process as well.
                _close_win32_handle(self._job_handle)
                self._job_handle = None
                if (self._server_process
                        and self._server_process.returncode is None):
                    self._server_process.kill()
                    await self._server_process.wait()
                self._server_process = None
                detail = "assign process to" if not assigned else "resume process in"
                raise RuntimeError(
                    f"Failed to {detail} the OpenCode Windows Job Object"
                )

        try:
            url = await asyncio.wait_for(self._read_server_url(), timeout=10.0)
        except Exception:
            await self._terminate_server()
            raise

        self._server_log_task = asyncio.create_task(self._drain_server_output())
        LOG.info("OpenCode server ready at %s", url)
        return url.rstrip("/")

    async def _read_server_url(self) -> str:
        process = self._server_process
        if not process or not process.stdout:
            raise RuntimeError("OpenCode server stdout is unavailable")

        output: List[str] = []
        while True:
            raw = await process.stdout.readline()
            if not raw:
                code = await process.wait()
                detail = "".join(output[-50:]).strip()
                raise RuntimeError(
                    f"OpenCode server exited with code {code} before startup"
                    + (f": {detail}" if detail else "")
                )

            line = raw.decode("utf-8", errors="replace")
            output.append(line)
            pw_match = _SERVER_PASSWORD_RE.search(line)
            if pw_match:
                self._server_password = pw_match.group(1).strip()
            match = _SERVER_URL_RE.search(line)
            if match:
                if not line.lower().strip().startswith("opencode") and self.version == 0:
                    self.version = 2
                    self.version_str = "v2"
                    LOG.info("Detected OpenCode v2 banner: using v2 protocol")
                url = match.group(1)
                if not self._server_password:
                    try:
                        raw_next = await asyncio.wait_for(
                            process.stdout.readline(), timeout=0.5
                        )
                        if raw_next:
                            line_next = raw_next.decode("utf-8", errors="replace")
                            output.append(line_next)
                            pw_match = _SERVER_PASSWORD_RE.search(line_next)
                            if pw_match:
                                self._server_password = pw_match.group(1).strip()
                    except (asyncio.TimeoutError, Exception):
                        pass
                return url

    async def _drain_server_output(self) -> None:
        process = self._server_process
        if not process or not process.stdout:
            return
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                if self.options.debug_agent_message:
                    LOG.debug(
                        "opencode server: %s",
                        raw.decode("utf-8", errors="replace").rstrip(),
                    )
        except asyncio.CancelledError:
            pass

    def _auth_headers(self, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        source = env or {**os.environ, **self.options.extra_env}
        password = (
            self._server_password
            or source.get("OPENCODE_SERVER_PASSWORD")
            or source.get("OPENCODE_PASSWORD")
            or find_opencode_v2_password()
        )
        if not password:
            return {}
        username = source.get("OPENCODE_SERVER_USERNAME", "opencode")
        token = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _build_url(
        self,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        include_directory: bool = True,
    ) -> str:
        if not self.server_url:
            raise RuntimeError("OpenCode server is not available")

        params = dict(query or {})
        if include_directory and self.options.cwd:
            params.setdefault("directory", self.options.cwd)
        suffix = "?" + urlencode(params, doseq=True) if params else ""
        return self.server_url + path + suffix

    async def _http(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        include_directory: bool = True,
        timeout: float = 30.0,
    ) -> Any:
        loop = self._loop or asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self._http_sync(
                method, path, body, query, include_directory, timeout
            ),
        )

    def _http_sync(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]],
        query: Optional[Dict[str, Any]],
        include_directory: bool,
        timeout: float,
    ) -> Any:
        url = self._build_url(path, query, include_directory)
        headers = {"Accept": "application/json", **self._auth_headers()}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                content_type = (
                    response.headers.get("Content-Type", "") if response.headers else ""
                )
                raw = response.read()
                if not raw:
                    return None
                if "text/html" in content_type:
                    status = getattr(
                        response, "status", getattr(response, "code", 200)
                    )
                    raise OpenCodeHTTPError(
                        method,
                        url,
                        status,
                        "OpenCode server returned HTML instead of JSON: "
                        "route may not exist or version mismatch",
                    )
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as err:
                    raise OpenCodeHTTPError(
                        method, url, None, f"Failed to parse JSON response: {err}"
                    ) from err
        except HTTPError as error:
            content_type = (
                error.headers.get("Content-Type", "") if error.headers else ""
            )
            raw_err = error.read()
            detail = raw_err.decode("utf-8", errors="replace")
            if (
                "text/html" in content_type
                or detail.strip().startswith("<!DOCTYPE")
                or detail.strip().startswith("<html")
            ):
                detail = (
                    f"HTTP {error.code} (HTML response): "
                    "route may not exist on this OpenCode version"
                )
            else:
                try:
                    parsed = json.loads(detail)
                    detail = self._error_text(parsed)
                except (TypeError, ValueError):
                    pass
            raise OpenCodeHTTPError(method, url, error.code, detail) from error
        except URLError as error:
            raise OpenCodeHTTPError(method, url, None, str(error.reason)) from error

    @staticmethod
    def _unwrap(value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("error"):
                raise RuntimeError(OpenCodeAgent._error_text(value["error"]))
            if "data" in value and set(value).issubset({
                "data", "error", "cursor", "total", "next_cursor", "location", "version"
            }):
                return value.get("data")
        return value

    @staticmethod
    def _error_text(error: Any) -> str:
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
            return str(error.get("message") or error.get("name") or error)
        return str(error)

    def _start_sse_thread(self, env: Dict[str, str]) -> None:
        self._sse_stop.clear()
        self._sse_ready.clear()
        self._sse_thread = threading.Thread(
            target=self._sse_worker,
            args=(env,),
            name="TermMate-OpenCode-SSE",
            daemon=True,
        )
        self._sse_thread.start()

    def _sse_worker(self, env: Dict[str, str]) -> None:
        reconnect_delay = 0.25
        # SSE stream endpoint: v2 connects to /api/event; v1 connects to /event
        path = "/api/event" if self.version >= 2 else "/event"
        while not self._sse_stop.is_set():
            url = self._build_url(path)
            headers = {
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
                **self._auth_headers(env),
            }
            request = Request(url, headers=headers, method="GET")
            try:
                response = urlopen(request, timeout=60.0)
                with self._sse_response_lock:
                    self._sse_response = response
                self._sse_ready.set()
                reconnect_delay = 0.25
                self._consume_sse(response)
            except Exception as error:
                if not self._sse_stop.is_set():
                    LOG.warning("OpenCode SSE disconnected: %s", error)
            finally:
                with self._sse_response_lock:
                    self._sse_response = None

            if not self._sse_stop.wait(reconnect_delay):
                reconnect_delay = min(reconnect_delay * 2, 5.0)

    def _consume_sse(self, response: Any) -> None:
        data_lines: List[str] = []
        while not self._sse_stop.is_set():
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line == "":
                if data_lines:
                    self._submit_sse_data("\n".join(data_lines))
                    data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        if data_lines:
            self._submit_sse_data("\n".join(data_lines))

    def _submit_sse_data(self, raw: str) -> None:
        if self.options.debug_agent_message:
            LOG.info("opencode msg: %s", raw)
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            LOG.debug("Ignoring invalid OpenCode SSE data: %s", raw[:200])
            return
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._event_queue.put_nowait, event)

    async def _process_events(self) -> None:
        try:
            while self._is_connected:
                event = await self._event_queue.get()
                try:
                    await self._dispatch_event(event)
                except Exception as error:
                    LOG.error(
                        "Failed to process OpenCode event: %s", error, exc_info=True
                    )
                    await self._message_queue.put(
                        Message(MessageType.ERROR.value, content=str(error))
                    )
        except asyncio.CancelledError:
            pass

    def _event_payload(self, event: Dict[str, Any]) -> Dict[str, Any]:
        payload = event.get("payload", event)
        return payload if isinstance(payload, dict) else {}

    def _belongs_to_session(self, properties: Dict[str, Any]) -> bool:
        session_id = properties.get("sessionID") or properties.get("sessionId")
        if not session_id:
            for val in properties.values():
                if isinstance(val, dict):
                    session_id = val.get("sessionID") or val.get("sessionId")
                    if session_id:
                        break
        return not session_id or session_id in (self._session_id, "global")

    @staticmethod
    def _message_id(properties: Dict[str, Any]) -> str:
        part = properties.get("part")
        if isinstance(part, dict):
            return str(part.get("messageID") or part.get("messageId") or "")
        return str(properties.get("messageID") or properties.get("messageId") or "")

    def _is_user_part(self, properties: Dict[str, Any]) -> bool:
        message_id = self._message_id(properties)
        return bool(
            message_id
            and (
                message_id in self._user_message_ids
                or self._message_roles.get(message_id) == "user"
            )
        )

    async def _dispatch_event(self, event: Dict[str, Any]) -> None:
        payload = self._event_payload(event)
        event_type = payload.get("type", "")
        properties = payload.get("data")
        if properties is None:
            properties = payload.get("properties", {})
        if not isinstance(properties, dict):
            LOG.warning(
                "OpenCode event has invalid properties: type=%s payload=%r",
                event_type,
                payload,
            )
            return

        if not self._belongs_to_session(properties):
            return

        # =================================================================
        # 1. OpenCode v2 Protocol Events (received on /api/event)
        # =================================================================

        # [v2] Assistant streaming text delta
        if event_type == "session.text.delta":
            delta = properties.get("delta", "")
            msg_id = str(
                properties.get("assistantMessageID") or properties.get("partID") or ""
            )
            if delta:
                await self._emit_text_delta(delta, msg_id)
            return

        # [v2] Assistant streaming reasoning / thought delta
        if event_type == "session.reasoning.delta":
            delta = properties.get("delta", "")
            msg_id = str(
                properties.get("assistantMessageID") or properties.get("partID") or ""
            )
            if delta:
                await self._message_queue.put(Message(
                    MessageType.THINKING.value,
                    content=delta,
                    msg_id=msg_id,
                ))
            return

        # [v2] Prompt inbox lifecycle (resolves user message id)
        if event_type in ("session.inbox.enqueued", "session.inbox.delivered"):
            message_id = str(
                properties.get("inboxID")
                or (properties.get("item") or {}).get("id")
                or ""
            )
            if message_id:
                self._user_message_ids.add(message_id)
                if self._awaiting_user_message_id:
                    self._awaiting_user_message_id = False
                    await self._message_queue.put(Message(
                        "user_message_id",
                        content={"message_id": message_id},
                        msg_id=message_id,
                    ))
            return

        # [v2] Tool invocation and progress tracking
        if event_type in (
            "session.tool.input.started",
            "session.tool.called",
            "session.tool.progress",
        ):
            tool_id = str(properties.get("id") or "")
            if tool_id:
                self._tool_calls.setdefault(tool_id, {}).update(properties)
            return

        # [v2] Tool terminal completion (success)
        if event_type == "session.tool.success":
            await self._handle_tool_terminal_v2(properties, "completed")
            return

        # [v2] Tool terminal completion (failure)
        if event_type == "session.tool.failed":
            await self._handle_tool_terminal_v2(properties, "error")
            return

        # [v2] Turn execution completed (succeeded or interrupted)
        if event_type in ("session.execution.succeeded", "session.execution.interrupted"):
            if self._turn_active:
                LOG.info(
                    "OpenCode conversation finished [session_id=%s, type=%s]",
                    self._session_id,
                    event_type,
                )
            await self._finish_turn()
            return

        # [v2] Turn execution failed with error
        if event_type == "session.execution.failed":
            error = properties.get("error") or properties
            error_text = self._error_text(error)
            if self._turn_active:
                LOG.error(
                    "OpenCode conversation execution failed [session_id=%s, error=%r]",
                    self._session_id,
                    error_text,
                )
            await self._message_queue.put(Message(
                MessageType.ERROR.value, content=error_text
            ))
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()
            return

        # =================================================================
        # 2. OpenCode v1 Protocol Events (received on /event)
        # =================================================================

        # [v1] Streaming part delta (text or reasoning)
        if event_type == "message.part.delta":
            if self._is_user_part(properties):
                return
            if properties.get("field") in (None, "text"):
                part_id = str(properties.get("partID") or "")
                delta = properties.get("delta", "")
                if delta:
                    self._text_cache[part_id] = self._text_cache.get(part_id, "") + delta
                    if self._part_types.get(part_id) == "reasoning":
                        await self._message_queue.put(Message(
                            MessageType.THINKING.value,
                            content=delta,
                            msg_id=part_id,
                        ))
                    else:
                        await self._emit_text_delta(delta, part_id)
            return

        # [v1] Part completed or updated (final text, reasoning, tool execution)
        if event_type == "message.part.updated":
            if self._is_user_part(properties):
                return
            await self._handle_part_updated(properties)
            return

        # [v1] Message metadata updated (user message role, user message id)
        if event_type == "message.updated":
            info = properties.get("info", {})
            if isinstance(info, dict):
                message_id = str(info.get("id") or "")
                role = info.get("role")
                if message_id and role:
                    self._message_roles[message_id] = str(role)
                if message_id and role == "user":
                    self._user_message_ids.add(message_id)
                    if self._awaiting_user_message_id:
                        self._awaiting_user_message_id = False
                        await self._message_queue.put(Message(
                            "user_message_id",
                            content={"message_id": message_id},
                            msg_id=message_id,
                        ))
                if info.get("error"):
                    # chat presents only the turn-level session.error notification
                    LOG.debug(
                        "OpenCode message error "
                        "[session_id=%s, message_id=%s, error=%r]",
                        self._session_id,
                        message_id,
                        self._error_text(info["error"]),
                    )
            return

        # [v1] File diff generated during turn
        if event_type == "session.diff":
            await self._handle_session_diff(properties.get("diff", []))
            return

        # [v1] Turn error
        if event_type == "session.error":
            error = properties.get("error") or properties
            error_text = self._error_text(error)
            if self._turn_active:
                LOG.error(
                    "OpenCode conversation failed [session_id=%s, error=%r]",
                    self._session_id,
                    error_text,
                )
            await self._message_queue.put(Message(
                MessageType.ERROR.value, content=error_text
            ))
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()
            return

        # [v1] Turn completion (session.status(idle) or legacy session.idle)
        turn_idle = False
        if event_type == "session.status":
            self._uses_session_status = True
            status = properties.get("status", {})
            turn_idle = isinstance(status, dict) and status.get("type") == "idle"
        elif event_type == "session.idle":
            turn_idle = not self._uses_session_status

        if turn_idle:
            if self._turn_active:
                LOG.info(
                    "OpenCode conversation succeeded [session_id=%s]",
                    self._session_id,
                )
            await self._finish_turn()
            return

        # =================================================================
        # 3. Cross-Protocol Events (v1 & v2)
        # =================================================================

        # [v1 & v2] User tool permission requests
        if event_type in ("permission.asked", "permission.updated"):
            await self._handle_permission(properties)
            return

    async def _finish_turn(self) -> None:
        # _finish_turn can be triggered either by the SSE event stream
        # (session.execution.succeeded / session.execution.interrupted) or by the interrupt
        # fallback timeout task.
        if not self._turn_active:
            return
        if self._awaiting_user_message_id:
            self._awaiting_user_message_id = False
        self._turn_active = False
        if self._turn_plan_mode and self._plan_text:
            await self._message_queue.put(Message(
                MessageType.PLAN_DELTA.value, content=self._plan_text
            ))
            self._plan_text = ""
        self._turn_plan_mode = False
        self._turn_done.set()
        await self._message_queue.put(Message(MessageType.STOP.value))

    async def _handle_part_updated(self, properties: Dict[str, Any]) -> None:
        """[OpenCode v1] Process part-based updates from message.part.updated."""
        part = properties.get("part", {})
        if not isinstance(part, dict):
            return
        part_type = part.get("type")
        part_id = str(part.get("id") or properties.get("partID") or "")
        if part_id and part_type:
            self._part_types[part_id] = part_type

        if part_type == "text":
            if part.get("ignored"):
                return
            full_text = part.get("text", "")
            previous = self._text_cache.get(part_id, "")
            if full_text:
                delta = full_text[len(previous):] if full_text.startswith(previous) else full_text
            else:
                delta = properties.get("delta") or ""
            self._text_cache[part_id] = full_text or previous + delta
            await self._emit_text_delta(delta or "", part_id)
            return

        if part_type == "reasoning":
            text = part.get("text", "")
            previous = self._text_cache.get(part_id, "")
            if text:
                delta = text[len(previous):] if text.startswith(previous) else text
            else:
                delta = properties.get("delta") or ""
            self._text_cache[part_id] = text or previous + delta
            if delta:
                await self._message_queue.put(Message(
                    MessageType.THINKING.value, content=delta, msg_id=part_id
                ))
            return

        if part_type == "tool":
            state = part.get("state", {})
            if not isinstance(state, dict):
                return
            status = state.get("status")
            # Running and pending tool states are intentionally not surfaced.
            if status not in ("completed", "error") or part_id in self._terminal_tools:
                return
            self._terminal_tools.add(part_id)
            await self._emit_completed_tool(part, state)

    async def _emit_text_delta(self, delta: str, msg_id: Optional[str]) -> None:
        if not delta:
            return
        if self._turn_plan_mode:
            self._plan_text += delta
        else:
            await self._message_queue.put(Message(
                MessageType.TEXT.value, content=delta, msg_id=msg_id
            ))

    async def _emit_completed_tool(
        self, part: Dict[str, Any], state: Dict[str, Any]
    ) -> None:
        """[OpenCode v1] Convert completed tool part into MessageType.TOOL_USE."""
        raw_name = str(part.get("tool") or "tool")
        normalized = raw_name.lower()
        input_data = state.get("input", {})
        if not isinstance(input_data, dict):
            input_data = {"value": input_data}

        if normalized in ("bash", "shell", "command"):
            content = {
                "name": "command_execution",
                "command": input_data.get("command") or input_data.get("cmd") or state.get("title"),
                "output": state.get("output") or state.get("error"),
                "status": state.get("status"),
            }
        else:
            content = {
                "name": raw_name,
                "input": input_data,
                "title": state.get("title"),
                "output": state.get("output"),
                "error": state.get("error"),
                "metadata": state.get("metadata", {}),
                "status": state.get("status"),
            }
        await self._message_queue.put(Message(
            MessageType.TOOL_USE.value, content=content, msg_id=part.get("id")
        ))

    async def _handle_tool_terminal_v2(
        self, properties: Dict[str, Any], status: str
    ) -> None:
        """[OpenCode v2] Handle terminal tool state from session.tool.success/failed."""
        tool_id = str(properties.get("id") or "")
        if tool_id in self._terminal_tools:
            return
        if tool_id:
            self._terminal_tools.add(tool_id)

        cached = self._tool_calls.get(tool_id, {})
        raw_name = str(properties.get("name") or cached.get("name") or "tool")
        normalized = raw_name.lower()
        input_data = cached.get("input") or properties.get("input", {})
        if not isinstance(input_data, dict):
            input_data = {"value": input_data}

        output_text = properties.get("output")
        if output_text is None and isinstance(properties.get("content"), list):
            output_parts = []
            for item in properties["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    output_parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    output_parts.append(item)
            output_text = "\n".join(output_parts) if output_parts else None

        error_data = properties.get("error")
        error_text = self._error_text(error_data) if error_data else None

        metadata = {
            **(cached.get("metadata") or {}),
            **(properties.get("metadata") or {}),
        }
        files = metadata.get("files")
        if isinstance(files, list) and files:
            first = files[0]
            if isinstance(first, dict) and first.get("patch"):
                metadata.setdefault("diff", first["patch"])

        if normalized in ("bash", "shell", "command"):
            content = {
                "name": "command_execution",
                "command": (
                    input_data.get("command")
                    or input_data.get("cmd")
                    or properties.get("title")
                ),
                "output": output_text or error_text,
                "status": status,
            }
        else:
            content = {
                "name": raw_name,
                "input": input_data,
                "title": properties.get("title"),
                "output": output_text,
                "error": error_text,
                "metadata": metadata,
                "status": status,
            }
        await self._message_queue.put(Message(
            MessageType.TOOL_USE.value, content=content, msg_id=tool_id
        ))

    async def _handle_permission(self, properties: Dict[str, Any]) -> None:
        """[v1 & v2] Handle tool authorization permission requests."""
        # Older servers may wrap the request in a ``permission`` object.
        # Current servers use ``permission`` as the string permission kind
        # (for example "bash") on the request itself.  Treat it as a wrapper
        # only when the value is actually a mapping.
        nested = properties.get("permission")
        permission = nested if isinstance(nested, dict) else properties
        permission_id = str(
            permission.get("id")
            or permission.get("requestID")
            or permission.get("permissionID")
            or ""
        )
        if not permission_id or permission_id in self._seen_permissions:
            return
        self._seen_permissions.add(permission_id)

        session_id = str(
            permission.get("sessionID")
            or properties.get("sessionID")
            or self._session_id
            or ""
        )
        self._permission_sessions[permission_id] = session_id
        permission_type = str(
            permission.get("action")
            or permission.get("type")
            or permission.get("permission")
            or "tool"
        )
        tool_name = _TOOL_NAMES.get(permission_type.lower(), permission_type)
        input_data = dict(permission.get("metadata") or {})

        # Extract structured input from linked tool call or permission resources
        source_id = str((permission.get("source") or {}).get("id") or "")
        cached_input = self._tool_calls.get(source_id, {}).get("input")
        if isinstance(cached_input, dict):
            input_data.update(cached_input)
        elif permission.get("resources"):
            res = permission["resources"]
            val = "\n".join(str(r) for r in res) if isinstance(res, list) else str(res)
            is_cmd = permission_type.lower() in ("shell", "bash", "command")
            input_data.setdefault("command" if is_cmd else "file_path", val)

        patterns = permission.get("patterns") or permission.get("pattern")
        input_data.update({
            "title": permission.get("title", ""),
            "pattern": patterns,
            "permission_type": permission_type,
        })

        await self._message_queue.put(Message(
            "control_request",
            content={
                "request_id": permission_id,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": tool_name,
                    "input": input_data,
                },
            },
        ))

    async def send_approval_response(
        self, permission_id: str, response_data: Dict[str, Any]
    ) -> None:
        session_id = self._permission_sessions.pop(
            permission_id, self._session_id or ""
        )
        behavior = response_data.get("behavior", "deny")
        response = "once" if behavior == "allow" else "reject"
        body = {"reply": response, "decision": response}
        if response == "reject" and response_data.get("message"):
            body["message"] = response_data["message"]

        if self.version >= 2:
            try:
                sid = quote(session_id, safe="")
                pid = quote(str(permission_id), safe="")
                reply_body: Dict[str, Any] = {"decision": response}
                if "message" in body:
                    reply_body["message"] = body["message"]
                await self._http(
                    "POST",
                    f"/api/session/{sid}/permission/{pid}/reply",
                    body=reply_body,
                )
                return
            except OpenCodeHTTPError as error:
                if error.status not in (404, 405):
                    raise

        v1_body: Dict[str, Any] = {"reply": response}
        if "message" in body:
            v1_body["message"] = body["message"]
        try:
            await self._http(
                "POST",
                f"/permission/{quote(str(permission_id), safe='')}/reply",
                body=v1_body,
            )
        except OpenCodeHTTPError as error:
            if error.status not in (404, 405):
                raise
            # Compatibility with pre-v1 permission routes.
            await self._http(
                "POST",
                "/session/{}/permissions/{}".format(
                    quote(session_id, safe=""), quote(str(permission_id), safe="")
                ),
                body={"response": response},
            )

    async def _handle_session_diff(self, diffs: Any) -> None:
        """[OpenCode v1] Map session.diff file modifications to unified diff cache."""
        if not isinstance(diffs, list):
            return
        changes = []
        for item in diffs:
            if not isinstance(item, dict) or not item.get("file"):
                continue
            path = item["file"]
            before = item.get("before", "")
            after = item.get("after", "")
            signature = (before, after)
            if self._diff_cache.get(path) == signature:
                continue
            self._diff_cache[path] = signature
            diff_text = "".join(difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=path,
                tofile=path,
            ))
            changes.append({
                "path": path,
                "oldText": before,
                "newText": after,
                "diff": diff_text,
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "kind": {"type": "update"},
            })
        if changes:
            await self._message_queue.put(Message(
                MessageType.TOOL_USE.value,
                content={
                    "name": "fileChange",
                    "changes": changes,
                    "filenames": [os.path.basename(c["path"]) for c in changes],
                    "status": "completed",
                },
            ))

    async def _fetch_models(self) -> None:
        try:
            if self.version >= 2:
                raw_models = self._unwrap(await self._http("GET", "/api/model"))
                models = []
                items = (
                    raw_models
                    if isinstance(raw_models, list)
                    else (
                        raw_models.get("data")
                        if isinstance(raw_models, dict)
                        else []
                    )
                )
                for item in items:
                    if not isinstance(item, dict) or item.get("enabled") is False:
                        continue
                    provider_id = item.get("providerID")
                    model_id = item.get("id") or item.get("modelID")
                    if not provider_id or not model_id:
                        continue
                    name = item.get("name") or model_id
                    models.append({
                        "displayName": name,
                        "description": provider_id,
                        "value": f"{provider_id}/{model_id}",
                    })
                if models:
                    self.available_models = models
                    await self._message_queue.put(Message(
                        "models_update", content={"models": models}
                    ))
                return

            result = self._unwrap(await self._http("GET", "/provider"))
            if not isinstance(result, dict):
                return
            connected = set(result.get("connected") or [])
            providers = result.get("all") or result.get("providers") or []
            models = []
            for provider in providers:
                if not isinstance(provider, dict):
                    continue
                provider_id = provider.get("id")
                if not provider_id or (connected and provider_id not in connected):
                    continue
                provider_name = provider.get("name") or provider_id
                raw_models = provider.get("models") or {}
                if isinstance(raw_models, dict):
                    iterator = raw_models.items()
                elif isinstance(raw_models, list):
                    iterator = ((m.get("id"), m) for m in raw_models if isinstance(m, dict))
                else:
                    continue
                for model_id, model in iterator:
                    if not model_id:
                        continue
                    model = model if isinstance(model, dict) else {}
                    models.append({
                        "displayName": model.get("name") or model_id,
                        "description": provider_name,
                        "value": f"{provider_id}/{model_id}",
                    })
            if models:
                self.available_models = models
                await self._message_queue.put(Message(
                    "models_update", content={"models": models}
                ))
        except Exception as error:
            LOG.warning("Failed to fetch OpenCode models: %s", error)

    def set_model(self, model: str) -> None:
        self.options.model = model

    async def set_plan_mode(self, plan_mode: bool) -> None:
        self.plan_mode = plan_mode
        if not plan_mode:
            self._plan_text = ""

    def _model_payload(self) -> Optional[Dict[str, str]]:
        model = self.options.model
        if not model or "/" not in model:
            return None
        provider_id, model_id = model.split("/", 1)
        if not provider_id or not model_id:
            return None
        return {"providerID": provider_id, "modelID": model_id}

    def _model_ref_v2(self) -> Optional[Dict[str, str]]:
        model = self.options.model
        if not model or "/" not in model:
            return None
        provider_id, model_id = model.split("/", 1)
        if not provider_id or not model_id:
            return None
        return {"providerID": provider_id, "id": model_id}

    def _session_permissions_v2(self) -> List[Dict[str, str]]:
        rules: List[Dict[str, str]] = []
        seen: set = set()
        for tool in self.options.disallowed_tools:
            key = self._permission_key(tool)
            if key and key not in seen:
                seen.add(key)
                action = "shell" if key == "bash" else key
                rules.append({
                    "action": action,
                    "resource": "*",
                    "effect": "deny",
                })
        return rules

    def _session_create_body_v2(self) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if self.options.cwd:
            body["location"] = {"directory": os.path.abspath(self.options.cwd)}
        permissions = self._session_permissions_v2()
        if permissions:
            body["permissions"] = permissions
        return body

    async def _ensure_session(self) -> str:
        """Create the server-side session on demand for the first prompt."""
        if self._session_id:
            return self._session_id

        if self.version >= 2:
            # [OpenCode v2] Create session with workspace location & tool permissions
            body = self._session_create_body_v2()
            session = await self._http("POST", "/api/session", body=body)
        else:
            # [OpenCode v1] Create simple session at root /session
            session = await self._http("POST", "/session", body={})

        session = self._unwrap(session)
        if not isinstance(session, dict) or not session.get("id"):
            raise RuntimeError("OpenCode did not return a valid session")

        self._session_id = session["id"]
        await self._message_queue.put(Message(
            "thread_started", content={"session_id": self._session_id}
        ))
        return self._session_id

    async def send_message(
        self,
        content: str,
        parent_tool_use_id: Optional[str] = None,
        proceed_plan: bool = False,
    ) -> None:
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        # prompt_async returns as soon as OpenCode accepts the prompt, while
        # the session's generation loop keeps running.  Submitting another
        # prompt before session.idle can persist the user message without
        # starting a new generation, so serialize turns at the adapter edge.
        while self._turn_active:
            await self._turn_done.wait()
        if not self._is_connected:
            raise RuntimeError("Client disconnected while waiting for the previous turn.")

        session_id = await self._ensure_session()
        sid = quote(session_id, safe="")
        agent_name = "build" if proceed_plan or not self.plan_mode else "plan"

        self._plan_text = ""
        self._turn_plan_mode = agent_name == "plan"
        self._turn_active = True
        self._turn_done.clear()
        self._awaiting_user_message_id = True
        await self._message_queue.put(Message(
            "turn_started",
            content={},
        ))
        try:
            if self.version >= 2:
                # [OpenCode v2] Configure agent mode and model, then submit prompt to /api/session
                await self._http(
                    "POST",
                    f"/api/session/{sid}/agent",
                    body={"agent": agent_name},
                )
                model_ref = self._model_ref_v2()
                if model_ref:
                    await self._http(
                        "POST",
                        f"/api/session/{sid}/model",
                        body={"model": model_ref},
                    )
                await self._http(
                    "POST",
                    f"/api/session/{sid}/prompt",
                    body={"text": content},
                )
            else:
                # [OpenCode v1] Single prompt_async request with text parts, model, and tools
                body: Dict[str, Any] = {
                    "agent": agent_name,
                    "parts": [{"type": "text", "text": content}],
                }
                model = self._model_payload()
                if model:
                    body["model"] = model
                if self.options.system_prompt:
                    body["system"] = self.options.system_prompt

                disabled = {}
                for tool in self.options.disallowed_tools:
                    key = self._permission_key(tool)
                    if key:
                        disabled[key] = False
                if disabled:
                    body["tools"] = disabled

                await self._http(
                    "POST",
                    f"/session/{sid}/prompt_async",
                    body=body,
                )
        except Exception as error:
            self._turn_active = False
            self._turn_plan_mode = False
            self._awaiting_user_message_id = False
            self._turn_done.set()
            await self._message_queue.put(
                Message(MessageType.ERROR.value, content=str(error))
            )
            raise

    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        await self.send_message(text, proceed_plan=proceed_plan)

    async def interrupt(self) -> None:
        if not (self._is_connected and self._session_id):
            return

        sid = quote(self._session_id, safe="")
        path = (
            f"/api/session/{sid}/interrupt"
            if self.version >= 2
            else f"/session/{sid}/abort"
        )
        try:
            await self._http("POST", path, body={}, timeout=5.0)
        except Exception as e:
            LOG.warning(f"OpenCode interrupt HTTP failed ({e}); forcing turn finish")
            await self._finish_turn()
            return

        async def _fallback():
            await asyncio.sleep(5.0)
            await self._finish_turn()

        asyncio.create_task(_fallback())

    async def new_session(self) -> str:
        """Create and switch to a fresh session without reconnecting the client."""
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")

        if self._turn_active:
            await self.interrupt()

        if self.version >= 2:
            body = self._session_create_body_v2()
            session = await self._http("POST", "/api/session", body=body)
        else:
            session = await self._http("POST", "/session", body={})

        session = self._unwrap(session)
        if not isinstance(session, dict) or not session.get("id"):
            raise RuntimeError("OpenCode did not return a valid session")

        # Switch the event filter before clearing the old session's caches so
        # any late SSE events from it are ignored by _belongs_to_session().
        self._session_id = session["id"]
        self.options.session_id = None

        self._turn_active = False
        self._turn_plan_mode = False
        self._turn_done.set()
        self._uses_session_status = False
        self._awaiting_user_message_id = False
        self._text_cache.clear()
        self._part_types.clear()
        self._message_roles.clear()
        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._tool_calls.clear()
        self._permission_sessions.clear()
        self._seen_permissions.clear()
        self._diff_cache.clear()
        self._plan_text = ""

        await self._message_queue.put(Message(
            "thread_started", content={"session_id": self._session_id}
        ))
        return self._session_id

    async def rewind(self, message_id: str) -> Optional[str]:
        if not self._is_connected or not self._session_id:
            raise RuntimeError("Client is not connected or has no active session")
        sid = quote(self._session_id, safe="")
        if self.version >= 2:
            # [OpenCode v2] Two-phase revert: stage target message, then commit revert
            try:
                await self._http(
                    "POST",
                    f"/api/session/{sid}/revert/stage",
                    body={"messageID": message_id},
                )
            except OpenCodeHTTPError as err:
                if err.status == 500:
                    raise RuntimeError(
                        "OpenCode snapshot file restore failed (HTTP 500). "
                        "OpenCode requires a newer version of Git."
                    ) from err
                raise
            await self._http(
                "POST",
                f"/api/session/{sid}/revert/commit",
                body={},
            )
        else:
            # [OpenCode v1] Single-phase revert endpoint
            await self._http(
                "POST",
                f"/session/{sid}/revert",
                body={"messageID": message_id},
            )
        self._turn_active = False
        self._turn_plan_mode = False
        self._awaiting_user_message_id = False
        self._turn_done.set()
        self._text_cache.clear()
        self._part_types.clear()
        self._message_roles.clear()
        self._user_message_ids.clear()
        self._terminal_tools.clear()
        self._tool_calls.clear()
        self._diff_cache.clear()
        return self._session_id

    async def receive_messages(self) -> AsyncIterator[Message]:
        if not self._is_connected:
            raise RuntimeError("Client is not connected. Call connect() first.")
        while self._is_connected:
            try:
                yield await asyncio.wait_for(self._message_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if self._server_process and self._server_process.returncode is not None:
                    break

    async def _terminate_server(self) -> None:
        process = self._server_process
        if sys.platform == "win32" and self._job_handle:
            # KILL_ON_JOB_CLOSE is synchronous at the kernel boundary and does
            # not depend on the Sublime background event loop staying alive.
            _close_win32_handle(self._job_handle)
            self._job_handle = None

        if process and process.returncode is None:
            if sys.platform == "win32":
                try:
                    exit_code = await asyncio.wait_for(process.wait(), timeout=2.0)
                    LOG.info(
                        "OpenCode server process exited: pid=%s returncode=%s",
                        process.pid,
                        exit_code,
                    )
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timed out waiting for OpenCode server process to exit; "
                        "killing: pid=%s",
                        process.pid,
                    )
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    await killer.wait()
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    exit_code = await asyncio.wait_for(process.wait(), timeout=3.0)
                    LOG.info(
                        "OpenCode server process exited: pid=%s returncode=%s",
                        process.pid,
                        exit_code,
                    )
                except asyncio.TimeoutError:
                    LOG.warning(
                        "Timed out waiting for OpenCode server process to exit; "
                        "killing: pid=%s",
                        process.pid,
                    )
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()

        self._server_process = None

    def terminate_server_now(self) -> None:
        """Synchronously signal the managed server during plugin unload."""
        if self.version >= 2:
            stop_sublime_opencode_server(self.cli_path)
            return
        process = self._server_process
        try:
            if sys.platform == "win32":
                closed_job = False
                if self._job_handle:
                    closed_job = _close_win32_handle(self._job_handle)
                    self._job_handle = None
                if (not closed_job and process
                        and process.returncode is None):
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=2.0,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                return
            if not process or process.returncode is not None:
                return
            os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            pass

    async def disconnect(self) -> None:
        if self._server_process is not None:
            set_active_opencode_server(None)
        _ACTIVE_CLIENTS.discard(self)
        if not _ACTIVE_CLIENTS and self.version >= 2:
            schedule_idle_shutdown_timer(self.cli_path)

        self._is_connected = False
        self._turn_active = False
        self._turn_plan_mode = False
        self._awaiting_user_message_id = False
        self._turn_done.set()

        self._sse_stop.set()
        with self._sse_response_lock:
            response = self._sse_response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

        if self._sse_thread and self._sse_thread.is_alive():
            loop = self._loop or asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self._sse_thread.join(timeout=2.0))
        self._sse_thread = None

        if self._event_task and not self._event_task.done():
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        self._event_task = None

        # Keep consuming stdout until the server exits.  In particular, the
        # Windows Proactor loop needs to observe EOF and finish its pipe-close
        # callbacks before the owning AgentThread closes the event loop.
        # Cancelling this task first leaves the subprocess transport to be
        # finalized after loop.close(), which produces "Event loop is closed"
        # warnings from BaseSubprocessTransport.__del__.
        server_log_task = self._server_log_task
        await self._terminate_server()
        if server_log_task:
            try:
                await asyncio.wait_for(server_log_task, timeout=2.0)
            except asyncio.TimeoutError:
                LOG.warning("Timed out draining OpenCode server output")
            except asyncio.CancelledError:
                pass
            except Exception as error:
                LOG.debug("OpenCode server output drain failed: %s", error)
        self._server_log_task = None

        # Let pipe connection_lost callbacks queued by EOF run while the event
        # loop is still alive.
        await asyncio.sleep(0)

        self._permission_sessions.clear()
        self._seen_permissions.clear()


async def query(
    prompt: str,
    options: Optional[AgentOptions] = None,
) -> AsyncIterator[Message]:
    """Run a one-shot OpenCode query."""
    client = OpenCodeAgent(options=options or AgentOptions())
    try:
        await client.connect(prompt=prompt)
        async for message in client.receive_messages():
            yield message
            if getattr(message, "type", None) == MessageType.STOP.value:
                break
    finally:
        await client.disconnect()


class _SyncOpenCodeServerV1:
    """Legacy synchronous ephemeral server helper for OpenCode v1."""

    def __init__(
        self,
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        cli_path: Optional[str] = None,
    ):
        self.cwd = cwd or os.getcwd()
        self.extra_env = extra_env or {}
        self.cli_path = cli_path
        self.url = ""
        self.password = ""
        self.version = 1
        self.process: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._job_handle: Optional[int] = None

    def __enter__(self):
        # 1. Check for any active in-process server
        active = get_active_opencode_server()
        if active and probe_opencode_service(active["url"], active.get("password")):
            self.url = active["url"]
            self.password = active.get("password") or ""
            LOG.debug("Reusing active in-process OpenCode server at %s", self.url)
            return self

        # 2. Spawn an ephemeral server process on a random port
        cli = find_opencode_cli(self.cli_path)
        if not cli:
            raise FileNotFoundError(
                f"OpenCode CLI not found: {self.cli_path or 'opencode'}"
            )

        env = os.environ.copy()
        env.update(self.extra_env)
        if not env.get("OPENCODE_SERVER_PASSWORD") and not env.get("OPENCODE_PASSWORD"):
            token = secrets.token_urlsafe(24)
            env["OPENCODE_SERVER_PASSWORD"] = token
            env["OPENCODE_PASSWORD"] = token
            self.password = token
        else:
            self.password = (
                env.get("OPENCODE_SERVER_PASSWORD")
                or env.get("OPENCODE_PASSWORD")
                or ""
            )

        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            self._job_handle = _create_win32_job_object()
            if not self._job_handle:
                raise RuntimeError("Failed to create the OpenCode Windows Job Object")
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | 0x00000004  # CREATE_SUSPENDED
            )
        else:
            kwargs["start_new_session"] = True

        try:
            self.process = subprocess.Popen(
                [cli, "serve", "--hostname=127.0.0.1", "--port=0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=env,
                **kwargs,
            )
        except Exception:
            if sys.platform == "win32":
                _close_win32_handle(self._job_handle)
                self._job_handle = None
            raise

        if sys.platform == "win32":
            assigned = _assign_process_to_job_object(self._job_handle, self.process)
            resumed = assigned and _resume_win32_process(self.process)
            if not resumed:
                _close_win32_handle(self._job_handle)
                self._job_handle = None
                if self.process and self.process.poll() is None:
                    self.process.kill()
                    self.process.wait(timeout=2.0)
                self.process = None
                detail = "assign process to" if not assigned else "resume process in"
                raise RuntimeError(
                    f"Failed to {detail} the OpenCode Windows Job Object"
                )
        lines: thread_queue.Queue = thread_queue.Queue()

        def read_output():
            if not self.process or not self.process.stdout:
                return
            for line in self.process.stdout:
                lines.put(line)

        self._reader = threading.Thread(target=read_output, daemon=True)
        self._reader.start()

        deadline = time.monotonic() + 10.0
        output = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None and lines.empty():
                break
            try:
                line = lines.get(
                    timeout=min(0.2, max(0.01, deadline - time.monotonic()))
                )
            except thread_queue.Empty:
                continue
            output.append(line)
            pw_match = _SERVER_PASSWORD_RE.search(line)
            if pw_match:
                self.password = pw_match.group(1).strip()
            match = _SERVER_URL_RE.search(line)
            if match:
                self.url = match.group(1).rstrip("/")
                if not self.password:
                    try:
                        next_line = lines.get(timeout=0.5)
                        output.append(next_line)
                        pw_match = _SERVER_PASSWORD_RE.search(next_line)
                        if pw_match:
                            self.password = pw_match.group(1).strip()
                    except thread_queue.Empty:
                        pass
                return self

        self.close()
        raise RuntimeError(
            "Timed out starting OpenCode server"
            + (": " + "".join(output[-20:]).strip() if output else "")
        )

    def close(self):
        if sys.platform == "win32" and self._job_handle:
            _close_win32_handle(self._job_handle)
            self._job_handle = None
        if self.process and self.process.poll() is None:
            if sys.platform == "win32":
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=2.0,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
            else:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2.0)
        self.process = None

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class _SyncOpenCodeServer:
    """Synchronous server helper used by session picker and session info helpers.

    For OpenCode v2, reuses or starts the shared background service singleton.
    Falls back to _SyncOpenCodeServerV1 if the CLI is explicitly detected as v1.
    """

    def __init__(
        self,
        cwd: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        cli_path: Optional[str] = None,
    ):
        self.cwd = cwd or os.getcwd()
        self.extra_env = extra_env or {}
        self.cli_path = cli_path
        self.url = ""
        self.password = ""
        self.version = 2
        self._v1: Optional[_SyncOpenCodeServerV1] = None

    def __enter__(self):
        cli = find_opencode_cli(self.cli_path)
        if cli and detect_opencode_version(cli) == 1:
            self._v1 = _SyncOpenCodeServerV1(self.cwd, self.extra_env, cli)
            res = self._v1.__enter__()
            self.url = res.url
            self.password = res.password
            self.version = 1
            return self

        cancel_idle_shutdown_timer()
        server_info = get_sublime_opencode_server(self.cli_path, self.extra_env)
        if server_info:
            self.url = server_info["url"]
            self.password = server_info.get("password") or ""
            self.version = 2
            LOG.debug("Reusing Sublime OpenCode server at %s", self.url)
            return self

        raise RuntimeError("Failed to connect to OpenCode server")

    def __exit__(self, exc_type, exc_value, traceback):
        if self._v1:
            return self._v1.__exit__(exc_type, exc_value, traceback)
        schedule_idle_shutdown_timer(self.cli_path)


def _sync_auth_headers(
    extra_env: Optional[Dict[str, str]], password: Optional[str] = None
) -> Dict[str, str]:
    source = {**os.environ, **(extra_env or {})}
    effective_password = (
        password
        or source.get("OPENCODE_SERVER_PASSWORD")
        or source.get("OPENCODE_PASSWORD")
        or find_opencode_v2_password()
    )
    if not effective_password:
        return {}
    username = source.get("OPENCODE_SERVER_USERNAME", "opencode")
    token = base64.b64encode(f"{username}:{effective_password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _sync_request(
    base_url: str,
    path: str,
    cwd: Optional[str],
    extra_env: Optional[Dict[str, str]],
    password: Optional[str] = None,
) -> Any:
    url = base_url.rstrip("/") + path
    if cwd:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urlencode({'directory': cwd})}"
    request = Request(
        url,
        headers={"Accept": "application/json", **_sync_auth_headers(extra_env, password)},
        method="GET",
    )
    with urlopen(request, timeout=15.0) as response:
        content_type = (
            response.headers.get("Content-Type", "") if response.headers else ""
        )
        if "text/html" in content_type:
            return None
        raw = response.read()
    value = json.loads(raw.decode("utf-8")) if raw else None
    return OpenCodeAgent._unwrap(value)


def list_opencode_sessions(
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cli_path: Optional[str] = None,
) -> list:
    """Return OpenCode sessions for a workspace, newest first."""
    try:
        with _SyncOpenCodeServer(cwd, extra_env, cli_path) as server:
            path = "/session" if getattr(server, "version", 2) == 1 else "/api/session"
            try:
                sessions = _sync_request(
                    server.url, path, cwd, extra_env, server.password
                )
            except Exception:
                sessions = None

            result = []
            for session in sessions if isinstance(sessions, list) else []:
                if not isinstance(session, dict) or not session.get("id"):
                    continue
                time_dict = (
                    session.get("time") if isinstance(session.get("time"), dict) else {}
                )
                updated = (
                    time_dict.get("updated")
                    or time_dict.get("created")
                    or session.get("mtime")
                    or 0
                )
                mtime = float(updated)
                if mtime > 100000000000:
                    mtime /= 1000.0
                result.append({
                    "session_id": session["id"],
                    "summary": session.get("title") or session["id"][:8],
                    "mtime": mtime,
                })
            return sorted(result, key=lambda item: item["mtime"], reverse=True)
    except Exception as error:
        LOG.warning("list_opencode_sessions failed: %s", error)
        return []


def get_opencode_session_info(
    session_id: str,
    cwd: Optional[str] = None,
    extra_env: Optional[Dict[str, str]] = None,
    cli_path: Optional[str] = None,
    history_limit: int = 0,
) -> Optional[dict]:
    """Return unified metadata and recent turns for an OpenCode session."""
    try:
        with _SyncOpenCodeServer(cwd, extra_env, cli_path) as server:
            sid = quote(session_id, safe="")
            session = None
            messages = None
            try:
                session = _sync_request(
                    server.url, f"/api/session/{sid}", cwd, extra_env, server.password
                )
                messages = _sync_request(
                    server.url, f"/api/session/{sid}/message", cwd, extra_env, server.password
                )
            except Exception:
                session = None
                messages = None

            if not isinstance(session, dict) or not session.get("id"):
                session = _sync_request(
                    server.url, f"/session/{sid}", cwd, extra_env, server.password
                )
                messages = _sync_request(
                    server.url, f"/session/{sid}/message", cwd, extra_env, server.password
                )

            if (
                isinstance(session, dict)
                and "data" in session
                and isinstance(session["data"], dict)
            ):
                session = session["data"]
            if not isinstance(session, dict) or not session.get("id"):
                return None

            if (
                isinstance(messages, dict)
                and "data" in messages
                and isinstance(messages["data"], list)
            ):
                messages = messages["data"]

            turns = []
            current = None
            entries = messages if isinstance(messages, list) else []

            # OpenCode v2 returns messages descending (newest first); v1 returns ascending.
            # Sort chronologically by created timestamp so turns accumulate in order.
            def _entry_time(e: Any) -> float:
                if not isinstance(e, dict):
                    return 0.0
                t = e.get("time")
                if isinstance(t, dict):
                    return float(t.get("created") or t.get("updated") or 0)
                if isinstance(t, (int, float)):
                    return float(t)
                return 0.0

            entries = sorted(entries, key=_entry_time)

            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                info = entry.get("info") or {}
                role = info.get("role") or entry.get("type")
                text = ""
                if entry.get("parts") and isinstance(entry["parts"], list):
                    text = "".join(
                        part.get("text", "")
                        for part in entry["parts"]
                        if isinstance(part, dict)
                        and part.get("type") == "text"
                        and not part.get("ignored")
                    ).strip()
                elif entry.get("content") and isinstance(entry["content"], list):
                    text = "\n\n".join(
                        part.get("text", "")
                        for part in entry["content"]
                        if isinstance(part, dict)
                        and part.get("type") == "text"
                        and part.get("text")
                    ).strip()
                elif isinstance(entry.get("text"), str):
                    text = entry["text"].strip()

                if role == "user" and text:
                    if current is not None:
                        turns.append(current)
                    current = {"prompt": text, "response": None}
                elif role == "assistant" and text and current is not None:
                    response = current["response"]
                    current["response"] = f"{response}\n\n{text}" if response else text

            if current is not None:
                turns.append(current)
            if history_limit > 0:
                turns = turns[-history_limit:]
            last_turn = turns[-1] if turns else {}

            time_dict = (
                session.get("time") if isinstance(session.get("time"), dict) else {}
            )
            updated = (
                time_dict.get("updated")
                or time_dict.get("created")
                or session.get("mtime")
                or 0
            )
            mtime = float(updated)
            if mtime > 100000000000:
                mtime /= 1000.0
            return {
                "summary": session.get("title"),
                "mtime": mtime,
                "turns": turns,
                "prompt": last_turn.get("prompt"),
                "response": last_turn.get("response"),
            }
    except Exception as error:
        LOG.warning("get_opencode_session_info failed: %s", error)
        return None
