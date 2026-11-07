"""
TermMate Chat Thread and Multi-Session Management.
"""
import enum
import logging
import os
import re

import sublime
import sublime_plugin

LOG = logging.getLogger("TermMate")

PACKAGE_NAME = "TermMate"
CHAT_VIEW_FLAG = "chatview_chat"
CHAT_VIEW_NAME = "Chat View"
CHAT_AGENT = "chatview_agent_provider"

# Thread settings keys
CHAT_ROLE_KEY = "chatview_role"
CHAT_TASK_COUNTER = "chatview_task_counter"
CHAT_TASK_ID = "chatview_task_id"
CHAT_TASK_TITLE = "chatview_task_title"


class SessionRole(enum.Enum):
    PRIMARY = "primary"
    TASK = "task"


def is_chat_view(view):
    """Return True if view is a TermMate chat view (primary or task)."""
    if not view:
        return False
    return bool(
        view.settings().get(CHAT_VIEW_FLAG, False) or view.name() == CHAT_VIEW_NAME
    )


def get_chat_view_role(view):
    """Return SessionRole for a chat view, or None if not a chat view."""
    if not is_chat_view(view):
        return None
    role_val = view.settings().get(CHAT_ROLE_KEY)
    if role_val == SessionRole.TASK.value:
        return SessionRole.TASK
    return SessionRole.PRIMARY


def is_primary_chat_view(view):
    """Return True if view is the window's primary Chat View."""
    return get_chat_view_role(view) == SessionRole.PRIMARY


def is_task_chat_view(view):
    """Return True if view is an independent Task Chat view."""
    return get_chat_view_role(view) == SessionRole.TASK


class ChatThreadSet:
    """Manages active ChatSession instances, window bindings, and view routing."""

    def __init__(self):
        # view_id -> ChatSession (all primary & task sessions)
        self.sessions = {}
        # window_id -> view_id (primary chat view per window)
        self.window_primary_chat = {}
        # window_id -> view_id (most recent chat view per window)
        self.window_last_active_chat = {}
        # window_id -> ChatSession (primary session alias for compatibility)
        self.clients = {}

    def find_primary_chat_view(self, window):
        """Find the primary Chat View in the given window, if any."""
        if not window:
            return None
        primary_vid = self.window_primary_chat.get(window.id())
        if primary_vid is not None:
            for view in window.views():
                if view.id() == primary_vid:
                    return view
        for view in window.views():
            if is_primary_chat_view(view):
                return view
        return None

    def find_last_active_chat_view(self, window):
        """Find the most recently focused chat view in the given window, if any."""
        if not window:
            return None
        active_vid = self.window_last_active_chat.get(window.id())
        if active_vid is not None:
            for view in window.views():
                if view.id() == active_vid and is_chat_view(view):
                    return view
        return None

    def find_any_chat_view(self, window):
        """Find any open Chat View in the given window (primary or task)."""
        if not window:
            return None
        primary = self.find_primary_chat_view(window)
        if primary:
            return primary
        for view in window.views():
            if is_task_chat_view(view):
                return view
        for view in window.views():
            if is_chat_view(view):
                return view
        return None

    def find_target_chat_view_for_context(self, window):
        """
        Resolve target Chat View for adding context based on `add_context_target`:
        'primary' (default) or 'last_active'.
        """
        if not window:
            return None

        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        mode = settings.get("add_context_target", "primary")

        if mode == "last_active":
            active_view = window.active_view()
            if active_view and is_chat_view(active_view):
                return active_view
            last_chat = self.find_last_active_chat_view(window)
            if last_chat:
                return last_chat
            return self.find_any_chat_view(window)

        # Mode: "primary" (default) returns None if not open
        return self.find_primary_chat_view(window)

    def get_primary_session(self, window):
        """Return the primary ChatSession for the given window, or None."""
        if not window:
            return None
        primary_vid = self.window_primary_chat.get(window.id())
        if primary_vid is not None and primary_vid in self.sessions:
            return self.sessions[primary_vid]
        return self.clients.get(window.id())

    def get_active_or_primary_session(self, window, view_id=None):
        """Resolve session: explicit view_id -> focused chat view -> window primary session."""
        if view_id is not None and view_id in self.sessions:
            return self.sessions[view_id]
        if not window:
            return None
        active_view = window.active_view()
        if active_view and active_view.id() in self.sessions:
            return self.sessions[active_view.id()]
        return self.get_primary_session(window)

    def get_setting_for_session(self, window, session, key, default=None):
        """Read a setting from the session's view first, falling back to window settings."""
        if session and session.chat_view and session.chat_view.settings().has(key):
            return session.chat_view.settings().get(key)
        if window:
            return window.settings().get(key, default)
        return default

    def set_setting_for_session(self, window, session, key, value):
        """Write a setting to the session's view, and to window settings if primary."""
        if session and session.chat_view:
            session.chat_view.settings().set(key, value)
            if session.role == SessionRole.PRIMARY and window:
                window.settings().set(key, value)
        elif window:
            window.settings().set(key, value)

    def update_window_model_phantoms(self, window):
        """Update model phantoms for all chat sessions in the given window."""
        if not window:
            return
        win_id = window.id()
        for s in list(self.sessions.values()):
            if s.window and s.window.id() == win_id and s.model_phantom:
                s.model_phantom.update()

    def register(self, window, view, session):
        """Register a ChatSession in global stores."""
        view_id = view.id()
        self.sessions[view_id] = session
        if window:
            self.window_last_active_chat[window.id()] = view_id
        if session.role == SessionRole.PRIMARY and window:
            window_id = window.id()
            self.window_primary_chat[window_id] = view_id
            self.clients[window_id] = session
        self.update_window_model_phantoms(window)

    def unregister(self, view_id):
        """Remove a view's session and clean up window references."""
        session = self.sessions.pop(view_id, None)
        for win_id, primary_vid in list(self.window_primary_chat.items()):
            if primary_vid == view_id:
                del self.window_primary_chat[win_id]
                self.clients.pop(win_id, None)
        for win_id, last_vid in list(self.window_last_active_chat.items()):
            if last_vid == view_id:
                del self.window_last_active_chat[win_id]
        return session

    def clear(self):
        """Clear all registered sessions and window bindings."""
        self.sessions.clear()
        self.window_primary_chat.clear()
        self.window_last_active_chat.clear()
        self.clients.clear()


chat_threads = ChatThreadSet()

# Module-level aliases for compatibility
chatview_sessions = chat_threads.sessions
window_primary_chat = chat_threads.window_primary_chat
window_last_active_chat = chat_threads.window_last_active_chat
chatview_clients = chat_threads.clients


class TermChatNewTaskCommand(sublime_plugin.WindowCommand):
    """
    Starts an independent Task Chat session in a new tab (`Term Chat #<id> <title>`).
    Closing the tab directly terminates the task session.
    """
    def run(
        self,
        title=None,
        initial_msg="",
        send_immediate=False,
        cwd="",
        files=None,
        dirs=None,
        ask_title=False,
    ):
        if not cwd:
            paths = (dirs or []) + (files or [])
            for path in paths:
                if os.path.isdir(path):
                    cwd = path
                    break
                parent = os.path.dirname(path)
                if os.path.isdir(parent):
                    cwd = parent
                    break

        target_group = self._resolve_task_group()

        if ask_title and title is None:
            panel = self.window.show_input_panel(
                "Chat Thread Title (optional, press Enter to start):",
                "",
                lambda text: self._start_task(
                    text, initial_msg, send_immediate, cwd, target_group
                ),
                None,
                None,
            )
            panel.settings().set("is_widget", True)
            return

        self._start_task(title or "", initial_msg, send_immediate, cwd, target_group)

    def is_visible(
        self,
        title=None,
        initial_msg="",
        send_immediate=False,
        cwd="",
        files=None,
        dirs=None,
        ask_title=False,
    ):
        if dirs is not None:
            return bool(dirs)
        if files is not None:
            return bool(files)
        return True

    def _resolve_task_group(self):
        if self.window.num_groups() <= 1:
            return None
        active_group = self.window.active_group()
        settings = sublime.load_settings(f"{PACKAGE_NAME}.sublime-settings")
        if not settings.get("dedicated_chat_pane", True):
            return active_group
        for v in self.window.views_in_group(active_group):
            if is_primary_chat_view(v):
                for g in range(self.window.num_groups()):
                    if g != active_group:
                        return g
        return active_group

    def _next_task_number(self):
        counter = self.window.settings().get(CHAT_TASK_COUNTER, 0)
        if not isinstance(counter, int) or counter < 0:
            counter = 0
        for v in self.window.views():
            if is_task_chat_view(v):
                num = v.settings().get(CHAT_TASK_ID)
                if isinstance(num, int) and num > counter:
                    counter = num
        counter += 1
        self.window.settings().set(CHAT_TASK_COUNTER, counter)
        return counter

    def _start_task(self, title, initial_msg, send_immediate, cwd, target_group):
        task_num = self._next_task_number()
        clean_title = (title or "").strip()
        if clean_title:
            view_name = f"Term Chat #{task_num} {clean_title}"
        else:
            view_name = f"Term Chat #{task_num}"

        from .chatview import _create_chat_view_and_session
        chat_view = _create_chat_view_and_session(
            self.window,
            role=SessionRole.TASK,
            view_name=view_name,
            initial_msg=initial_msg,
            send_immediate=send_immediate,
            cwd=cwd,
            target_group=target_group,
        )
        chat_view.settings().set(CHAT_TASK_ID, task_num)
        if clean_title:
            chat_view.settings().set(CHAT_TASK_TITLE, clean_title)


class TermChatSwitchThreadCommand(sublime_plugin.WindowCommand):
    """
    Shows a quick panel listing all active chat sessions (Primary and Task threads)
    in the current window, allowing quick switching between them.
    """
    def _get_session_thread_status(self, session, is_current=False):
        """Return (icon, status_desc, annotation) for a chat session in the switch panel."""
        if session.permission_requests:
            return ("⚠️", "Waiting for approval", "Approval Needed")
        if session.loading_animation and session.loading_animation.is_loading:
            return ("🟢", "Running...", "Running")
        if is_current:
            return ("⭐️", "Active", "Current")
        return ("⚪", "Idle", "Idle")

    def run(self):
        window = self.window or sublime.active_window()
        if not window:
            return
        win_id = window.id()
        sessions = [
            s for s in chat_threads.sessions.values()
            if s.window and s.window.id() == win_id
            and s.chat_view and s.chat_view.is_valid()
        ]
        if not sessions:
            sublime.status_message("No active ChatView sessions")
            return

        active_view = window.active_view()
        current_vid = None
        if active_view and is_chat_view(active_view):
            current_vid = active_view.id()
        else:
            last_chat = chat_threads.find_last_active_chat_view(window)
            if last_chat:
                current_vid = last_chat.id()

        def _sort_key(s):
            is_cur = 0 if (current_vid and s.chat_view.id() == current_vid) else 1
            if s.role == SessionRole.PRIMARY:
                return (is_cur, 0, 0)
            task_id = s.chat_view.settings().get(CHAT_TASK_ID, 0)
            return (is_cur, 1, task_id if isinstance(task_id, int) else 999)

        sessions.sort(key=_sort_key)

        items = []
        for s in sessions:
            vid = s.chat_view.id()
            is_current = bool(current_vid and vid == current_vid)

            if s.role == SessionRole.PRIMARY:
                raw_title = "Chat View (Primary)"
            else:
                raw_title = (
                    s.chat_view.name()
                    or f"Term Chat #{s.chat_view.settings().get(CHAT_TASK_ID, '')}"
                )

            icon, status, annotation = self._get_session_thread_status(
                s, is_current=is_current
            )
            title = raw_title

            agent = chat_threads.get_setting_for_session(
                window, s, CHAT_AGENT, "claude"
            )
            model = chat_threads.get_setting_for_session(
                window, s, f"chatview_model_{agent}", "default"
            )
            details = f"{icon} Agent: {agent} · Model: {model} · {status}"

            if hasattr(sublime, "QuickPanelItem"):
                item = sublime.QuickPanelItem(
                    trigger=title,
                    details=details,
                    annotation=annotation,
                )
            else:
                item = [title, f"{details}  [{annotation}]"]
            items.append(item)

        def on_select(idx):
            if idx < 0 or idx >= len(sessions):
                return
            target_session = sessions[idx]
            window.focus_view(target_session.chat_view)

        window.show_quick_panel(
            items,
            on_select,
            placeholder="Switch Chat Thread / Session",
        )

    def is_enabled(self):
        window = self.window or sublime.active_window()
        if not window:
            return False
        win_id = window.id()
        return any(
            s.window and s.window.id() == win_id
            and s.chat_view and s.chat_view.is_valid()
            for s in chat_threads.sessions.values()
        )


class TermChatRenameThreadCommand(sublime_plugin.WindowCommand):
    """
    Rename the current Task Chat thread tab (`Term Chat #<id> <title>`).
    Primary Chat View cannot be renamed.
    """
    def _resolve_target_view(self, group=-1, index=-1, view_id=None):
        if view_id is not None:
            for v in self.window.views():
                if v.id() == view_id:
                    return v
        try:
            group, index = int(group), int(index)
        except (TypeError, ValueError):
            group, index = -1, -1
        if group >= 0 and index >= 0:
            views = self.window.views_in_group(group)
            if index < len(views):
                return views[index]
        return self.window.active_view()

    def is_visible(self, group=-1, index=-1, view_id=None):
        view = self._resolve_target_view(group, index, view_id)
        return bool(view and is_task_chat_view(view))

    def is_enabled(self, group=-1, index=-1, view_id=None):
        view = self._resolve_target_view(group, index, view_id)
        return bool(view and is_task_chat_view(view))

    def run(self, title=None, group=-1, index=-1, view_id=None):
        target_view = self._resolve_target_view(group, index, view_id)
        if not target_view:
            sublime.status_message("No view to rename")
            return

        if is_primary_chat_view(target_view):
            sublime.status_message("Primary Chat View cannot be renamed")
            return

        if not is_task_chat_view(target_view):
            sublime.status_message("Current tab is not a Task Chat thread")
            return

        if title is not None:
            self._apply_rename(target_view, title)
            return

        current_title = target_view.settings().get(CHAT_TASK_TITLE, "")
        panel = self.window.show_input_panel(
            "Rename Chat Thread:",
            current_title,
            lambda text: self._apply_rename(target_view, text),
            None,
            None,
        )
        panel.settings().set("is_widget", True)

    def _apply_rename(self, view, title):
        clean_title = (title or "").strip()
        clean_title = re.sub(
            r"^(?:Term\s*Chat|TermChat)\s*#\d+\s*", "", clean_title
        ).strip()
        task_num = view.settings().get(CHAT_TASK_ID)
        if task_num is not None:
            view_name = (
                f"Term Chat #{task_num} {clean_title}"
                if clean_title
                else f"Term Chat #{task_num}"
            )
        else:
            view_name = clean_title or "Term Chat"

        view.set_name(view_name)
        if clean_title:
            view.settings().set(CHAT_TASK_TITLE, clean_title)
        else:
            view.settings().erase(CHAT_TASK_TITLE)

        chat_threads.update_window_model_phantoms(self.window)
        sublime.status_message(f"Renamed thread to: {view_name}")
