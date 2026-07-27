import logging
import enum
import os
import shutil
import asyncio
import threading
import sublime
import sublime_plugin

from . import utils as plugin
from ..genfoundry import (
    ClaudeCodeAgent, CodexAgent, PiAgent, AgentOptions, AssistantMessage, TextBlock,
    PermissionResultAllow, PermissionResultDeny, list_sessions_for_cwd, list_codex_sessions, list_pi_sessions)
from ..genfoundry.claude_agent import get_claude_session_tail
from ..genfoundry.codex_agent import get_codex_session_info
from ..genfoundry.pi_agent import get_pi_session_tail
from .chatprocessor import ClaudeMessageProcessor, CodexMessageProcessor, PiMessageProcessor
from .chatpanel import LoadingAnimation, RewindConfirmPanel, StatusHint
from .artifact import FileChangesArtifact, DIFF_VIEW_PATH_KEY, diff_view_click
from .install import run_install, find_existing_cli, get_agent_list_items

def get_available_agents(settings):
    """Returns a list of available agents."""
    from .install import AGENT_FIND_FN
    return [agent for agent in AGENT_FIND_FN if find_existing_cli(agent, settings)]

# Constants for gutter highlights
PROMPT_HIGHLIGHT_KEY = "chatview_prompt_highlight"
PROMPT_HIGHLIGHT_SCOPE = "region.purplish"
PROMPT_HIGHLIGHT_ICON = "dot"
PROMPT_HIGHLIGHT_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.PERSISTENT


# logger by package name
LOG = logging.getLogger("TermMate")

CHAT_VIEW_FLAG = "chatview_chat"
CHAT_INPUT_START = "chatview_input_start"
CHAT_INPUT_ANCHOR = "chatview_input_anchor"
CHAT_WORKSPACE = "chatview_active_workspace"
CHAT_MODEL = "chatview_model"
CHAT_PLAN_MODE = "chatview_plan_mode"
CHAT_AGENT = "chatview_agent_provider"
CHAT_SESSION_ID = "chatview_session_id"
CHAT_VIEW_NAME = "Chat View"
PACKAGE_NAME = "TermMate"
PROMPT_PREFIX = "\n❯ "  # transcript prefix for submitted prompts; the live input line uses InputPromptMarker instead

# Global store for active ChatSession: window_id -> ChatSession
chatview_clients = {}


def set_input_start(view, pos):
    """Record the input start position (the newline preceding the input line).

    Besides the settings value, an anchor region is placed over that newline.
    Regions shift automatically with buffer modifications, so the position
    stays correct even when text before it changes
    """
    view.settings().set(CHAT_INPUT_START, pos)
    if pos + 1 <= view.size():
        view.add_regions(CHAT_INPUT_ANCHOR, [sublime.Region(pos, pos + 1)],
                         flags=sublime.HIDDEN | sublime.PERSISTENT)
    else:
        # No newline to anchor on yet; drop any stale anchor so it can't
        # shadow the settings value.
        view.erase_regions(CHAT_INPUT_ANCHOR)


def get_input_start(view, default=None):
    """Current input start position.

    The anchor region is the source of truth; the settings value is a
    fallback (restored views, older sessions) and is re-synced whenever the
    two drift apart."""
    if default is None:
        default = view.size()
    regions = view.get_regions(CHAT_INPUT_ANCHOR)
    if regions and not regions[0].empty():
        pos = regions[0].begin()
        if view.settings().get(CHAT_INPUT_START) != pos:
            view.settings().set(CHAT_INPUT_START, pos)
        return pos
    if not view.settings().has(CHAT_INPUT_START):
        return default
    pos = min(view.settings().get(CHAT_INPUT_START), view.size())
    # Re-anchor lazily (view restored from a session, or anchor lost)
    set_input_start(view, pos)
    return pos


def input_editable_start(view):
    """Start of the editable input text. The input start points at the newline
    that precedes the input line; the ❯ marker is a phantom, not buffer text."""
    return get_input_start(view, 0) + 1


class PlanMode(enum.Enum):
    FAST = "fast"
    PLANNING = "planning"


CHAT_APPROVE_MODE = "chatview_approve_mode"


class ApproveMode(enum.Enum):
    DEFAULT = "default"
    ALLOW_EDIT = "allow-edit"
    ACCEPT_ALL = "accept-all"


def plugin_loaded():
    """
    Called by Sublime Text when the plugin is loaded.
    """
    settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
    plugin.update_log_level(settings)
    # Defer scan to allow ST to finish restoring all scratch views
    sublime.set_timeout(_restore_chat_sessions, 500)


def plugin_unloaded():
    """
    Called by Sublime Text when the plugin is unloaded.
    Cleans up active ChatSessions.
    """
    for window_id, session in list(chatview_clients.items()):
        try:
            LOG.info(f"Stopping ChatView session for window {window_id} on unload")
            session.stop()
        except Exception as e:
            LOG.error(f"Failed to stop ChatView session on plugin unload: {e}")

    chatview_clients.clear()


def _restore_chat_sessions():
    """Scan all windows for orphaned chat views and reconnect their agents."""
    for window in sublime.windows():
        for view in window.views():
            if (view.settings().get(CHAT_VIEW_FLAG, False) and
                    window.id() not in chatview_clients):
                _reconnect_chat_view(view)


def _reconnect_chat_view(view):
    """
    Reconnect an existing chat view to a new ChatSession after a restart.
    Appends a reconnection notice, but does not redraw historical phantoms.
    """
    window = view.window()
    if not window:
        return
    window_id = window.id()
    if window_id in chatview_clients:
        return

    settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
    share_folders = settings.get("share_workspace_folders", False)

    cwd = get_best_dir(view)
    add_dirs = get_all_folders(view) if share_folders else []
    session_id = ChatSession.get_view_session_id(view)

    # Don't draw the NBSP fold terminator appended after the artifact list
    view.settings().set("draw_unicode_white_space", "none")

    session = ChatSession(window, view, cwd, add_dirs=add_dirs, session_id=session_id)
    chatview_clients[window_id] = session
    # Restore the model phantom at the existing CHAT_INPUT_START position
    session.model_phantom.update()
    view.run_command("term_chat_output_append", {"text": "\n\n[Reconnected after restart]\n"})
    LOG.info(f"Reconnected ChatView agent for window {window_id}, cwd={cwd}, add_dirs={add_dirs}, session_id={session_id}")


def get_all_folders(view):
    window = view.window()
    if window:
        return window.folders()
    return []


def get_best_dir(view):
    window = view.window()
    if window:
        # Check for explicitly set workspace
        custom_cwd = window.settings().get(CHAT_WORKSPACE)
        if custom_cwd and os.path.isdir(custom_cwd):
            return custom_cwd

        folders = get_all_folders(view)
        if folders:
            return folders[0]
    return ""


class AgentThread(threading.Thread):
    """
    Background thread to run the asyncio Claude Agent.
    """
    def __init__(self, cwd, on_message, cli_path=None, anthropic_config=None, add_dirs=None):
        super().__init__()
        self.cwd = cwd
        self.on_message = on_message
        self.cli_path = cli_path
        self.anthropic_config = anthropic_config or {}
        self.add_dirs = add_dirs or []
        self.loop = None
        self.agent = None
        self.input_queue = None
        self.running = True
        self.daemon = True

    def run(self):
        """Run the asyncio loop."""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.input_queue = asyncio.Queue()
            self.loop.run_until_complete(self._agent_loop())
        finally:
            if self.loop:
                # Drain pending callbacks and async generators so subprocess
                # transports are closed while the loop is still alive, preventing
                try:
                    self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                except Exception:
                    pass
                self.loop.close()

    async def _agent_loop(self):
        """Main async loop for the agent."""
        options = AgentOptions(
            cwd=self.cwd,
            add_dirs=self.add_dirs,
            cli_path=self.cli_path,
            model=self.anthropic_config.get("model"),
            can_use_tool=getattr(self, 'agent_options_callback', None),
            plan_mode=self.anthropic_config.get("plan_mode", False),
            allowed_tools=self.anthropic_config.get("allowed_tools"),
            disallowed_tools=self.anthropic_config.get("disallowed_tools"),
            approve_mode=self.anthropic_config.get("approve_mode"),
            session_id=self.anthropic_config.get("session_id"),
            extra_env=self.anthropic_config.get("env"),
            debug_agent_message=self.anthropic_config.get("debug_agent_message", False),
            enable_file_checkpoint=(
                self.anthropic_config.get("agent_provider", "claude") == "claude"
                and self.anthropic_config.get("enable_file_checkpoint", True)
            ),
        )

        agent_provider = self.anthropic_config.get("agent_provider", "claude")
        if agent_provider == "codex":
            AgentClass = CodexAgent
        elif agent_provider == "pi":
            AgentClass = PiAgent
        else:
            AgentClass = ClaudeCodeAgent

        try:
            async with AgentClass(options) as agent:
                self.agent = agent

                # Create tasks for reading inputs and handling agent messages
                input_task = asyncio.create_task(self._process_inputs())
                receive_task = asyncio.create_task(self._receive_messages())

                # Wait until we are stopped
                while self.running:
                    await asyncio.sleep(0.1)

                # Cleanup
                input_task.cancel()
                receive_task.cancel()
                try:
                    await input_task
                    await receive_task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            LOG.error(f"Agent error: {e}")
            error_msg = str(e)
            sublime.set_timeout(lambda: self.on_message(("error", error_msg)), 0)

    async def _process_inputs(self):
        """Read from input queue and send to agent."""
        while self.running:
            text = await self.input_queue.get()
            if text:
                await self.agent.send_message(text)

    async def _steer(self, text, proceed_plan=False):
        """Send steering message to agent."""
        if self.agent:
            await self.agent.steer(text, proceed_plan=proceed_plan)

    def steer(self, text, proceed_plan=False):
        """Proxy steer call through the loop."""
        if self.loop and self.running:
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._steer(text, proceed_plan=proceed_plan))
            )

    def rewind(self, user_message_id: str, on_done: callable = None) -> None:
        """Run the full rewind sequence on the live agent loop (rewind_files + fork).

        on_done(new_session_id) is called on the Sublime main thread when complete.
        on_done(None) is called if rewind is not supported or fails.
        """
        if not self.loop or not self.running or not self.agent:
            if on_done:
                sublime.set_timeout(lambda: on_done(None), 0)
            return

        async def _run():
            new_session_id = None
            if hasattr(self.agent, "rewind"):
                try:
                    new_session_id = await self.agent.rewind(user_message_id)
                except Exception as e:
                    LOG.error(f"[rewind] agent.rewind failed: {e}", exc_info=True)
            if on_done:
                sublime.set_timeout(lambda: on_done(new_session_id), 0)

        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(_run()))

    @property
    def session_id(self):
        """Get the session id of the current running agent."""
        if self.agent:
            sid = getattr(self.agent, "_session_id", None) or getattr(self.agent, "thread_id", None)
            if sid:
                return sid
        return self.anthropic_config.get("session_id")

    async def _receive_messages(self):
        """Read messages from agent and callback."""
        async for message in self.agent.receive_messages():
            if not self.running:
                break

            # Dispatch to main thread
            sublime.set_timeout(lambda m=message: self.on_message(m), 0)

    async def _reset_agent(self):
        """Disconnect and reconnect the agent to clear conversation context."""
        try:
            # Disconnect current session
            await self.agent.disconnect()
            LOG.info("Agent disconnected for reset")

            # Reconnect to start fresh session
            await self.agent.connect()
            LOG.info("Agent reconnected with fresh session")

            # Notify UI that reset is complete
            sublime.set_timeout(
                lambda: self.on_message(("reset_complete", "Session reset successfully")), 0
            )
        except Exception as e:
            LOG.error(f"Error resetting agent: {e}")
            sublime.set_timeout(
                lambda: self.on_message(("error", f"Failed to reset session: {str(e)}")), 0
            )

    async def _send_permission_response(self, request_id, response_data, is_extension_ui=False):
        """Internal async method to send a permission response."""
        if isinstance(self.agent, CodexAgent):
            # Codex agent: route through its approval response handler
            await self.agent.send_approval_response(request_id, response_data)
        elif isinstance(self.agent, PiAgent) or is_extension_ui:
            # Pi agent only supports extension_ui_request/response protocol
            # (no control_request/response). Also used for explicit extension UI responses.
            if is_extension_ui:
                # Already in extension_ui_response shape (confirmed/cancelled/value)
                pi_response_data = response_data
            else:
                # Convert allow/deny behavior to extension_ui_response shape
                behavior = response_data.get("behavior", "deny")
                if behavior == "allow":
                    pi_response_data = {"confirmed": True}
                else:
                    pi_response_data = {"cancelled": True}
            response = {
                "type": "extension_ui_response",
                "id": request_id,
                **pi_response_data
            }
            try:
                await self.agent._write_json(response)
            except Exception as e:
                LOG.error(f"Failed to send extension ui response: {e}")
        else:
            # Claude agent: send control_response JSON
            response = {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": request_id,
                    "response": response_data
                }
            }
            try:
                await self.agent._write_json(response)
            except Exception as e:
                LOG.error(f"Failed to send permission response: {e}")

    def send_permission_response(self, request_id, response_data, is_extension_ui=False):
        """Schedule a permission response to be sent."""
        if self.loop and self.agent:
            asyncio.run_coroutine_threadsafe(
                self._send_permission_response(request_id, response_data, is_extension_ui),
                self.loop
            )

    def send(self, text):
        """Queue input to be sent."""
        if self.loop and self.input_queue:
            LOG.debug(f"Send to agent msg: {text}")
            self.loop.call_soon_threadsafe(self.input_queue.put_nowait, text)

    def stop(self):
        """Signal thread to stop."""
        self.running = False

    def reset(self):
        """Reset the agent session by disconnecting and reconnecting."""
        if self.loop and self.agent:
            # Schedule the reset in the agent's event loop
            asyncio.run_coroutine_threadsafe(self._reset_agent(), self.loop)

    def update_config(self, **kwargs):
        """Update the agent configuration dynamically."""
        self.anthropic_config.update(kwargs)
        if not self.agent:
            return

        if "plan_mode" in kwargs:
            plan_mode = kwargs["plan_mode"]
            if isinstance(self.agent, CodexAgent):
                self.agent.plan_mode = plan_mode
                LOG.info(f"Updated Codex plan_mode to: {plan_mode}")
            elif isinstance(self.agent, PiAgent):
                asyncio.run_coroutine_threadsafe(
                    self.agent.set_plan_mode(plan_mode),
                    self.loop
                )
                LOG.info(f"Updated Pi plan_mode to: {plan_mode}")
            elif isinstance(self.agent, ClaudeCodeAgent):
                # Map boolean plan_mode to CLI permission mode
                # If plan_mode is True, use 'plan'
                # If plan_mode is False, use 'default'
                mode = "plan" if plan_mode else "default"
                asyncio.run_coroutine_threadsafe(
                    self.agent.set_permission_mode(mode),
                    self.loop
                )
                LOG.info(f"Updated Claude plan_mode to: {plan_mode} (perm: {mode})")

        if "model" in kwargs:
            model = kwargs["model"]
            if isinstance(self.agent, CodexAgent):
                self.agent.set_model(model)
                LOG.info(f"Updated Codex model to: {model}")
            elif isinstance(self.agent, ClaudeCodeAgent):
                asyncio.run_coroutine_threadsafe(
                    self.agent.set_model(model),
                    self.loop
                )
                LOG.info(f"Updated Claude model to: {model}")
            elif isinstance(self.agent, PiAgent):
                asyncio.run_coroutine_threadsafe(
                    self.agent.set_model(model),
                    self.loop
                )
                LOG.info(f"Updated Pi model to: {model}")


class InputPromptMarker:
    """
    Renders the ❯ prompt as an inline phantom at the start of the input line,
    keeping the marker out of the buffer text so the editable region starts at
    column 0.
    """
    HTML = (
        "<body id='chatview-input-marker' style='margin:0;padding:0'>"
        "<span style='color:var(--foreground);padding-right:0.2em'>❯</span>"
        "</body>"
    )

    def __init__(self, view):
        self.view = view
        self.phantom_set = sublime.PhantomSet(view, "chatview_input_marker")

    def update(self):
        """Pin the marker at the input line start."""
        start = input_editable_start(self.view)
        if start > self.view.size():
            # Input line not created yet (fresh view before term_chat_input_prompt)
            return
        phantom = sublime.Phantom(
            sublime.Region(start, start),
            self.HTML,
            sublime.LAYOUT_INLINE,
        )
        self.phantom_set.update([phantom])

    def clear(self):
        self.phantom_set.update([])


class ModelPanel:
    """
    Displays the current model name as a phantom above the prompt area.
    """
    def __init__(self, view, window):
        self.view = view
        self.window = window
        self.phantom_set = sublime.PhantomSet(view, "chatview_model")
        self.status_hint = StatusHint()

    def set_running(self, running):
        """Show the stop hint only while the agent is processing a turn."""
        if self.status_hint.visible == running:
            return
        self.status_hint.set_visible(running)
        self.update()

    def set_stopping(self, stopping, text=None):
        """Update the stop label while the agent processes an interrupt."""
        if self.status_hint.visible and self.status_hint.set_stopping(stopping, text):
            self.update()

    def update(self):
        """Update the model phantom display."""
        plan_mode_value = self.window.settings().get(CHAT_PLAN_MODE, PlanMode.FAST.value)
        try:
            plan_mode = PlanMode(plan_mode_value)
        except ValueError:
            plan_mode = PlanMode.FAST

        input_start = get_input_start(self.view)
        region = sublime.Region(input_start, input_start)

        agent_provider = self.window.settings().get(CHAT_AGENT, "claude")
        model = self.window.settings().get(f"chatview_model_{agent_provider}") or "default"
        # Keep the display key in sync
        self.window.settings().set(CHAT_MODEL, model)

        display_model = model
        if agent_provider == "pi" and display_model and "/" in display_model:
            display_model = display_model.split("/", 1)[-1]

        plan_tag_html = ""
        if plan_mode == PlanMode.PLANNING:
            plan_tag_html = """
                <a href="toggle_plan" class="model-tag" style="margin-left: 4px;">
                    <span class="label">PlanMode:</span>
                    <span class="value">planning</span>
                </a>
            """
        else:
            plan_tag_html = """
                <a href="toggle_plan" class="model-tag" style="margin-left: 4px;">
                    <span class="label">PlanMode:</span>
                    <span class="value">fast</span>
                </a>
            """

        stop_hint_html = self.status_hint.render()

        html = f"""
        <body id="chatview-model" style="margin: 0; padding: 0;">
            <style>
                .model-row {{
                    margin: 0 0 6px 0;
                    padding: 8px 0;
                    border-bottom: 1px solid color(var(--foreground) alpha(0.06));
                }}
                .model-tag {{
                    color: var(--accent);
                    background-color: color(var(--accent) alpha(0.08));
                    border: 1px solid color(var(--accent) alpha(0.15));
                    font-size: 0.85em;
                    font-family: var(--font-mono);
                    text-decoration: none;
                    padding: 4px 2px;
                    border-radius: 4px;
                    line-height: 1.2;
                }}
                .stop-hint {{
                    color: var(--accent);
                    display: inline-block;
                    text-decoration: none;
                    margin-left: 2px;
                    padding: 4px 2px;
                    line-height: 1.2;
                }}
                .icon {{
                    padding-right: 2px;
                }}
                .label {{
                    opacity: 0.7;
                    padding-right: 2px;
                }}
                .value {{
                    font-weight: bold;
                }}
            </style>
            <div class="model-row">
                <a href="set_agent" class="model-tag">
                    <span class="icon">✨</span>
                    <span class="label">Agent:</span>
                    <span class="value">{agent_provider}</span>
                </a>
                <a href="set_model" class="model-tag" style="margin-left: 4px;">
                    <span class="label">Model:</span>
                    <span class="value">{display_model}</span>
                </a>{plan_tag_html}{stop_hint_html}
            </div>
        </body>
        """

        def on_navigate(href):
            if href == "set_agent":
                self.window.run_command("term_chat_set_agent")
            elif href == "set_model":
                self.window.run_command("term_chat_set_model")
            elif href == "toggle_plan":
                self.window.run_command("term_chat_toggle_plan_mode")
            elif href == "stop_conversation":
                self.window.run_command("term_chat_interrupt", {"confirm": True})

        self.phantom_set.update([sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK,
            on_navigate
        )])

    def clear(self):
        """Clear the model phantom."""
        self.phantom_set.update([])


class PermissionPanel:
    """
    Displays permission request phantoms for tool authorization.
    """
    def __init__(self, view, window, on_action):
        """
        :param view: Sublime View object.
        :param window: Sublime Window object.
        :param on_action: Callback function(request_id, action) for handling user actions.
        """
        self.view = view
        self.window = window
        self.on_action = on_action
        self.phantom_sets = {}  # Map of request_id -> PhantomSet
        self.diff_data = {}  # Map of request_id -> (old_text, new_text, name)

    def show(self, request_id, tool_name, input_data, approve_mode=None):
        """Show a permission phantom for a tool request."""
        input_start = get_input_start(self.view)
        region = sublime.Region(input_start - 1, input_start)

        phantom_set = sublime.PhantomSet(self.view, f"permission_{request_id}")
        self.phantom_sets[request_id] = phantom_set

        def on_navigate(action):
            self._handle_navigate(request_id, action)

        # Prepare display content based on tool type
        display_content = self._prepare_display_content(request_id, tool_name, input_data)
        has_diff = request_id in self.diff_data

        html = self._build_html(request_id, tool_name, display_content, has_diff, approve_mode)
        phantom_set.update([sublime.Phantom(region, html, sublime.LAYOUT_BLOCK, on_navigate)])

        # Scroll to bottom to show request
        self.view.show(self.view.size())

    @staticmethod
    def _html_escape(text):
        """Escape HTML special characters in text."""
        return (text
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    def _focus_plan_if_exists(self, request_id):
        """Focus an existing plan view for this request and return True if found."""
        for v in self.window.views():
            if v.settings().get("chatview_plan_request_id") == request_id:
                self.window.focus_view(v)
                return True
        return False

    def _open_plan(self, request_id, plan, name, background=False):
        """Open a new plan view for this request."""
        active_view = self.window.active_view()

        plan_view = self.window.new_file()
        plan_view.set_name(name)
        plan_view.settings().set("chatview_plan_request_id", request_id)
        plan_view.run_command("append", {"characters": plan})
        plan_view.set_syntax_file("Packages/Markdown/Markdown.sublime-syntax")
        plan_view.set_scratch(True)

        if background and active_view:
            self.window.focus_view(active_view)

    def _prepare_display_content(self, request_id, tool_name, input_data):
        """Prepare the display content for a permission request."""
        if tool_name == "Edit":
            old_text = input_data.get("old_string", "")
            new_text = input_data.get("new_string", "")
            file_path = input_data.get("file_path", "unknown")
            name = os.path.basename(file_path)
            self.diff_data[request_id] = (old_text, new_text, name)
            return f'📄 <a href="show_diff" class="file-link">{name}</a>'

        elif tool_name in ("ExitPlanMode", "CodexImplementPlan", "ImplementPlan"):
            plan = input_data.get("plan", "")
            first_line = plan.split("\n")[0] if plan else "Empty Plan"
            self.diff_data[request_id] = ("", plan, "Implementation Plan")

            # Automatically open the plan in a new view in the background
            sublime.set_timeout(lambda: self._open_plan(request_id, plan, "Implementation Plan", background=True), 0)

            return (
                f'📄 <a href="show_plan" class="file-link">plan</a>\n\n'
                f'{first_line}'
            )

        elif tool_name == "Write":
            file_path = input_data.get("file_path", "")
            new_text = input_data.get("content", "")
            old_text = ""
            if file_path and os.path.exists(file_path):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        old_text = f.read()
                except Exception as e:
                    LOG.error(f"Failed to read file for diff: {e}")
            name = os.path.basename(file_path) if file_path else "new_file"
            self.diff_data[request_id] = (old_text, new_text, name)
            return f'📄 <a href="show_diff" class="file-link">{name}</a>'
        elif tool_name == "command_execution":
            command = input_data.get("command", "")
            cwd = input_data.get("cwd", "")
            display = self._html_escape(command)
            if cwd:
                display += f'\n<span style="opacity:0.6">cwd: {self._html_escape(cwd)}</span>'
            return display

        elif tool_name == "fileChange":
            processed_diff = input_data.get("processed_diff")
            if not processed_diff:
                return f"<b>{tool_name}</b> (no changes)"

            old_text = processed_diff.get("old_text", "")
            new_text = processed_diff.get("new_text", "")
            display_name = processed_diff.get("display_name", "file")
            self.diff_data[request_id] = (old_text, new_text, display_name)

            files = processed_diff.get("files", [])
            count = processed_diff.get("count", 1)
            summary = f'📄 <a href="show_diff" class="file-link">{self._html_escape(display_name)}</a>'
            if count > 1:
                limit = 5
                details = "<br>".join([f"&nbsp;&nbsp;- {self._html_escape(f)}" for f in files[:limit]])
                if len(files) > limit:
                    details += f"<br>&nbsp;&nbsp;- ... and {len(files) - limit} more"
                return f"{summary}<br>{details}"
            return summary

        else:
            # Format input data line by line
            display_lines = []
            for k, v in input_data.items():
                if isinstance(v, str):
                    display_lines.append(f"{self._html_escape(k)}: {self._html_escape(v)}")
            return "\n".join(display_lines)

    def _build_html(self, request_id, tool_name, display_content, has_diff, approve_mode=None):
        """Build the HTML for the permission phantom."""
        allow_chat_btn = ""
        if approve_mode in (ApproveMode.DEFAULT.value, ApproveMode.ALLOW_EDIT.value):
            allow_chat_btn = '<a href="allow_chat" class="btn btn-chat">Allow for this chat</a>'

        allow_btn_text = "Implement the Plan" if tool_name in ("ExitPlanMode", "CodexImplementPlan", "ImplementPlan") else "Allow"

        return f"""
        <body id="permission-{request_id}">
            <style>
                .permission-box {{
                    background-color: color(var(--background) blend(var(--foreground) 92%));
                    padding: 10px 10px 15px 10px;
                    border: 1px solid var(--accent);
                    border-radius: 4px;
                    margin: 10px 0;
                }}
                .header {{
                    font-weight: bold;
                    color: var(--accent);
                    margin-bottom: 5px;
                }}
                .content {{
                    font-family: var(--font-mono);
                    font-size: 0.9em;
                    white-space: pre-wrap;
                    margin-bottom: 20px;
                }}
                .file-link {{
                    text-decoration: none;
                    color: var(--accent);
                    font-weight: bold;
                    font-size: 1.1em;
                    background-color: color(var(--background) blend(var(--foreground) 85%));
                    padding: 2px 6px;
                    border-radius: 3px;
                }}
                .actions {{
                    display: block;
                    margin-top: 15px;
                    margin-bottom: 5px;
                }}
                .btn {{
                    display: inline-block;
                    text-decoration: none;
                    padding: 4px 8px;
                    border-radius: 3px;
                    font-weight: bold;
                }}
                .btn-allow {{
                    background-color: var(--greenish);
                    color: var(--background);
                }}
                .btn-deny {{
                    background-color: var(--redish);
                    color: var(--background);
                    margin-left: 10px;
                }}
                .btn-chat {{
                    background-color: color(var(--background) blend(var(--foreground) 75%));
                    color: var(--foreground);
                    margin-left: 30px;
                    border: 1px solid color(var(--foreground) alpha(0.2));
                }}
                .btn-diff {{
                    background-color: var(--accent);
                    color: var(--background);
                    margin-left: 10px;
                }}
            </style>
            <div class="permission-box">
                <div class="header">Tool Permission: {tool_name}</div>
                <div class="content">{display_content}</div>
                <div class="actions">
                    <a href="allow" class="btn btn-allow">{allow_btn_text}</a>
                    <a href="deny" class="btn btn-deny">Deny</a>
                    {allow_chat_btn}
                </div>
            </div>
        </body>
        """

    def _handle_navigate(self, request_id, action):
        """Handle navigation actions from the phantom."""
        if action == "show_diff":
            if request_id in self.diff_data:
                old_text, new_text, name = self.diff_data[request_id]
                plugin.show_diff(self.window, old_text, new_text, name)
            return
        elif action == "show_plan":
            if request_id in self.diff_data:
                _, plan, name = self.diff_data[request_id]
                if not self._focus_plan_if_exists(request_id):
                    self._open_plan(request_id, plan, name)
            return

        # For allow/deny actions, delegate to the callback
        self.on_action(request_id, action)

    def clear(self, request_id):
        """Remove a permission phantom."""
        if request_id in self.phantom_sets:
            self.phantom_sets[request_id].update([])
            del self.phantom_sets[request_id]
        if request_id in self.diff_data:
            del self.diff_data[request_id]

    def clear_all(self):
        """Remove all permission phantoms."""
        for phantom_set in self.phantom_sets.values():
            phantom_set.update([])
        self.phantom_sets.clear()
        self.diff_data.clear()


class SelectPanel:
    """
    A generic UI component for showing a Sublime Quick Panel.
    Supports both single and multi-select modes.
    """
    def __init__(self, window, items, on_done, placeholder="", multi_select=False):
        """
        :param window: Sublime Window object.
        :param items: List of (label, description) tuples.
        :param on_done: Callback function(indices or None).
        :param placeholder: Text to show in the input.
        :param multi_select: Boolean, if True allows selecting multiple items.
        """
        self.window = window
        self.items = items
        self.on_done = on_done
        self.placeholder = placeholder
        self.multi_select = multi_select
        self.selected_indices = set()

    def show(self):
        """Show the quick panel."""
        display_items = []

        if self.multi_select:
            # Add Done option for multi-select
            display_items.append(sublime.QuickPanelItem("Done", "Finish selection", kind=sublime.KIND_ID_AMBIGUOUS))
            for i, (label, desc) in enumerate(self.items):
                prefix = "✅ " if i in self.selected_indices else "⬜ "
                display_items.append(sublime.QuickPanelItem(prefix + label, desc))
        else:
            # Single select items
            for label, desc in self.items:
                display_items.append(sublime.QuickPanelItem(label, desc))

        # Show panel
        flags = sublime.KEEP_OPEN_ON_FOCUS_LOST if self.multi_select else 0
        self.window.show_quick_panel(
            display_items,
            self._handle_done,
            flags=flags,
            placeholder=self.placeholder
        )

    def _handle_done(self, index):
        """Internal callback for the quick panel."""
        if index == -1:
            # User cancelled (Esc)
            self.on_done(None)
            return

        if self.multi_select:
            # Handle Done option (index 0)
            if index == 0:
                self.on_done(list(self.selected_indices))
                return

            # Toggle selection (adjust index for "Done" item)
            real_index = index - 1
            if real_index in self.selected_indices:
                self.selected_indices.remove(real_index)
            else:
                self.selected_indices.add(real_index)

            # Re-show panel to allow more selections
            sublime.set_timeout(self.show, 0)
        else:
            # Single select - Submit immediately
            self.on_done([index])


class AskUserQuestionHandler:
    """
    Handles the AskUserQuestion tool using SelectPanel.
    """
    def __init__(self, session, request_id, input_data):
        self.session = session
        self.request_id = request_id
        self.input_data = input_data

    def run(self):
        """Start the question flow."""
        questions = self.input_data.get("questions", [])
        if not questions:
            self._cleanup()
            return

        # Currently only handling the first question as per original implementation
        question_data = questions[0]
        options = question_data.get("options", [])
        items = [(opt.get("label", ""), opt.get("description", "")) for opt in options]
        multi_select = question_data.get("multiSelect", False)
        question_text = question_data.get("question", "")

        def on_done(indices):
            if indices is None:
                # User cancelled (Esc) - Deny permission
                self.session.send_permission_response(self.request_id, {
                    "behavior": "deny",
                    "message": "User cancelled selection"
                })
            else:
                # Submit selections
                self._submit(indices, question_data)
            self._cleanup()

        SelectPanel(
            self.session.window,
            items,
            on_done,
            placeholder=question_text,
            multi_select=multi_select
        ).show()

    def _submit(self, indices, question_data):
        """Submit the selected options back to the agent."""
        options = question_data.get("options", [])
        selected_labels = [options[i]["label"] for i in indices if i < len(options)]

        answers = {}
        if selected_labels:
            # Use 'id' if present (e.g. for Codex), otherwise fallback to the question text (e.g. for Claude)
            question_key = question_data.get("id", question_data.get("question", ""))
            multi_select = question_data.get("multiSelect", False)
            if multi_select:
                answers[question_key] = selected_labels
            else:
                answers[question_key] = selected_labels[0]

        # Update the input_data with answers
        updated_input = self.input_data.copy()
        updated_input["answers"] = answers

        self.session.send_permission_response(self.request_id, {
            "behavior": "allow",
            "updatedInput": updated_input
        })

    def _cleanup(self):
        """Remove the request from the session's pending list."""
        if self.request_id in self.session.permission_requests:
            del self.session.permission_requests[self.request_id]


class ChatSession:
    """
    Manages the state and UI for a single ChatView session.
    """
    def __init__(self, window, view, cwd, add_dirs=None, session_id=None):
        self.window = window
        self.chat_view = view
        self.cwd = cwd
        self.add_dirs = add_dirs or []
        self.loading_animation = LoadingAnimation(self.chat_view)

        self.model_phantom = ModelPanel(self.chat_view, self.window)
        self.input_marker = InputPromptMarker(self.chat_view)
        if self.chat_view.settings().has(CHAT_INPUT_START):
            # Reconnect case: input line already exists, pin the marker now
            self.input_marker.update()
        self.rewind_confirm_panel = RewindConfirmPanel(self.chat_view)
        self.permission_panel = PermissionPanel(
            self.chat_view, self.window, self._handle_permission_decision
        )
        self.history = []
        self.history_index = 0
        self.history_stash = ""
        self.permission_requests = {} # Map of request_id -> (tool_name, input_data)
        self.available_models = []  # Will be populated from control_response
        self.prompt_regions = [] # List of (Region, uuid, phantom_index) for submitted prompts
        self.prompt_button_phantoms = [] # List of PhantomSet, parallel to prompt_regions
        self.session_allow_all = False
        # Only persist session_id after the first user message
        self.has_sent_message = bool(session_id)

        # End-of-turn file changes artifact (records edit diffs, renders file list)
        self.artifact = FileChangesArtifact(self.chat_view, self.window, get_input_start)

        self.implement_plan_phantoms = sublime.PhantomSet(self.chat_view, "implement_plan")
        self.implement_plan_buttons = [] # List of (region, phantom) tuples

        self.agent_thread = None

        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        self.available_agents = get_available_agents(settings)

        if not self.available_agents:
            self.chat_view.run_command("term_chat_output_append", {
                "text": f"\n\n⚠️ Error: No agent CLI found.\nPlease install Claude CLI (`npm install -g @anthropic-ai/claude-code`) or Codex CLI, or set their paths in {PACKAGE_NAME} settings.\n\n"
            })
            self.window.run_command("term_chat_install_agent")
            return

        # Determine agent provider early
        agent_provider = self.window.settings().get(CHAT_AGENT, settings.get("agent_provider", "claude"))

        if agent_provider not in self.available_agents:
            agent_provider = self.available_agents[0]
            self.window.settings().set(CHAT_AGENT, agent_provider)

        if agent_provider == "codex":
            self.message_processor = CodexMessageProcessor(self)
        elif agent_provider == "pi":
            self.message_processor = PiMessageProcessor(self)
        else:
            self.message_processor = ClaudeMessageProcessor(self)

        # Load cli_path from settings (provider-specific only, no fallback to avoid mixing CLIs)
        cli_path = settings.get(f"{agent_provider}_command")
        if not cli_path:
            cli_path = None  # Let the agent class find its own CLI

        # Use provider-specific model key so switching agents won't carry over incompatible models
        model = self.window.settings().get(f"chatview_model_{agent_provider}") or None

        disallowed_tools = self._get_disallowed_tools(settings)

        anthropic_config = {
            "model": model,
            "plan_mode": self.window.settings().get(CHAT_PLAN_MODE) == PlanMode.PLANNING.value,
            "allowed_tools": settings.get("allowed_tools"),
            "disallowed_tools": disallowed_tools,
            "agent_provider": agent_provider,
            "approve_mode": self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value),
            "session_id": session_id,
            "env": settings.get("env", {}),
            "debug_agent_message": settings.get("debug_agent_message", False)
        }

        # Initialize background agent thread
        self.agent_thread = AgentThread(
            cwd,
            self._handle_agent_message,
            cli_path=cli_path,
            anthropic_config=anthropic_config,
            add_dirs=self.add_dirs
        )
        self.agent_thread.start()

    @staticmethod
    def get_view_session_id(view):
        return view.settings().get(CHAT_SESSION_ID)

    def set_view_session_id(self, view, session_id):
        """Persist session_id only if the user has sent message."""
        if not self.has_sent_message:
            return
        view.settings().set(CHAT_SESSION_ID, session_id)

    def sync_pi_approve_mode(self, mode):
        """Sends a phantom command to pi agent to sync approve mode."""
        if self.agent_thread and self.agent_thread.agent and self.agent_thread.agent.is_connected:
            if isinstance(self.message_processor, PiMessageProcessor):
                import uuid
                message = {
                    "type": "prompt",
                    "message": f"/termchat-setting approve_mode={mode}",
                    "id": str(uuid.uuid4())
                }
                if hasattr(self.agent_thread.agent, "_write_json"):
                    import asyncio
                    asyncio.run_coroutine_threadsafe(
                        self.agent_thread.agent._write_json(message),
                        self.agent_thread.loop
                    )

    def _get_disallowed_tools(self, settings):
        """Returns a list of disallowed tools based on settings."""
        disallowed_tools = settings.get("disallowed_tools", [])
        if settings.get("disable_ask_user", False):
            if "AskUserQuestion" not in disallowed_tools:
                disallowed_tools = disallowed_tools + ["AskUserQuestion"]
        return disallowed_tools

    def show_permission_phantom(self, request_id, tool_name, input_data):
        """Show a phantom asking for permission."""
        if tool_name == "AskUserQuestion":
            self.handle_ask_user_question(request_id, input_data)
            return

        if self.session_allow_all:
            self._auto_approve(request_id, input_data)
            return

        approve_mode = self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)

        always_confirm_tools = ("ExitPlanMode",)
        if tool_name in always_confirm_tools:
            self.permission_panel.show(request_id, tool_name, input_data, approve_mode=approve_mode)
            return

        if approve_mode == ApproveMode.ACCEPT_ALL.value:
            self._auto_approve(request_id, input_data)
            return

        risky_tools = ("Bash",)
        if approve_mode == ApproveMode.ALLOW_EDIT.value and tool_name not in risky_tools:
            self._auto_approve(request_id, input_data)
            return

        self.permission_panel.show(request_id, tool_name, input_data, approve_mode=approve_mode)

    def _auto_approve(self, request_id, input_data):
        """Auto-approve a permission request without showing a phantom."""
        if request_id in self.permission_requests:
            response_data = {
                "behavior": "allow",
                "updatedInput": input_data
            }
            self.send_permission_response(request_id, response_data)
            del self.permission_requests[request_id]

    def handle_ask_user_question(self, request_id, input_data):
        """Handle AskUserQuestion tool using Quick Panel."""
        handler = AskUserQuestionHandler(self, request_id, input_data)
        handler.run()

    def clear_permission_phantom(self, request_id):
        """Remove the permission phantom."""
        self.permission_panel.clear(request_id)

    def _handle_permission_decision(self, request_id, action):
        """Handle allow/deny decision from PermissionPanel."""
        LOG.info(f"Permission decision: {action} for request {request_id}")

        if request_id in self.permission_requests:
            tool_name, input_data = self.permission_requests[request_id]

            if tool_name in ("CodexImplementPlan", "ImplementPlan"):
                if action == "allow":
                    self.window.run_command("term_chat_implement_plan")
                self.clear_permission_phantom(request_id)
                del self.permission_requests[request_id]
                return

            if tool_name == "ExitPlanMode":
                # Claude CLI's ExitPlanMode is a real control_request that needs a response
                if action == "allow":
                    self.send_permission_response(request_id, {
                        "behavior": "allow",
                        "updatedInput": input_data
                    })
                    # Force UI out of plan mode so subsequent turns use fast mode
                    self.window.run_command("term_chat_toggle_plan_mode", {"mode": "fast"})
                else:
                    self.send_permission_response(request_id, {
                        "behavior": "deny",
                        "message": "User chose to stay in plan mode"
                    })
                self.clear_permission_phantom(request_id)
                del self.permission_requests[request_id]
                return

            if tool_name == "termchat_tool_permission":
                if action in ("allow", "allow_chat"):
                    response_data = {"confirmed": True}
                else:
                    response_data = {"cancelled": True}
                self.send_permission_response(request_id, response_data, is_extension_ui=True)
                self.clear_permission_phantom(request_id)
                del self.permission_requests[request_id]
                return

            if tool_name.startswith("extension_ui_"):
                is_confirm = tool_name == "extension_ui_confirm"
                if action in ("allow", "allow_chat"):
                    response_data = {"confirmed": True} if is_confirm else {"value": "Allow"}
                else:
                    response_data = {"cancelled": True}
                self.send_permission_response(request_id, response_data, is_extension_ui=True)
                self.clear_permission_phantom(request_id)
                del self.permission_requests[request_id]
                return

            if action == "allow":
                response_data = {
                    "behavior": "allow",
                    "updatedInput": input_data
                }
            elif action == "allow_chat":
                self.session_allow_all = True
                response_data = {
                    "behavior": "allow",
                    "updatedInput": input_data
                }
            else:
                response_data = {
                    "behavior": "deny",
                    "message": "User denied permission via UI"
                }

            self.send_permission_response(request_id, response_data)

            # Cleanup
            self.clear_permission_phantom(request_id)
            del self.permission_requests[request_id]

    def send_permission_response(self, request_id, response_data, is_extension_ui=False):
        """Send a control response back to the agent via agent_thread."""
        if self.agent_thread:
            self.agent_thread.send_permission_response(request_id, response_data, is_extension_ui)

    def _handle_agent_message(self, message):
        """Handle messages received from the agent thread."""
        self.message_processor.handle_message(message)

    def start_loading(self, text=None):
        """Start the loading animation."""
        def start():
            self.loading_animation.start(self.loading_region, text)
            self.model_phantom.set_running(True)

        sublime.set_timeout(start, 0)

    def stop_loading(self):
        def stop():
            self.loading_animation.stop()
            self.model_phantom.set_running(False)

        sublime.set_timeout(stop, 0)

    def loading_region(self):
        """Get the region where the loading animation should be displayed."""
        input_start = get_input_start(self.chat_view)
        return sublime.Region(input_start-1, input_start)

    def stop(self):
        self.loading_animation.stop()
        self.model_phantom.set_running(False)
        self.model_phantom.clear()
        self.input_marker.clear()
        self.rewind_confirm_panel.clear()
        self.permission_panel.clear_all()
        self.implement_plan_phantoms.update([])
        if self.agent_thread:
            self.agent_thread.stop()

    def show_implement_plan_button(self, plan_text="", tool_name="CodexImplementPlan"):
        """Show a phantom button to trigger plan implementation via PermissionPanel."""
        request_id = f"codex_plan_{id(self)}"
        self.permission_requests[request_id] = (tool_name, {"plan": plan_text})
        self.permission_panel.show(request_id, tool_name, {"plan": plan_text})

    @property
    def plan_mode(self):
        val = self.window.settings().get(CHAT_PLAN_MODE, PlanMode.FAST.value)
        try:
            return PlanMode(val)
        except ValueError:
            return PlanMode.FAST

    @plan_mode.setter
    def plan_mode(self, value):
        if isinstance(value, PlanMode):
            self.window.settings().set(CHAT_PLAN_MODE, value.value)
        else:
            self.window.settings().set(CHAT_PLAN_MODE, value)

    def send_input(self, user_input, region=None):
        """Start animation and send to agent."""
        self.rewind_confirm_panel.clear()
        if not self.agent_thread:
            self.chat_view.run_command("term_chat_output_append",
                {"text": f"\n\n⚠️ Error: No agent CLI found.\n\n"})
            self.stop_loading()
            return

        if region:
            self.add_prompt_highlight(region)
        self.message_processor._plan_text = ""
        self.has_sent_message = True
        s_id = self.agent_thread.session_id
        if s_id:
            self.chat_view.settings().set(CHAT_SESSION_ID, s_id)
        self.agent_thread.send(user_input)

    def steer(self, text, proceed_plan=False):
        """Send steering message to agent."""
        if self.agent_thread:
            self.agent_thread.steer(text, proceed_plan=proceed_plan)

    def record_file_change(self, abs_path, rel_path, diff_text):
        """Record an edit diff so the file is listed in the end-of-turn artifact."""
        extra_env = self.agent_thread.anthropic_config.get("env") if self.agent_thread else None
        self.artifact.record(abs_path, rel_path, diff_text, extra_env=extra_env)

    def show_file_changes_artifact(self):
        """Append the collapsed file changes artifact for the finished turn."""
        self.artifact.show()

    def open_artifact_diff_at(self, point):
        """If point is on an artifact file name, open its diff view. Returns True if handled."""
        return self.artifact.open_diff_at(point)

    def add_prompt_highlight(self, region):
        """Add a gutter highlight and an end-of-line rewind button for a submitted prompt."""
        region_index = len(self.prompt_regions)
        phantom_index = len(self.prompt_button_phantoms)
        self.prompt_regions.append((region, None, phantom_index))
        phantom_set = sublime.PhantomSet(self.chat_view, f"chatview_rewind_btn_{region_index}")
        self.prompt_button_phantoms.append(phantom_set)
        self._redraw_prompt_highlights()
        # Button starts greyed out — uuid not yet available
        self._draw_prompt_button(region_index, active=False)

    def update_last_prompt_uuid(self, uuid):
        """Attach the echoed user message UUID to the most recent prompt region."""
        if not self.prompt_regions:
            return
        region_index = len(self.prompt_regions) - 1
        region, _, phantom_index = self.prompt_regions[region_index]
        self.prompt_regions[region_index] = (region, uuid, phantom_index)
        # Activate the button now that we have a uuid
        self._draw_prompt_button(region_index, active=True)

    def _draw_prompt_button(self, region_index, active):
        """Draw (or redraw) the end-of-line rewind button phantom for prompt at region_index."""
        region, _uuid, phantom_index = self.prompt_regions[region_index]
        if phantom_index >= len(self.prompt_button_phantoms):
            return
        phantom_set = self.prompt_button_phantoms[phantom_index]

        # Anchor at the end of the last line of the user's text.
        # region.end() may point past the user's text if the view grew after
        # submit (next prompt appended), so clamp to the line boundary.
        last_char = max(region.begin(), region.end() - 1)
        end_point = self.chat_view.line(last_char).end()
        anchor = sublime.Region(end_point, end_point)

        if active:
            html = (
                "<body style='margin:0;padding:0'>"
                "<a href='rewind' style='color:var(--orangish);text-decoration:none;"
                "font-size:0.85em;opacity:0.7;padding:2px 10px;display:inline-block;'>↩</a>"
                "</body>"
            )
            def on_navigate(href, idx=region_index):
                if href != "rewind":
                    return
                if self.rewind_confirm_panel.visible:
                    self.rewind_confirm_panel.clear()
                    return
                r, *_ = self.prompt_regions[idx]
                def on_confirm(i=idx):
                    sublime.status_message(f"Rewinding to prompt {i + 1}...")
                    self.rewind_to_prompt(i)
                self.rewind_confirm_panel.show(r, idx, on_confirm)
        else:
            html = (
                "<body style='margin:0;padding:0'>"
                "<span style='color:var(--foreground);"
                "font-size:0.85em;opacity:0.25;padding:2px 10px;display:inline-block;'>↩</span>"
                "</body>"
            )
            on_navigate = None

        phantom_set.update([sublime.Phantom(
            anchor, html, sublime.LAYOUT_INLINE, on_navigate
        )])

    def _redraw_prompt_highlights(self):
        """Redraw all gutter dots from prompt_regions."""
        self.chat_view.add_regions(
            PROMPT_HIGHLIGHT_KEY,
            [r for r, *_ in self.prompt_regions],
            PROMPT_HIGHLIGHT_SCOPE,
            PROMPT_HIGHLIGHT_ICON,
            PROMPT_HIGHLIGHT_FLAGS
        )

    def clear_prompt_buttons(self):
        """Remove all rewind button phantoms; keep gutter dots but disable rewind."""
        for phantom_set in self.prompt_button_phantoms:
            phantom_set.update([])
        # Null UUIDs so gutter clicks are blocked
        self.prompt_regions = [(r, None, pi) for r, _, pi in self.prompt_regions]
        self.prompt_button_phantoms = []

    def clear_prompt_highlights(self):
        """Clear all prompt gutter highlights and inline buttons."""
        self.prompt_regions = []
        self.chat_view.erase_regions(PROMPT_HIGHLIGHT_KEY)
        self.clear_prompt_buttons()

    def rewind_to_prompt(self, prompt_index):
        """Fork the current session up to prompt_index and restart the agent on the fork."""
        if prompt_index < 0 or prompt_index >= len(self.prompt_regions):
            return

        _region, user_message_uuid, *_ = self.prompt_regions[prompt_index]
        session_id = self.agent_thread.session_id if self.agent_thread else None

        if not session_id:
            LOG.warning("[rewind] no active session ID")
            sublime.status_message("Rewind: no active session ID found")
            return

        if not user_message_uuid:
            LOG.warning(f"[rewind] no uuid for prompt {prompt_index}")
            sublime.status_message("Rewind: message UUID not yet available for this prompt")
            return

        self.stop_loading()

        def _on_rewind(new_session_id):
            if new_session_id is None:
                sublime.status_message("Rewind failed: see console for details")
                return
            self._on_rewind_complete(new_session_id, prompt_index)

        self.agent_thread.rewind(user_message_uuid, on_done=_on_rewind)

    def _on_rewind_complete(self, new_session_id, prompt_index):
        """Called on main thread after fork completes; restarts agent on forked session."""
        region, _uuid, phantom_index = self.prompt_regions[prompt_index]
        cut_point = region.begin() - len(PROMPT_PREFIX)

        # Clear button phantoms for trimmed prompts
        for phantom_set in self.prompt_button_phantoms[phantom_index:]:
            phantom_set.update([])
        self.prompt_regions = self.prompt_regions[:prompt_index]
        self.prompt_button_phantoms = self.prompt_button_phantoms[:phantom_index]
        self._redraw_prompt_highlights()

        self.artifact.truncate(cut_point)

        rewind_text = self.chat_view.substr(region)

        self.session_allow_all = False
        self.has_sent_message = True
        self.chat_view.settings().set(CHAT_SESSION_ID, new_session_id)

        if cut_point >= 0:
            self.chat_view.run_command("term_chat_rewind_truncate", {"cut_point": cut_point, "rewind_text": rewind_text})
        else:
            self.chat_view.run_command("term_chat_input_prompt", {"text": rewind_text})

        self.chat_view.run_command(
            "term_chat_output_append",
            {"text": "\n■ Rewind conversation to earlier checkpoint\n"}
        )

        self.reload_agent(session_id_override=new_session_id, quiet=True)

    def reset_session(self):
        """Reset the chat session by restarting the agent and notifying in UI."""
        if not self.agent_thread:
            return

        # Stop any ongoing loading animation
        self.stop_loading()
        self.clear_prompt_highlights()
        self.artifact.clear()
        self.session_allow_all = False
        self.has_sent_message = False
        self.chat_view.settings().set(CHAT_SESSION_ID, None)

        # Show reset message in the history
        reset_msg = f"\n\n{PACKAGE_NAME} session reset...\n"
        self.chat_view.run_command("term_chat_output_append", {"text": reset_msg})

        cwd = get_best_dir(self.chat_view)
        if cwd:
            self.chat_view.run_command("term_chat_output_append", {"text": f"cwd: {cwd}\n\n"})

        # Reset the agent (disconnect and reconnect)
        self.agent_thread.reset()

    def switch_agent(self, new_agent_provider):
        """Stop the current agent thread and start a new one with the given provider."""
        self.stop_loading()
        self.session_allow_all = False
        self.has_sent_message = False
        # Clear stale models and session_id from previous agent
        self.available_models = []
        self.chat_view.settings().set(CHAT_SESSION_ID, None)
        self.clear_prompt_buttons()
        self.artifact.clear()

        if self.agent_thread:
            self.agent_thread.stop()
            self.agent_thread = None

        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        # Update available agents
        self.available_agents = get_available_agents(settings)
        if new_agent_provider not in self.available_agents:
            self.chat_view.run_command("term_chat_output_append", {"text": f"\n\n⚠️ Error: Agent '{new_agent_provider}' not found on system.\n\n"})
            return

        cli_path = settings.get(f"{new_agent_provider}_command") or None
        model = self.window.settings().get(f"chatview_model_{new_agent_provider}") or None

        disallowed_tools = self._get_disallowed_tools(settings)

        anthropic_config = {
            "model": model,
            "plan_mode": self.window.settings().get(CHAT_PLAN_MODE) == PlanMode.PLANNING.value,
            "allowed_tools": settings.get("allowed_tools"),
            "disallowed_tools": disallowed_tools,
            "agent_provider": new_agent_provider,
            "approve_mode": self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value),
            "env": settings.get("env", {}),
            "debug_agent_message": settings.get("debug_agent_message", False)
        }

        if new_agent_provider == "codex":
            self.message_processor = CodexMessageProcessor(self)
        elif new_agent_provider == "pi":
            self.message_processor = PiMessageProcessor(self)
        else:
            self.message_processor = ClaudeMessageProcessor(self)

        cwd = get_best_dir(self.chat_view)
        self.agent_thread = AgentThread(
            cwd, self._handle_agent_message, cli_path=cli_path, anthropic_config=anthropic_config
        )
        self.agent_thread.start()
        LOG.info(f"Switched agent to: {new_agent_provider}")

        switch_msg = f"\n\n[Switched agent to: {new_agent_provider}]\n\n"
        self.chat_view.run_command("term_chat_output_append", {"text": switch_msg})

    def reload_agent(self, plan_mode=None, session_id_override=None, quiet=False):
        """Restart the current agent process, optionally with a new plan mode or resuming session."""
        self.stop_loading()

        # Get current session_id to resume if possible
        if session_id_override is not None:
            old_session_id = session_id_override
        elif self.agent_thread:
            old_session_id = self.agent_thread.session_id
        else:
            old_session_id = None

        current_agent_provider = self.window.settings().get(CHAT_AGENT, "claude")

        if self.agent_thread:
            self.agent_thread.stop()
            self.agent_thread = None

        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        # Update available agents
        self.available_agents = get_available_agents(settings)
        if current_agent_provider not in self.available_agents:
            self.chat_view.run_command("term_chat_output_append", {"text": f"\n\n⚠️ Error: Agent '{current_agent_provider}' not found on system.\n\n"})
            return

        cli_path = settings.get(f"{current_agent_provider}_command") or None
        model = self.window.settings().get(f"chatview_model_{current_agent_provider}") or None

        if plan_mode is None:
            plan_mode = self.plan_mode

        disallowed_tools = self._get_disallowed_tools(settings)

        anthropic_config = {
            "model": model,
            "plan_mode": plan_mode == PlanMode.PLANNING,
            "allowed_tools": settings.get("allowed_tools"),
            "disallowed_tools": disallowed_tools,
            "agent_provider": current_agent_provider,
            "approve_mode": self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value),
            "session_id": old_session_id,
            "env": settings.get("env", {}),
            "debug_agent_message": settings.get("debug_agent_message", False)
        }

        cwd = get_best_dir(self.chat_view)
        self.agent_thread = AgentThread(
            cwd, self._handle_agent_message, cli_path=cli_path, anthropic_config=anthropic_config
        )
        self.agent_thread.start()
        LOG.info(f"Reconnected agent: {current_agent_provider} (resume: {bool(old_session_id)})")

        if not quiet:
            if old_session_id:
                session_info = self._fetch_session_info(current_agent_provider, old_session_id, cwd)
                self._append_resume_banner(current_agent_provider, old_session_id, session_info)
            else:
                self.chat_view.run_command("term_chat_output_append", {"text": f"\n\n[reconnecting agent...]\n\n"})

    def _replay_prompt(self, prompt_text):
        """Append a previously-submitted prompt with gutter highlight, as if the user had just sent it."""
        text = f"{PROMPT_PREFIX}{prompt_text}\n"
        pos_before = get_input_start(self.chat_view, 0) - 1
        self.chat_view.run_command("term_chat_output_append", {"text": text})
        pos_after = get_input_start(self.chat_view, 0) - 1
        region_start = pos_before + len(PROMPT_PREFIX)
        region_end = pos_after - 1  # exclude trailing \n
        if region_end > region_start:
            self.add_prompt_highlight(sublime.Region(region_start, region_end))

    def _fetch_session_info(self, agent, session_id, cwd):
        """Fetch session metadata and last turn for the given agent. Returns a unified dict or None.

        Keys: summary (str|None), mtime (float), prompt (str|None), response (str|None).
        """
        if agent == "codex":
            info = get_codex_session_info(session_id, cwd)
            if info:
                return {"summary": info.get("summary"), "mtime": info.get("updated_at", 0),
                        "prompt": info.get("prompt"), "response": info.get("response")}
            return None

        if agent == "claude":
            sessions = list_sessions_for_cwd(cwd)
            meta = next((s for s in sessions if s["session_id"] == session_id), None)
            tail = get_claude_session_tail(session_id, cwd)
            if meta or tail:
                return {"summary": (meta or {}).get("summary"), "mtime": (meta or {}).get("mtime", 0),
                        "prompt": (tail or {}).get("prompt"), "response": (tail or {}).get("response")}
            return None

        if agent == "pi":
            sessions = list_pi_sessions(cwd)
            meta = next((s for s in sessions if s["session_id"] == session_id), None)
            tail = get_pi_session_tail(session_id, cwd)
            if meta or tail:
                return {"summary": (meta or {}).get("summary"), "mtime": (meta or {}).get("mtime", 0),
                        "prompt": (tail or {}).get("prompt"), "response": (tail or {}).get("response")}
            return None

        return None

    def _append_resume_banner(self, agent, session_id, session_info):
        """Render resume banner and last turn to the chat view. Pure display — no I/O."""
        import datetime
        self.chat_view.run_command("term_chat_output_append",
            {"text": f"\n[Resuming session for {agent}]\n\n"})

        if session_info and (session_info.get("prompt") or session_info.get("response")):
            if session_info.get("prompt"):
                self._replay_prompt(session_info["prompt"])
                self.chat_view.run_command("term_chat_output_append", {"text": "\n"})
            if session_info.get("response"):
                self.chat_view.run_command("term_chat_output_append", {"text": session_info["response"] + "\n"})

        if session_info and session_info.get("mtime"):
            dt = datetime.datetime.fromtimestamp(session_info["mtime"]).strftime("%Y-%m-%d %H:%M")
            self.chat_view.run_command("term_chat_output_append",
                {"text": f"\n■ ResumeConversation ({session_id[:8]} : {dt})\n\n"})
        else:
            self.chat_view.run_command("term_chat_output_append",
                {"text": f"\n■ ResumeConversation ({session_id[:8]})\n\n"})

    def update_plan_mode(self, plan_mode):
        """Update the plan mode for the current session."""
        self.stop_loading()
        # support dynamic plan mode updates
        is_planning = (plan_mode == PlanMode.PLANNING)
        if self.agent_thread:
            self.agent_thread.update_config(plan_mode=is_planning)
        LOG.info(f"Dynamically updated plan mode to: {plan_mode}")


class TermChatCliCommand(sublime_plugin.WindowCommand):
    """
    A Sublime Text plugin command for calling the ChatView
    """
    def run(self, initial_msg="", send_immediate=False):
        # Try to find and focus existing chat view
        for view in self.window.views():
            if view.settings().get(CHAT_VIEW_FLAG, False):
                self.window.focus_view(view)
                # Reconnect if session was lost (e.g., after a restart)
                if self.window.id() not in chatview_clients:
                    _reconnect_chat_view(view)
                if initial_msg:
                    view.run_command("term_chat_input_prompt", {"text": initial_msg})
                return

        # Create a new view
        chat_view = self.window.new_file()
        chat_view.set_name(CHAT_VIEW_NAME)
        chat_view.set_scratch(True)
        chat_view.set_syntax_file(f"Packages/{PACKAGE_NAME}/chatview/ChatMD.sublime-syntax")

        chat_view.settings().set("draw_minimap", False)
        chat_view.settings().set("line_numbers", False)
        chat_view.settings().set("word_wrap", True)
        # Needed so the file-changes artifact can be expanded via the gutter
        chat_view.settings().set("fold_buttons", True)
        # Don't draw the NBSP fold terminator appended after the artifact list
        chat_view.settings().set("draw_unicode_white_space", "none")
        chat_view.settings().set(CHAT_VIEW_FLAG, True)

        shortcut = "Command+Enter" if sublime.platform() == "osx" else "Ctrl+Enter"
        welcome_text = "\nType your message and press %s to send.\n" % shortcut

        chat_view.run_command("append", {"characters": f"Starting {PACKAGE_NAME} agent\n"})

        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        share_folders = settings.get("share_workspace_folders", False)

        cwd = get_best_dir(chat_view)
        add_dirs = get_all_folders(chat_view) if share_folders else []

        if cwd:
            chat_view.run_command("append", {"characters": f"cwd: {cwd}\n"})

        chat_view.run_command("append", {"characters": welcome_text})

        # Set input start position
        set_input_start(chat_view, chat_view.size())

        # Create and start the ChatSession
        session = ChatSession(self.window, chat_view, cwd, add_dirs=add_dirs)
        window_id = self.window.id()
        chatview_clients[window_id] = session

        # Show initial prompt (this will also update the model phantom)
        chat_view.run_command("term_chat_input_prompt", {"text": initial_msg})


class TermChatSplitChatCommand(sublime_plugin.WindowCommand):
    """
    Splits the window and moves the chat view to the left-most group.
    """
    def is_visible(self, group=-1, index=-1):
        try:
            group, index = int(group), int(index)
        except (TypeError, ValueError):
            group, index = -1, -1
        # Called from tab context menu
        if group >= 0 and index >= 0:
            views = self.window.views_in_group(group)
            if index < len(views):
                view = views[index]
                return view.settings().get(CHAT_VIEW_FLAG, False)
            return False
        # Called from command palette
        for view in self.window.views():
            if view.settings().get(CHAT_VIEW_FLAG, False):
                return True
        return False

    def run(self, group=-1, index=-1):
        try:
            group, index = int(group), int(index)
        except (TypeError, ValueError):
            group, index = -1, -1
        # Try to find existing chat view
        chat_view = None
        for view in self.window.views():
            if view.settings().get(CHAT_VIEW_FLAG, False):
                chat_view = view
                break
        
        if chat_view:
            self._split_and_move(chat_view)
        else:
            sublime.status_message(f"No active {PACKAGE_NAME} found to split")

    def _split_and_move(self, view):
        window = self.window
        layout = window.get_layout()
        cols = layout.get("cols", [0.0, 1.0])
        
        if len(cols) < 3:
            # It's a single column layout. Let's split it into two columns with equal width.
            window.set_layout({
                "cols": [0.0, 0.5, 1.0],
                "rows": [0.0, 1.0],
                "cells": [[0, 0, 1, 1], [1, 0, 2, 1]]
            })
            
            # Since we just split, all existing views are in group 0.
            # Move all views EXCEPT the chat view to group 1 (right side).
            for v in list(window.views_in_group(0)):
                if v.id() != view.id():
                    window.set_view_index(v, 1, 0)
            
            
        # Ensure the chat view is in the left-most group (group 0)
        window.set_view_index(view, 0, 0)
        # Focus the chat view
        window.focus_group(0)
        window.focus_view(view)


class TermChatSendInputCommand(sublime_plugin.TextCommand):
    """
    Handles the input submission (bound to Ctrl+Enter).
    """
    def run(self, edit):
        window = self.view.window()
        if not window:
            return

        window_id = window.id()
        if window_id not in chatview_clients:
            sublime.status_message(f"No active {PACKAGE_NAME} session found")
            return

        editable_start = input_editable_start(self.view)
        user_input = self.view.substr(sublime.Region(editable_start, self.view.size())).strip()

        if not user_input:
            return

        if sublime.platform() == "osx":
            sublime.status_message("Sending... (Cmd+Esc to stop)")
        else:
            sublime.status_message("Sending... (Shift+Esc to stop)")

        # Materialize the phantom marker as text so the transcript keeps the
        # "❯ " prefix (replay and rewind cut_point math rely on it)
        self.view.insert(edit, editable_start, "❯ ")
        input_region = sublime.Region(editable_start + 2, self.view.size())

        # Show input text and next prompt (simulated local echo/confirmation)
        self.view.run_command("term_chat_input_prompt", {"text": ""})

        # Send to session
        session = chatview_clients[window_id]
        session.history.append(user_input)
        session.history_index = len(session.history)
        session.history_stash = ""
        session.send_input(user_input, region=input_region)
        LOG.info(f"User enter prompt {user_input}")


class TermChatHistoryUpCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        if not window or window.id() not in chatview_clients:
            return

        session = chatview_clients[window.id()]
        editable_start = input_editable_start(self.view)

        # History navigation
        if session.history_index == len(session.history):
            # Stash current input
            current_input_region = sublime.Region(editable_start, self.view.size())
            session.history_stash = self.view.substr(current_input_region)

        if session.history_index > 0:
            session.history_index -= 1
            self._replace_input(edit, session.history[session.history_index], editable_start)

    def _replace_input(self, edit, text, start_point):
        region = sublime.Region(start_point, self.view.size())
        self.view.replace(edit, region, text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self.view.size()))
        self.view.show(self.view.size())


class TermChatHistoryDownCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        window = self.view.window()
        if not window or window.id() not in chatview_clients:
            return

        session = chatview_clients[window.id()]

        # History navigation
        if session.history_index < len(session.history):
            session.history_index += 1

            text_to_show = ""
            if session.history_index == len(session.history):
                text_to_show = session.history_stash
            else:
                text_to_show = session.history[session.history_index]

            editable_start = input_editable_start(self.view)
            self._replace_input(edit, text_to_show, editable_start)

    def _replace_input(self, edit, text, start_point):
        region = sublime.Region(start_point, self.view.size())
        self.view.replace(edit, region, text)
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(self.view.size()))
        self.view.show(self.view.size())



class ChatViewListener(sublime_plugin.EventListener):
    def on_activated_async(self, view):
        """
        Refresh input marker and reconnect chat view if orphaned when view gains focus.
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False):
            return

        window = view.window()
        if not window:
            return

        window_id = window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            if session.chat_view.id() == view.id():
                session.input_marker.update()
                session.model_phantom.update()
                LOG.debug("refresh chat view input phantom")
                return

        sublime.set_timeout(lambda: _reconnect_chat_view(view), 100)

    def on_load(self, view):
        window = view.window()
        if not window:
            return

        # reconnect restored chat view
        if view.settings().get(CHAT_VIEW_FLAG, False):
            if window.id() not in chatview_clients:
                sublime.set_timeout(lambda: _reconnect_chat_view(view), 100)
            return

        # redirect non-chat views away from the chat group (split layout only)
        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        if not settings.get("dedicated_chat_pane", True):
            return

        if window.num_groups() <= 1 or window.id() not in chatview_clients:
            return

        # Ignore widgets like the console or input panels
        if view.settings().get('is_widget'):
            return

        group, _ = window.get_view_index(view)
        if group == -1:
            return

        # Check if a ChatView occupies this group, then redirect to any other group
        for v in window.views_in_group(group):
            if v.settings().get(CHAT_VIEW_FLAG, False):
                for g in range(window.num_groups()):
                    if g != group:
                        window.set_view_index(view, g, 0)
                        window.focus_view(view)
                        break
                break

    def on_close(self, view):
        """
        Cleanup session when the chat view is closed.
        """
        if view.name() == CHAT_VIEW_NAME:
            window = view.window()
            if window is None:
                window = sublime.active_window()

            if window is not None:
                window_id = window.id()
                if window_id in chatview_clients:
                    try:
                        chatview_clients[window_id].stop()
                    except Exception:
                        pass
                    del chatview_clients[window_id]
                    LOG.info(f"Exit ChatView CLI for window {window_id}")
            LOG.info("ChatView closed")

    def on_selection_modified(self, view):
        """
        Restrict cursor movement to the editable area.
        Allows selecting history for copy, but prevents placing the caret in history.
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False) and view.name() != CHAT_VIEW_NAME:
            return
        if not view.settings().has(CHAT_INPUT_START):
            return

        editable_start = input_editable_start(view)

        new_sel = []
        changed = False

        for sel in view.sel():
            # Only restrict empty regions (cursor carets), allowing user to select history to copy
            if sel.empty() and sel.begin() < editable_start:
                new_sel.append(sublime.Region(editable_start))
                changed = True
            else:
                new_sel.append(sel)

        if changed:
            view.sel().clear()
            view.sel().add_all(new_sel)

    def _redirect_cursor(self, view):
        """Helper to move cursor to the end of the view."""
        end_pos = view.size()
        view.sel().clear()
        view.sel().add(sublime.Region(end_pos))
        view.show(end_pos)

    def on_text_command(self, view, command_name, args):
        """Intercept text commands to protect content before prompt area."""
        # Double-click in a diff view → open the actual file
        abs_path = view.settings().get(DIFF_VIEW_PATH_KEY)
        if (abs_path and command_name == "drag_select" and args
                and args.get("by") == "words"
                and not args.get("extend", False)
                and not args.get("additive", False)):
            point = args.get("event", {}).get("x"), args.get("event", {}).get("y")
            click_point = (
                view.window_to_text((point[0], point[1]))
                if point[0] is not None and point[1] is not None
                else None
            )
            if click_point is not None:
                if diff_view_click(view, abs_path, click_point):
                    return ("noop", {})

        # Only monitor ChatView chat views
        if not view.settings().get(CHAT_VIEW_FLAG, False) and view.name() != CHAT_VIEW_NAME:
            return None

        # Double-click on a tool call file line → open the file instead of selecting the word
        # Also handles artifact file name lines → open the recorded diff view
        if (command_name == "drag_select" and args
                and args.get("by") == "words"
                and not args.get("extend", False)
                and not args.get("additive", False)):
            window = view.window()
            if window and window.id() in chatview_clients:
                session = chatview_clients[window.id()]
                point = args.get("event", {}).get("x"), args.get("event", {}).get("y")
                click_point = (
                    view.window_to_text((point[0], point[1]))
                    if point[0] is not None and point[1] is not None
                    else None
                )
                if click_point is not None:
                    line_text = view.substr(view.line(click_point))
                    if session.message_processor.open_tool_file(line_text, window, view=view, point=click_point):
                        return ("noop", {})
                    if session.open_artifact_diff_at(click_point):
                        return ("noop", {})

        editable_start = input_editable_start(view)

        # Handle move commands for history navigation
        if command_name == "move" and args and args.get("by") == "lines":
            # Don't intercept if auto-complete is active, so user can select items
            if not view.is_auto_complete_visible():
                is_up = not args.get("forward", True)
                if len(view.sel()) > 0:
                    sel = view.sel()[0]
                    if sel.empty():
                        if is_up:
                            row_sel, _ = view.rowcol(sel.begin())
                            row_start, _ = view.rowcol(editable_start)
                            if row_sel == row_start:
                                return ("term_chat_history_up", {})
                        else:
                            row_sel, _ = view.rowcol(sel.end())
                            row_last, _ = view.rowcol(view.size())
                            if row_sel == row_last:
                                return ("term_chat_history_down", {})

        # Handle deletion commands - block if they affect content before prompt
        delete_commands = ("left_delete", "right_delete", "delete_word", "delete_word_backward",
                          "delete_to_mark", "run_macro_file", "cut",)

        if command_name in delete_commands:
            for sel in view.sel():
                # Block deletion if cursor is in protected area
                if sel.begin() < editable_start:
                    self._redirect_cursor(view)
                    return ("noop", {})

                # Special case for backspace: if at the exact boundary,
                # it deletes backward into protected area
                if (command_name in ("left_delete", "delete_word_backward") and
                    sel.empty() and sel.begin() == editable_start):
                    self._redirect_cursor(view)
                    return ("noop", {})

        # Handle insert/modification commands - redirect to end if in protected area
        mod_commands = ("insert", "paste", "insert_characters", "insert_snippet",
                       "append", "yank", "paste_and_indent", "clipboard_history_paste")

        if command_name in mod_commands:
            should_redirect = False
            for sel in view.sel():
                if sel.begin() < editable_start:
                    should_redirect = True
                    break

            if should_redirect:
                self._redirect_cursor(view)
                return ("noop", {})

        return None

    def on_query_completions(self, view, prefix, locations):
        """
        Provide filename completions when typing '@' in the prompt area.
        Shows three categories: open files, current directory files, and subdirectories.
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False):
            return None

        # Check if in editable area
        editable_start = input_editable_start(view)
        pos = locations[0]

        if pos < editable_start:
            return None

        # Check if the prefix is preceded by '@'
        trigger_pos = pos - len(prefix) - 1
        if trigger_pos < 0 or view.substr(trigger_pos) != '@':
            return None

        completions = []
        window = view.window()
        if not window:
            return None

        # Get current directory (first workspace folder)
        current_dir = None
        folders = window.folders()
        if folders:
            current_dir = folders[0]

        # Category 1: Currently open files
        seen_files = set()
        for v in window.views():
            file_path = v.file_name()
            if not file_path:
                continue

            # Skip the chat view itself
            if v.settings().get(CHAT_VIEW_FLAG, False):
                continue

            file_name = os.path.basename(file_path)
            if file_name in seen_files:
                continue

            seen_files.add(file_name)

            # Use relative path as hint if available
            rel_path = file_name
            if current_dir and file_path.startswith(current_dir):
                rel_path = os.path.relpath(file_path, current_dir)

            completions.append(sublime.CompletionItem(
                file_name,
                annotation=f"📂 {rel_path}",
                completion=file_name,
                kind=sublime.KIND_VARIABLE
            ))

        # Category 2: Files in current directory
        if current_dir and os.path.isdir(current_dir):
            try:
                for item in os.listdir(current_dir):
                    item_path = os.path.join(current_dir, item)
                    if os.path.isfile(item_path) and not item.startswith('.'):
                        if item not in seen_files:
                            seen_files.add(item)
                            completions.append(sublime.CompletionItem(
                                item,
                                annotation="📄 current dir",
                                completion=item,
                                kind=sublime.KIND_AMBIGUOUS
                            ))
            except OSError:
                pass

        # Category 3: Subdirectories in current directory
        if current_dir and os.path.isdir(current_dir):
            try:
                for item in os.listdir(current_dir):
                    item_path = os.path.join(current_dir, item)
                    if os.path.isdir(item_path) and not item.startswith('.'):
                        completions.append(sublime.CompletionItem(
                            item + "/",
                            annotation="📁 subdirectory",
                            completion=item + "/",
                            kind=sublime.KIND_NAMESPACE
                        ))
            except OSError:
                pass

        return sublime.CompletionList(completions, flags=sublime.INHIBIT_WORD_COMPLETIONS)

    def on_hover(self, view, point, hover_zone):
        """Show rewind confirm phantom when hovering over a prompt gutter dot."""
        if hover_zone != sublime.HOVER_GUTTER:
            return
        if not view.settings().get(CHAT_VIEW_FLAG, False):
            return
        window = view.window()
        if not window or window.id() not in chatview_clients:
            return
        session = chatview_clients[window.id()]

        row, _ = view.rowcol(point)
        for i, (region, uuid, _) in enumerate(session.prompt_regions):
            start_row, _ = view.rowcol(region.begin())
            end_row, _ = view.rowcol(region.end())
            if start_row <= row <= end_row:
                if not uuid:
                    return
                if session.rewind_confirm_panel.visible:
                    return
                def on_confirm(idx=i):
                    sublime.status_message(f"Rewinding to prompt {idx + 1}...")
                    session.rewind_to_prompt(idx)
                session.rewind_confirm_panel.show(region, i, on_confirm)
                return

    def on_modified_async(self, view):
        """
        Trigger autocompletion immediately when '@' is typed.
        """
        if not view.settings().get(CHAT_VIEW_FLAG, False):
            return

        # Check if the last character typed was '@'
        sel = view.sel()
        if not sel:
            return

        pos = sel[0].begin()
        if pos <= 0:
            return

        # Check if in editable area
        editable_start = input_editable_start(view)
        if pos < editable_start:
            return

        last_char = view.substr(pos - 1)
        if last_char == '@':
            # Run auto_complete command
            view.run_command("auto_complete", {
                "disable_auto_insert": True,
                "api_completions_only": True,
                "next_completion_if_showing": False
            })


class TermChatRewindTruncateCommand(sublime_plugin.TextCommand):
    """
    Erase everything from cut_point to end of the view, then set up a
    fresh prompt area. rewind_text, when provided, is placed in the prompt
    area (the original user input at the rewind point); otherwise the current
    in-progress input is preserved.
    """
    def run(self, edit, cut_point, rewind_text=None):
        if rewind_text is None:
            # Preserve whatever the user has typed in the current input area.
            editable_start = input_editable_start(self.view)
            rewind_text = self.view.substr(sublime.Region(editable_start, self.view.size())).strip()

        if cut_point < self.view.size():
            self.view.erase(edit, sublime.Region(cut_point, self.view.size()))

        # Two newlines: input start points at the second one, which precedes
        # the input line. It must exist before set_input_start anchors on it.
        self.view.insert(edit, self.view.size(), "\n\n")
        set_input_start(self.view, self.view.size() - 1)

        window = self.view.window()
        if window and window.id() in chatview_clients:
            chatview_clients[window.id()].model_phantom.update()

        if rewind_text:
            self.view.insert(edit, self.view.size(), rewind_text)
        end = self.view.size()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(end))
        self.view.show(end)

        if window and window.id() in chatview_clients:
            chatview_clients[window.id()].input_marker.update()


class TermChatOutputAppendCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        insert_at = get_input_start(self.view, 0) - 1
        inserted = self.view.insert(edit, insert_at, text)
        # The anchor region shifts with the insert; re-set to keep the
        # settings value in sync (and re-anchor if the anchor was missing).
        set_input_start(self.view, insert_at + inserted + 1)
        self.view.show(self.view.size())


class TermChatInputPromptCommand(sublime_plugin.TextCommand):

    def run(self, edit, text):
        # The last newline precedes the input line; it must exist before
        # set_input_start anchors on it.
        self.view.insert(edit, self.view.size(), "\n\n\n\n\n")
        set_input_start(self.view, self.view.size() - 1)

        # Update model phantom at new position
        window = self.view.window()
        if window and window.id() in chatview_clients:
            session = chatview_clients[window.id()]
            session.model_phantom.update()

        # Next input prompt (the ❯ itself is the InputPromptMarker phantom)
        if text:
            self.view.insert(edit, self.view.size(), text + " ")
        end = self.view.size()
        self.view.sel().clear()
        self.view.sel().add(sublime.Region(end))
        self.view.show(end)

        if window and window.id() in chatview_clients:
            chatview_clients[window.id()].input_marker.update()


class TermChatAddContextCommand(sublime_plugin.WindowCommand):
    """
    Command to add file context to the ChatView chat prompt.
    Called from the context menu (current view + selection) or sidebar (files/dirs args).
    Sidebar callers pass files=[...] with no line numbers; context menu uses active view + selection.
    In the chat view itself, the selected history text is quoted into the next prompt.
    """
    def run(self, files=[], dirs=[]):
        paths = files + dirs
        view = self.window.active_view()
        if paths:
            # Sidebar: insert @path for each selected file/dir, no line numbers
            tags = " ".join(f"@{p}" for p in paths)
            insert_text = tags + " "
        elif view and view.settings().get(CHAT_VIEW_FLAG, False):
            # Chat view: quote the selected history text as context for the
            # next prompt (there is no file to @-tag). Capture before the
            # selection gets snapped to the input line below.
            editable_start = input_editable_start(view)
            selected = "\n".join(
                view.substr(s) for s in view.sel()
                if not s.empty() and s.begin() < editable_start
            )
            if not selected.strip():
                return
            tags = "\n".join("> " + line for line in selected.splitlines())
            input_text = view.substr(sublime.Region(editable_start, view.size()))
            # A blank line always precedes and follows the quote block,
            insert_text = tags + "\n\n"
            trailing = len(input_text) - len(input_text.rstrip("\n"))
            needed = 2 if input_text else 1
            insert_text = "\n" * max(0, needed - trailing) + insert_text
        else:
            # Context menu: use active view + selection
            file_path = view.file_name() if view else None
            if file_path:
                sel = view.sel()[0]
                row_start, _ = view.rowcol(sel.begin())
                row_end, _ = view.rowcol(sel.end())
                if row_start == row_end:
                    tags = f"@{file_path}#L{row_start + 1}"
                else:
                    tags = f"@{file_path}#L{row_start + 1}-{row_end + 1}"
                insert_text = tags + " "
            else:
                # No active view, or a non-file view such as Terminus:
                # continue through the command without adding file context.
                tags = ""
                insert_text = ""

        chat_view = None
        for v in self.window.views():
            if v.settings().get(CHAT_VIEW_FLAG, False):
                chat_view = v
                break

        if not chat_view:
            self.window.run_command("term_chat_cli", {"initial_msg": tags})
        else:
            self.window.focus_view(chat_view)
            if not insert_text:
                return
            # Preserve the current cursor/selection when it is in the input
            # area. A history selection is allowed for copying/quoting, but a
            # programmatic insert there would bypass on_text_command and
            # modify history, so fall back to the end of the input instead.
            editable_start = input_editable_start(chat_view)
            selections = list(chat_view.sel())
            # Multiple selections would insert the same context more than once.
            if not (
                len(selections) == 1
                and selections[0].begin() >= editable_start
            ):
                chat_view.sel().clear()
                chat_view.sel().add(sublime.Region(chat_view.size()))
            chat_view.run_command("insert", {"characters": insert_text})
            chat_view.show(chat_view.sel()[0].end())


class TermChatPromptHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "prompt"

    def placeholder(self):
        return "Enter your prompt for ChatView..."

    def description(self, text):
        return f"{PACKAGE_NAME}: " + text if text else f"{PACKAGE_NAME} Prompt"


class TermChatPromptCommand(sublime_plugin.WindowCommand):
    def run(self, prompt):
        if not prompt:
            return

        # Try to find existing chat view
        chat_view = None
        for v in self.window.views():
            if v.settings().get(CHAT_VIEW_FLAG, False):
                chat_view = v
                break

        if chat_view:
            self.window.focus_view(chat_view)
            # Snap the selection out of the history area before inserting
            # (a copy-selection there would drop the text into history).
            chat_view.sel().clear()
            chat_view.sel().add(sublime.Region(chat_view.size()))
            chat_view.run_command("insert", {"characters": prompt})
            chat_view.run_command("term_chat_send_input")
        else:
            # Start a new session and send immediately
            self.window.run_command("term_chat_cli", {
                "initial_msg": prompt,
                "send_immediate": True
            })

    def input(self, args):
        return TermChatPromptHandler()


class TermChatSetWorkspaceInputHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "path"

    def placeholder(self):
        return "Enter workspace path..."

    def description(self, text):
        return "Set WorkSpace: " + text if text else "Set WorkSpace Path"

    def validate(self, text):
        return os.path.isdir(os.path.expanduser(text))


class TermChatSetWorkspaceInputCommand(sublime_plugin.WindowCommand):
    """
    Command that asks for input and then calls TermChatSetWorkspaceCommand.
    """
    def run(self, path):
        if path:
            full_path = os.path.expanduser(path)
            # Delegate to the existing command
            self.window.run_command("term_chat_set_workspace", {"dirs": [full_path]})

    def input(self, args):
        return TermChatSetWorkspaceInputHandler()


class TermChatSetWorkspaceCommand(sublime_plugin.WindowCommand):
    """
    Sets the active workspace for ChatView based on the selected folder in sidebar.
    """
    def run(self, files=[], dirs=[]):
        # Handle both files and dirs arguments, though typically called with dirs from sidebar
        paths = files + dirs
        LOG.info(f"set workspace path {paths}")
        if not paths:
            return

        # Find the first valid directory
        target_dir = None
        for path in paths:
            if os.path.isdir(path):
                target_dir = path
                break
            else:
                # If it's a file, use its parent directory
                parent = os.path.dirname(path)
                if os.path.isdir(parent):
                    target_dir = parent
                    break

        if target_dir:
            self.window.settings().set(CHAT_WORKSPACE, target_dir)
            sublime.status_message(f"ChatView Dir set to: {target_dir}")
        else:
            sublime.status_message(f"No valid directory for {PACKAGE_NAME} Workspace")

    def is_visible(self, files=[], dirs=[]):
        # Show only if at least one item is selected
        return bool(files or dirs)


class TermChatClearSessionCommand(sublime_plugin.WindowCommand):
    """
    Clears the current chat session by disconnecting and reconnecting the agent.
    This resets the conversation history similar to Claude Code's reset session.
    """
    def run(self):
        window_id = self.window.id()
        if window_id not in chatview_clients:
            sublime.status_message("No active ChatView session found")
            return

        # Reset the session (disconnect and reconnect agent)
        session = chatview_clients[window_id]
        session.reset_session()
        sublime.status_message("Resetting chat session...")
        LOG.info("Resetting chat session via disconnect/reconnect")

    def is_enabled(self):
        # Only enable if there's an active session
        return self.window.id() in chatview_clients


class TermChatResumeSessionCommand(sublime_plugin.WindowCommand):
    """
    Shows a quick-panel listing past sessions for the current workspace and agent.
    Works whether or not a ChatView is already open: when none exists, opening
    the selected session creates one first.
    Supports both claude and codex agents.
    """

    _PREVIEW_LEN = 80

    def _get_cwd(self, session):
        if session is not None:
            return get_best_dir(session.chat_view)
        custom_cwd = self.window.settings().get(CHAT_WORKSPACE)
        if custom_cwd and os.path.isdir(custom_cwd):
            return custom_cwd
        folders = self.window.folders()
        return folders[0] if folders else ""

    def _get_agent(self, session):
        if session is not None:
            return session.window.settings().get(CHAT_AGENT, "claude")
        return self.window.settings().get(CHAT_AGENT, "claude")

    def run(self):
        import datetime
        window_id = self.window.id()
        session = chatview_clients.get(window_id)
        agent = self._get_agent(session)
        cwd = self._get_cwd(session)

        if agent == "codex":
            raw = list_codex_sessions(cwd)
            sessions = [{"session_id": s["session_id"], "summary": s["summary"],
                         "mtime": s["updated_at"]} for s in raw]
            placeholder = "Resume previous Codex session"
        elif agent == "pi":
            sessions = list_pi_sessions(cwd)
            placeholder = "Resume previous Pi session"
        else:
            sessions = list_sessions_for_cwd(cwd)
            placeholder = "Resume previous Claude session"

        if not sessions:
            sublime.status_message("No past sessions found for this workspace")
            return

        current_session_id = session.agent_thread.session_id if session and session.agent_thread else None

        items = []
        for s in sessions:
            sid = s["session_id"]
            summary = s["summary"] or "(empty)"
            if len(summary) > self._PREVIEW_LEN:
                summary = summary[:self._PREVIEW_LEN] + "…"
            dt = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
            marker = " ●" if sid == current_session_id else ""
            items.append([f"{summary}{marker}", f"{sid[:8]}  {dt}"])

        def on_select(index):
            if index < 0:
                return
            chosen = sessions[index]
            chosen_id = chosen["session_id"]
            if chosen_id == current_session_id:
                sublime.status_message("Already on that session")
                return

            active_session = chatview_clients.get(window_id)
            if active_session is not None:
                active_session.chat_view.settings().set(CHAT_SESSION_ID, chosen_id)
                active_session.has_sent_message = True
                active_session.reload_agent(session_id_override=chosen_id, quiet=False)
            else:
                self.window.run_command("term_chat_cli")

                def _resume_after_open():
                    new_session = chatview_clients.get(window_id)
                    if new_session is None:
                        LOG.warning("[resume] chat view not ready after term_chat_cli")
                        return
                    new_session.chat_view.settings().set(CHAT_SESSION_ID, chosen_id)
                    new_session.has_sent_message = True
                    new_session.reload_agent(session_id_override=chosen_id, quiet=False)

                sublime.set_timeout(_resume_after_open, 0)

            sublime.status_message(f"Resuming session {chosen_id[:8]}…")

        self.window.show_quick_panel(items, on_select, placeholder=placeholder)

    def is_enabled(self):
        session = chatview_clients.get(self.window.id())
        agent = self._get_agent(session)
        return agent in ("claude", "codex", "pi")


class TermChatInterruptCommand(sublime_plugin.WindowCommand):
    """
    Interrupts the current chat session.
    """
    def run(self, confirm=False):
        window_id = self.window.id()
        if window_id not in chatview_clients:
            sublime.status_message("No active ChatView session found")
            return

        session = chatview_clients[window_id]
        if not session.loading_animation.is_loading:
            sublime.status_message("No active conversation to interrupt")
            return

        if confirm and not sublime.ok_cancel_dialog("Stop the running conversation?", "Stop"):
            return

        if session.agent_thread and session.agent_thread.agent:
            # The agent method is async, but we can't easily await it from run().
            # So we use asyncio.run_coroutine_threadsafe.
            try:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    session.agent_thread.agent.interrupt(),
                    session.agent_thread.loop
                )
                session.model_phantom.set_stopping(True)
                sublime.set_timeout(
                    lambda: session.chat_view.run_command(
                        "term_chat_output_append",
                        {"text": "\n■ Conversation interrupted\n"}
                    ),
                    1000
                )
                sublime.status_message("Interrupting agent")
                LOG.info(f"Interrupt conversation for window {window_id}")
            except Exception as e:
                session.model_phantom.set_stopping(False)
                LOG.error(f"Failed to interrupt agent: {e}")

    def is_enabled(self):
        # Only enable if there's an active session
        return self.window.id() in chatview_clients


class TermChatSetModelListHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_model=None):
        self.current_model = current_model

    def name(self):
        return "model"

    def list_items(self):
        # Get the active ChatSession to access available_models
        window = sublime.active_window()
        if window:
            window_id = window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                if session.available_models:
                    # Return list of tuples: (display_text, value)
                    items = [
                        (f"{m['displayName']} - {m['description']}", m['value'])
                        for m in session.available_models
                    ]

                    # Move current model to the front
                    if self.current_model:
                        for i, item in enumerate(items):
                            if item[1] == self.current_model:
                                items.insert(0, items.pop(i))
                                break
                    return items

        return []

    def placeholder(self):
        return "Select a model"

    def description(self, value, text):
        return f"Set Model: {value}"


class TermChatSetModelTextHandler(sublime_plugin.TextInputHandler):
    def name(self):
        return "model"

    def placeholder(self):
        return "Enter model name (e.g., sonnet, opus, haiku)"

    def description(self, text):
        return "Set Model: " + text if text else "Set Model Name"

    def validate(self, text):
        return bool(text.strip())


class TermChatAgentProviderInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_agent=None, available_agents=None):
        self.current_agent = current_agent
        self.available_agents = available_agents or ["claude", "codex"]

    def name(self):
        return "agent"

    def list_items(self):
        labels = {
            "claude": "claude: (Claude Code CLI by Anthropic)",
            "codex":  "codex: (Codex CLI by OpenAI)",
            "pi":     "pi: (Pi Coding Agent by Earendil)",
        }
        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        items = []
        for agent in ("claude", "codex", "pi"):
            if agent not in self.available_agents:
                continue
            path = find_existing_cli(agent, settings) or ""
            items.append(sublime.ListInputItem(labels[agent], agent, annotation=path))

        if not items:
            items.append(sublime.ListInputItem("No agent CLI found", ""))

        if self.current_agent:
            for i, item in enumerate(items):
                if item.value == self.current_agent:
                    items.insert(0, items.pop(i))
                    break

        return items

    def placeholder(self):
        return "Select agent provider"

    def description(self, agent, text):
        return f"Agent: {agent}" if agent else "No agent available"


class TermChatSetAgentCommand(sublime_plugin.WindowCommand):
    """
    Sets the agent provider for ChatView sessions in the current window.
    """
    def run(self, agent):
        if agent:
            current_agent = self.window.settings().get(CHAT_AGENT, "claude")
            self.window.settings().set(CHAT_AGENT, agent)
            sublime.status_message(f"ChatView agent provider set to: {agent}")

            window_id = self.window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                if agent != current_agent:
                    session.switch_agent(agent)
                session.model_phantom.update()

    def input(self, args):
        current_agent = self.window.settings().get(CHAT_AGENT, "claude")
        window_id = self.window.id()

        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            # use cached agents if available
            if hasattr(session, "available_agents") and session.available_agents:
                return TermChatAgentProviderInputHandler(current_agent, session.available_agents)

        # fetch from system if no session exists or agents not cached
        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        available_agents = get_available_agents(settings)

        if window_id in chatview_clients:
            chatview_clients[window_id].available_agents = available_agents

        return TermChatAgentProviderInputHandler(current_agent, available_agents)


class TermChatSetModelCommand(sublime_plugin.WindowCommand):
    """
    Sets the model for ChatView sessions in the current window.
    """
    def run(self, model):
        if model:
            # Store model under provider-specific key
            agent_provider = self.window.settings().get(CHAT_AGENT, "claude")
            self.window.settings().set(f"chatview_model_{agent_provider}", model.strip())
            # Also update the display key
            self.window.settings().set(CHAT_MODEL, model.strip())
            sublime.status_message(f"{PACKAGE_NAME} model set to: {model}")

            # Update the model phantom if session exists
            window_id = self.window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                session.model_phantom.update()
                # Update the running agent directly
                if session.agent_thread:
                    session.agent_thread.update_config(model=model.strip())

    def input(self, args):
        # Check if ChatSession has available models
        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            if session.available_models:
                # Get current model to highlight it
                agent_provider = self.window.settings().get(CHAT_AGENT, "claude")
                current_model = self.window.settings().get(f"chatview_model_{agent_provider}")
                # Use ListInputHandler for dropdown selection
                return TermChatSetModelListHandler(current_model)

        # Fallback to TextInputHandler for manual input
        return TermChatSetModelTextHandler()


class TermChatPlanModeInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_mode=None):
        self.current_mode = current_mode

    def name(self):
        return "mode"

    def list_items(self):
        items = [
            ("fast: (executing task straightly, complete faster)", PlanMode.FAST.value),
            ("planning: (make plan and todo-list before execute)", PlanMode.PLANNING.value),
        ]

        if self.current_mode:
            for i, item in enumerate(items):
                if item[1] == self.current_mode:
                    items.insert(0, items.pop(i))
                    break

        return items

    def placeholder(self):
        return "Select mode"

    def description(self, mode, text):
        return f"Plan Mode: {mode}"


class TermChatApproveModeInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, current_mode=None):
        self.current_mode = current_mode

    def name(self):
        return "mode"

    def list_items(self):
        items = [
            ("default: ask for confirmation on tool call", ApproveMode.DEFAULT.value),
            ("allow-edit: auto-approve file edits", ApproveMode.ALLOW_EDIT.value),
            ("accept-all: accept all without asking", ApproveMode.ACCEPT_ALL.value),
        ]

        if self.current_mode:
            # Find the item with the current mode and move it to the front
            for i, item in enumerate(items):
                if item[1] == self.current_mode:
                    items.insert(0, items.pop(i))
                    break

        return items

    def placeholder(self):
        if self.current_mode:
            return f" ( {self.current_mode} ); select approve mode on tool call"
        return "select approve mode on tool call"


class TermChatSetApproveModeCommand(sublime_plugin.WindowCommand):
    """Set permission approve mode for the current ChatView session."""
    def run(self, mode):
        self.window.settings().set(CHAT_APPROVE_MODE, mode)
        sublime.status_message(f"Approve mode set to: {mode}")
        
        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            if self.window.settings().get(CHAT_AGENT) == "pi":
                session.sync_pi_approve_mode(mode)

    def input(self, args):
        if "mode" not in args:
            current_mode = self.window.settings().get(CHAT_APPROVE_MODE, ApproveMode.ALLOW_EDIT.value)
            return TermChatApproveModeInputHandler(current_mode)
        return None


class TermChatTogglePlanModeCommand(sublime_plugin.WindowCommand):
    """
    Toggle plan mode for the current ChatView session.
    """
    def run(self, mode):
        # mode is a string value from PlanMode
        if mode == PlanMode.PLANNING.value:
            plan_mode_enum = PlanMode.PLANNING
        else:
            plan_mode_enum = PlanMode.FAST

        self.window.settings().set(CHAT_PLAN_MODE, plan_mode_enum.value)

        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            session.model_phantom.update()
            # Update plan mode (reconnects for Claude, dynamic update for Codex)
            session.update_plan_mode(plan_mode=plan_mode_enum)

        status = "planning" if plan_mode_enum == PlanMode.PLANNING else "fast"
        sublime.status_message(f"Plan mode set to: {status}")

    def input(self, args):
        if "mode" not in args:
            current_mode = self.window.settings().get(CHAT_PLAN_MODE, PlanMode.FAST.value)
            return TermChatPlanModeInputHandler(current_mode)
        return None


class TermChatPermissionActionCommand(sublime_plugin.WindowCommand):
    """
    Handle permission actions from the phantom UI.
    """
    def run(self, action, request_id):
        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            session._handle_permission_decision(request_id, action)


class TermChatImplementPlanCommand(sublime_plugin.WindowCommand):
    """
    Trigger the 'Implement the plan.' steering message.
    """
    def run(self):
        window_id = self.window.id()
        if window_id in chatview_clients:
            session = chatview_clients[window_id]
            sublime.status_message("Implementing plan...")

            # Get position before appending
            input_start = get_input_start(session.chat_view, 0)
            # Display implementation message in chat history
            session.chat_view.run_command("term_chat_output_append", {"text": "\nimplement the plan\n\n"})

            # Add gutter highlight mimicking user prompt
            highlight_region = sublime.Region(input_start, input_start)
            session.add_prompt_highlight(highlight_region)

            # For Codex, we must explicitly exit Plan mode to execute.
            # We use proceed_plan=True to signal CodexAgent to switch mode to 'default' for this turn.
            session.steer("Implement the plan.", proceed_plan=True)

            # Force the UI out of plan mode so subsequent turns don't trigger planning.
            self.window.run_command("term_chat_toggle_plan_mode", {"mode": "fast"})


class TermChatInstallAgentCommand(sublime_plugin.WindowCommand):
    def run(self, agent=None):
        if agent is None:
            return

        def _on_success(agent, display_name, write_fn):
            settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
            found = find_existing_cli(agent, settings)
            if found:
                write_fn(f"\n{display_name} installed successfully.\n  Installed at: {found}\n")
            else:
                write_fn(f"\n{display_name} installed successfully.\n")
            window_id = self.window.id()
            if window_id in chatview_clients:
                session = chatview_clients[window_id]
                session.available_agents = get_available_agents(settings)
                # Session was created but aborted early because no CLI was found —
                # start the agent thread now that one is available.
                if session.agent_thread is None and session.available_agents:
                    session.switch_agent(agent)
            sublime.status_message(f"{display_name} installed.")

        run_install(self.window, agent, _on_success)

    def input(self, args):
        return TermChatInstallAgentInputHandler()


class TermChatInstallAgentInputHandler(sublime_plugin.ListInputHandler):
    def name(self):
        return "agent"

    def list_items(self):
        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        return get_agent_list_items(settings)
