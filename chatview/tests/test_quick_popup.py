import sys
import unittest
from unittest.mock import MagicMock, patch

mock_sublime = MagicMock()
mock_sublime_plugin = MagicMock()
sys.modules.setdefault("sublime", mock_sublime)
sys.modules.setdefault("sublime_plugin", mock_sublime_plugin)

from chatview.chatrender import (
    QuickOutputPopup,
    QuickOutputPopupManager,
    quick_popup_manager,
)


class TestQuickPopup(unittest.TestCase):

    def test_popup_lifecycle_and_actions(self):
        mock_view = MagicMock()
        mock_view.is_valid.return_value = True
        mock_view.sel.return_value = [
            MagicMock(begin=lambda: 10, end=lambda: 25, empty=lambda: False)
        ]
        mock_view.visible_region.return_value = MagicMock(begin=lambda: 0, end=lambda: 100)
        mock_region = MagicMock()
        mock_region.begin.return_value = 10
        mock_region.end.return_value = 25
        mock_region.b = 25
        mock_view.get_regions.return_value = [mock_region]

        mock_window = MagicMock()

        mock_mdpopups = MagicMock()
        mock_mdpopups.is_popup_visible.return_value = False

        with patch("chatview.chatrender.HAS_MDPOPUPS", True), \
             patch("chatview.chatrender.mdpopups", mock_mdpopups):
            manager = QuickOutputPopupManager()
            self.assertTrue(manager.is_available)

            popup = manager.start(mock_window, mock_view, chat_view_id=999, prompt="Test prompt")
            self.assertIsNotNone(popup)
            self.assertTrue(popup.is_active)
            self.assertTrue(mock_mdpopups.show_popup.called)
            self.assertEqual(mock_mdpopups.show_popup.call_count, 1)
            # Initially compact while waiting for response
            self.assertFalse(popup.is_expanded)
            self.assertEqual(popup.slot_height, 72)

            # Once popup is shown, is_popup_visible returns True
            mock_mdpopups.is_popup_visible.return_value = True

            # Test appending text expands to full height
            popup.append("```python\ndef add(a, b): return a + b\n```")
            self.assertIn("def add", popup.buffer)
            self.assertTrue(popup.is_expanded)
            self.assertTrue(popup.is_visible)
            self.assertEqual(popup.slot_height, popup.full_height)

            # Trigger finish updates popup without calling show_popup again
            popup.finish()
            self.assertTrue(popup.is_finished)
            self.assertTrue(mock_mdpopups.update_popup.called)
            self.assertEqual(mock_mdpopups.show_popup.call_count, 1)
            self.assertIsNotNone(popup.phantom_set)
            self.assertTrue(popup.phantom_set.update.called)
            saved_height = popup.slot_height

            # Simulate scrolling away (anchor point 25 outside visible_region 100..200)
            mock_view.visible_region.return_value = MagicMock(begin=lambda: 100, end=lambda: 200)
            popup._on_hide()
            self.assertFalse(popup.is_visible)
            self.assertTrue(popup.is_active)
            self.assertEqual(popup.slot_height, saved_height)

            # Simulate scrolling back (anchor point 25 back inside visible_region 0..100)
            mock_view.visible_region.return_value = MagicMock(begin=lambda: 0, end=lambda: 100)
            manager.on_view_activated(mock_view)
            self.assertTrue(popup.is_visible)
            self.assertEqual(popup.slot_height, saved_height)

            # Test on_navigate action:close
            popup._on_navigate("action:close")
            self.assertFalse(popup.is_active)
            popup.phantom_set.update.assert_called_with([])

            # Test on_navigate action:chat
            mock_chat_view = MagicMock(id=lambda: 999)
            mock_window.views.return_value = [mock_chat_view]
            popup.is_active = True
            popup._on_navigate("action:chat")
            mock_window.focus_view.assert_called_with(mock_chat_view)

            # Clean up
            manager.close(999)
            self.assertIsNone(manager.get(999))


if __name__ == "__main__":
    unittest.main()
