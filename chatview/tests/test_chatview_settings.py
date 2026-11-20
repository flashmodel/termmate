import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)


def _ensure_sublime_mocks():
    """Populate integer flags on mocked sublime modules so chatview imports cleanly."""
    sublime_mod = sys.modules.setdefault("sublime", MagicMock())
    for attr, val in (
        ("DRAW_NO_FILL", 1),
        ("DRAW_NO_OUTLINE", 2),
        ("HIDDEN", 4),
        ("PERSISTENT", 8),
        ("ENCODED_POSITION", 1),
    ):
        if not isinstance(getattr(sublime_mod, attr, None), int):
            setattr(sublime_mod, attr, val)

    plugin_mod = sys.modules.setdefault("sublime_plugin", MagicMock())
    for base in ("WindowCommand", "TextCommand", "EventListener", "ListInputHandler"):
        if not isinstance(getattr(plugin_mod, base, None), type):
            setattr(
                plugin_mod,
                base,
                type(base, (), {"__init__": lambda self, *a, **kw: None}),
            )


def _load_chatview_modules():
    """Load chatview.py under a synthetic package so its relative ..genfoundry imports resolve."""
    _ensure_sublime_mocks()
    pkg_name = "_termmate_test_pkg"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [repo_dir]
        sys.modules[pkg_name] = pkg
    chatview_mod = importlib.import_module(f"{pkg_name}.chatview.chatview")
    chatthread_mod = importlib.import_module(f"{pkg_name}.chatview.chatthread")
    return chatview_mod, chatthread_mod


chatview_mod, chatthread_mod = _load_chatview_modules()
SessionRole = chatthread_mod.SessionRole
chat_threads = chatthread_mod.chat_threads


class _FakeViewSettings:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    def has(self, key):
        return key in self.data

    def erase(self, key):
        self.data.pop(key, None)


def _make_fake_view(view_id=10, window=None, initial_settings=None):
    view = MagicMock()
    view.id.return_value = view_id
    view.window.return_value = window
    view.size.return_value = 0
    view.get_regions.return_value = []
    settings = _FakeViewSettings(initial_settings)
    view.settings.return_value = settings
    return view, settings


class TestChatViewSettings(unittest.TestCase):
    """Verify default editor settings applied to ChatView tabs."""

    def setUp(self):
        chat_threads.clear()

    def tearDown(self):
        chat_threads.clear()

    def _assert_default_view_settings(self, settings):
        self.assertIs(settings.get("highlight_gutter"), False)
        self.assertIs(settings.get("highlight_line"), False)
        self.assertIs(settings.get("draw_minimap"), False)
        self.assertIs(settings.get("line_numbers"), False)
        self.assertIs(settings.get("word_wrap"), True)
        self.assertIs(settings.get("fold_buttons"), True)
        self.assertEqual(settings.get("draw_unicode_white_space"), "none")

    def test_create_primary_and_task_chat_view_defaults(self):
        for role, name in (
            (SessionRole.PRIMARY, chatview_mod.CHAT_VIEW_NAME),
            (SessionRole.TASK, "TermChat #1"),
        ):
            window = MagicMock()
            window.id.return_value = 1
            window.folders.return_value = []
            window.settings.return_value = _FakeViewSettings()
            chat_view, settings = _make_fake_view(view_id=20 + len(name), window=window)
            window.new_file.return_value = chat_view

            app_settings = _FakeViewSettings({"share_workspace_folders": False})
            with patch.object(chatview_mod.sublime, "load_settings", return_value=app_settings), \
                 patch.object(chatview_mod, "ChatSession") as mock_session_cls, \
                 patch.object(chatview_mod, "update_agent_model_status"):
                mock_session_cls.return_value.role = role
                chatview_mod._create_chat_view_and_session(
                    window, role=role, view_name=name, cwd="/tmp"
                )

            self._assert_default_view_settings(settings)

    def test_reconnect_chat_view_applies_default_settings(self):
        window = MagicMock()
        window.id.return_value = 2
        window.folders.return_value = []
        window.settings.return_value = _FakeViewSettings()

        chat_view, settings = _make_fake_view(
            view_id=42,
            window=window,
            initial_settings={
                chatview_mod.CHAT_VIEW_FLAG: True,
                chatview_mod.CHAT_THREAD_LABEL: SessionRole.PRIMARY.value,
                "highlight_gutter": True,
                "highlight_line": True,
            },
        )

        app_settings = _FakeViewSettings({"share_workspace_folders": False})
        with patch.object(chatview_mod.sublime, "load_settings", return_value=app_settings), \
             patch.object(chatview_mod, "ChatSession") as mock_session_cls, \
             patch.object(chatview_mod, "update_agent_model_status"):
            mock_session_cls.return_value.role = SessionRole.PRIMARY
            chatview_mod._reconnect_chat_view(chat_view)

        self.assertIs(settings.get("highlight_gutter"), False)
        self.assertIs(settings.get("highlight_line"), False)
        self.assertEqual(settings.get("draw_unicode_white_space"), "none")


if __name__ == "__main__":
    unittest.main()
