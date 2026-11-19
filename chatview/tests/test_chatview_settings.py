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


class TestResumeRewind(unittest.TestCase):

    def _make_session(self):
        window = MagicMock()
        window.id.return_value = 3
        window.folders.return_value = []
        window.settings.return_value = _FakeViewSettings()
        view, _ = _make_fake_view(view_id=99, window=window)
        view.substr.return_value = "resumed prompt"
        view.line.side_effect = lambda pt: MagicMock(end=lambda p=pt: p)
        with patch.object(chatview_mod, "AgentThread"), \
             patch.object(chatview_mod, "RewindConfirmPanel"), \
             patch.object(chatview_mod, "PermissionPanel"), \
             patch.object(chatview_mod, "LoadingAnimation"), \
             patch.object(chatview_mod, "NoticePhantom"), \
             patch.object(chatview_mod, "TokenSummaryPhantom"), \
             patch.object(chatview_mod, "ModelPanel"), \
             patch.object(chatview_mod, "InputPromptMarker"), \
             patch.object(chatview_mod, "FileChangesArtifact"):
            session = chatview_mod.ChatSession(window, view, "/workspace")
        session.agent_thread = MagicMock()
        session.agent_thread.session_id = "sess-resume"
        session.reload_agent = MagicMock()
        return session, view

    def test_append_resume_history_attaches_uuid(self):
        session, view = self._make_session()
        make_region = lambda a, b: MagicMock(begin=lambda x=a: x, end=lambda y=b: y)
        with patch.object(chatview_mod.sublime, "Region", side_effect=make_region), \
             patch.object(chatview_mod, "get_input_start", side_effect=[10, 26]):
            session._append_resume_history(
                "antigravity",
                "sess-resume",
                {"turns": [{"prompt": "hello", "response": "hi", "user_message_id": "2"}]},
            )

        self.assertEqual(len(session.prompt_regions), 1)
        self.assertEqual(session.prompt_regions[0][1], "2")

    def test_rewind_to_prompt_passes_restore_files_flag_and_banner(self):
        session, view = self._make_session()
        region = MagicMock()
        region.begin.return_value = 10
        region.end.return_value = 24
        session.add_prompt_highlight(region, user_message_uuid="uuid-1")

        # 1. Chat-only rewind (restore_files=False)
        session.rewind_to_prompt(0, restore_files=False)
        args, kwargs = session.agent_thread.rewind.call_args
        self.assertEqual(args[0], "uuid-1")
        self.assertIs(kwargs["restore_files"], False)

        # 2. Rewind with files (restore_files=True)
        session.agent_thread.rewind.reset_mock()
        session.rewind_to_prompt(0, restore_files=True)
        args2, kwargs2 = session.agent_thread.rewind.call_args
        self.assertEqual(args2[0], "uuid-1")
        self.assertIs(kwargs2["restore_files"], True)

        # Banner reflects restore_files flag
        session._on_rewind_complete("sess-resume", 0, restore_files=False)
        append_calls = [
            c for c in view.run_command.call_args_list
            if c.args[0] == "term_chat_output_append"
        ]
        self.assertEqual(
            append_calls[-1].args[1]["text"],
            "■ Rewind conversation to earlier checkpoint\n",
        )

    def test_show_rewind_confirm_uses_agent_capability(self):
        session, _view = self._make_session()
        region = MagicMock()
        region.begin.return_value = 10
        region.end.return_value = 20
        session.add_prompt_highlight(region, user_message_uuid="u0")

        session.agent_thread.agent.support_file_rewind = False
        session.show_rewind_confirm(region, 0)
        self.assertIs(
            session.rewind_confirm_panel.show.call_args.kwargs["support_file_rewind"],
            False,
        )

        session.agent_thread.agent.support_file_rewind = True
        session.show_rewind_confirm(region, 0)
        self.assertIs(
            session.rewind_confirm_panel.show.call_args.kwargs["support_file_rewind"],
            True,
        )

    def test_rewind_confirm_panel_shows_or_hides_files_button(self):
        chatpanel_mod = importlib.import_module("_termmate_test_pkg.chatview.chatpanel")
        view = MagicMock()
        region = MagicMock()
        region.begin.return_value = 10
        panel = chatpanel_mod.RewindConfirmPanel(view)

        results = []
        panel.show(region, 0, results.append, support_file_rewind=True)
        html_with_files = view.show_popup.call_args.args[0]
        on_nav = view.show_popup.call_args.kwargs["on_navigate"]
        self.assertIn(">Restore</a>", html_with_files)
        self.assertIn(">Restore &amp; files</a>", html_with_files)
        self.assertIn(">Cancel</a>", html_with_files)
        on_nav("rewind")
        panel.show(region, 0, results.append, support_file_rewind=True)
        view.show_popup.call_args.kwargs["on_navigate"]("rewind_files")
        self.assertEqual(results, [False, True])

        view.show_popup.reset_mock()
        panel.show(region, 0, results.append, support_file_rewind=False)
        html_chat_only = view.show_popup.call_args.args[0]
        self.assertIn(">Restore</a>", html_chat_only)
        self.assertNotIn("Restore &amp; files", html_chat_only)
        self.assertIn(">Cancel</a>", html_chat_only)


if __name__ == "__main__":
    unittest.main()
