import os
import sys
import unittest
from unittest.mock import MagicMock

# Allow importing chatview when running this test file directly
repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_dir not in sys.path:
    sys.path.insert(0, repo_dir)


class MockWindowCommand:
    def __init__(self, window):
        self.window = window


class MockTextCommand:
    def __init__(self, view):
        self.view = view


mock_sublime = sys.modules.setdefault("sublime", MagicMock())
mock_sublime.platform.return_value = "osx"

mock_sublime_plugin = sys.modules.setdefault("sublime_plugin", MagicMock())
mock_sublime_plugin.WindowCommand = MockWindowCommand
mock_sublime_plugin.TextCommand = MockTextCommand

from chatview.chatthread import (
    SessionRole,
    CHAT_VIEW_NAME,
    CHAT_THREAD_NAME,
    CHAT_THREAD_LABEL,
    CHAT_THREAD_COUNTER,
    CHAT_THREAD_ID,
    CHAT_THREAD_TITLE,
    ChatThreadSet,
    chat_threads,
    chatview_sessions,
    window_primary_chat,
    window_last_active_chat,
    is_chat_view,
    get_chat_view_role,
    is_primary_chat_view,
    is_task_chat_view,
    TermChatNewThreadCommand,
    TermChatRenameThreadCommand,
)


class TestChatThread(unittest.TestCase):

    def setUp(self):
        chat_threads.clear()

    def test_session_role_and_view_identification(self):
        primary_view = MagicMock()
        primary_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.PRIMARY.value if k == CHAT_THREAD_LABEL else True
        )
        primary_view.name.return_value = CHAT_VIEW_NAME

        self.assertTrue(is_chat_view(primary_view))
        self.assertEqual(get_chat_view_role(primary_view), SessionRole.PRIMARY)
        self.assertTrue(is_primary_chat_view(primary_view))
        self.assertFalse(is_task_chat_view(primary_view))

        task_view = MagicMock()
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.TASK.value if k == CHAT_THREAD_LABEL else True
        )
        task_view.name.return_value = f"{CHAT_THREAD_NAME} #1 Bugfix"

        self.assertTrue(is_chat_view(task_view))
        self.assertEqual(get_chat_view_role(task_view), SessionRole.TASK)
        self.assertFalse(is_primary_chat_view(task_view))
        self.assertTrue(is_task_chat_view(task_view))

    def test_register_and_lookup_sessions(self):
        window = MagicMock()
        window.id.return_value = 1

        primary_view = MagicMock()
        primary_view.id.return_value = 101
        primary_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.PRIMARY.value if k == CHAT_THREAD_LABEL else True
        )
        primary_view.name.return_value = CHAT_VIEW_NAME

        task_view = MagicMock()
        task_view.id.return_value = 102
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: SessionRole.TASK.value if k == CHAT_THREAD_LABEL else True
        )
        task_view.name.return_value = f"{CHAT_THREAD_NAME} #1"

        primary_session = MagicMock()
        primary_session.role = SessionRole.PRIMARY
        primary_session.window = window
        primary_session.chat_view = primary_view

        task_session = MagicMock()
        task_session.role = SessionRole.TASK
        task_session.window = window
        task_session.chat_view = task_view

        chat_threads.register(window, primary_view, primary_session)
        chat_threads.register(window, task_view, task_session)

        self.assertEqual(chat_threads.get_primary_session(window), primary_session)
        self.assertEqual(
            chat_threads.get_active_or_primary_session(window, view_id=102),
            task_session,
        )

        removed = chat_threads.unregister(101)
        self.assertEqual(removed, primary_session)
        self.assertIsNone(chat_threads.get_primary_session(window))

    def test_rename_thread_command(self):
        window = MagicMock()
        cmd = TermChatRenameThreadCommand(window)

        task_view = MagicMock()
        task_settings = {CHAT_THREAD_LABEL: SessionRole.TASK.value, CHAT_THREAD_ID: 2}
        task_view.settings.return_value.get.side_effect = (
            lambda k, d=None: task_settings.get(k, d)
        )
        task_view.settings.return_value.set.side_effect = (
            lambda k, v: task_settings.__setitem__(k, v)
        )
        task_view.name.return_value = f"{CHAT_THREAD_NAME} #2"

        cmd._apply_rename(task_view, "Fix authentication bug")

        task_view.set_name.assert_called_with(f"{CHAT_THREAD_NAME} #2 Fix authentication bug")
        self.assertEqual(task_settings[CHAT_THREAD_TITLE], "Fix authentication bug")


if __name__ == "__main__":
    unittest.main()
