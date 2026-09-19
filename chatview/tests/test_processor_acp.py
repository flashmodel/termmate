import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.modules.setdefault("sublime", MagicMock())

from chatview.chatprocessor import (
    BaseChatMessageProcessor,
    AcpMessageProcessor,
)
from genfoundry.base_agent import Message


class TestAcpMessageProcessor(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(
            chat_view=MagicMock(),
            markdown_formatter=MagicMock(),
            append_markdown=MagicMock(),
            start_loading=MagicMock(),
            stop_loading=MagicMock(),
            show_permission_phantom=MagicMock(),
            set_view_session_id=MagicMock(),
            permission_requests={},
            agent_thread=SimpleNamespace(cwd="/Users/test/workspace"),
            cwd="/Users/test/workspace",
        )
        self.processor = AcpMessageProcessor(self.session)

    def test_for_provider_mapping(self):
        self.assertEqual(BaseChatMessageProcessor.for_provider("gemini"), AcpMessageProcessor)
        self.assertEqual(BaseChatMessageProcessor.for_provider("acp"), AcpMessageProcessor)
        self.assertEqual(BaseChatMessageProcessor.for_provider("GEMINI"), AcpMessageProcessor)

    def test_for_provider_dynamic_acp_agent(self):
        mock_settings = MagicMock()
        mock_settings.get.return_value = {"my_bot": {"command": "bot", "args": []}}
        with patch("chatview.chatprocessor.sublime.load_settings", return_value=mock_settings):
            self.assertEqual(BaseChatMessageProcessor.for_provider("my_bot"), AcpMessageProcessor)

    def test_format_tool_block_simple(self):
        block = {"kind": "execute", "title": "ls -la"}
        formatted = self.processor._format_tool_block(block)
        self.assertEqual(formatted, "⏺ Execute ls -la")

    def test_format_tool_block_with_brackets(self):
        block = {"kind": "execute", "title": "Run [test] python app.py"}
        formatted = self.processor._format_tool_block(block)
        self.assertEqual(formatted, "⏺ Execute Run python app.py")

    def test_format_tool_block_multiline(self):
        block = {"kind": "execute", "title": "python3\nimport os\nprint(os.getcwd())"}
        formatted = self.processor._format_tool_block(block)
        expected = "⏺ Execute python3\n\n    import os\n    print(os.getcwd())"
        self.assertEqual(formatted, expected)

    def test_thinking_does_not_append_content(self):
        """Verify that thinking updates do not display thought blocks in the chat view."""
        msg = Message("thinking", content="internal thinking steps...")
        self.processor.handle_message(msg)

        # start_loading called with text="thinking"
        self.session.start_loading.assert_called_with(text="thinking")
        # No text must be appended to chat!
        self.session.append_markdown.assert_not_called()

    def test_text_message_appends_content(self):
        msg = Message("text", content="Hello, world!")
        self.processor.handle_message(msg)

        self.session.start_loading.assert_called()
        self.session.append_markdown.assert_called_with("Hello, world!", flush=False)

    def test_tool_use_message_appends_formatted_tool(self):
        msg = Message("tool_use", content={"kind": "read", "title": "file.txt"})
        self.processor.handle_message(msg)

        self.session.append_markdown.assert_called_with("⏺ Read file.txt\n", flush=False)

    def test_control_request_triggers_permission_phantom(self):
        msg = Message(
            "control_request",
            content={
                "request_id": "req_1",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "input": {"command": "npm start"},
                },
            },
        )
        self.processor.handle_message(msg)

        self.assertIn("req_1", self.session.permission_requests)
        self.session.show_permission_phantom.assert_called_with("req_1", "Bash", {"command": "npm start"})

    def test_system_init_saves_session_id(self):
        msg = Message("system", content={"subtype": "init", "session_id": "ses_999"})
        self.processor.handle_message(msg)

        self.session.set_view_session_id.assert_called_with(self.session.chat_view, "ses_999")

    def test_result_stops_loading(self):
        msg = Message("result", content={"success": True})
        self.processor.handle_message(msg)

        self.session.stop_loading.assert_called()


class TestAcpDiscoveryAndInstall(unittest.TestCase):
    def test_no_install_for_acp(self):
        from chatview.install import get_agent_install_info, get_agent_list_items

        # gemini or ACP clients do not provide install functionality
        label, cmd, can_install, env = get_agent_install_info("gemini")
        self.assertFalse(can_install)
        self.assertIsNone(cmd)

        mock_settings = MagicMock()
        mock_settings.get.return_value = {}
        items = get_agent_list_items(mock_settings)
        agent_names = [item.value for item in items]
        self.assertNotIn("gemini", agent_names)

    def test_find_existing_cli_gemini_auto_detect(self):
        from chatview.install import find_existing_cli

        with patch("shutil.which", return_value="/usr/local/bin/gemini"):
            found = find_existing_cli("gemini")
            self.assertEqual(found, "/usr/local/bin/gemini")

    def test_normalize_acp_agents(self):
        from chatview.install import normalize_acp_agents

        # List format
        list_raw = [
            {"name": "gemini", "command": "gemini", "args": ["--acp"]},
            {"name": "bot", "command": "/bin/bot"},
        ]
        norm = normalize_acp_agents(list_raw)
        self.assertIn("gemini", norm)
        self.assertIn("bot", norm)
        self.assertEqual(norm["bot"]["command"], "/bin/bot")

        # Dict format
        dict_raw = {"gemini": {"command": "gemini"}}
        norm_dict = normalize_acp_agents(dict_raw)
        self.assertIn("gemini", norm_dict)
        self.assertEqual(norm_dict["gemini"]["name"], "gemini")

    def test_find_existing_cli_acp_agents_list_setting(self):
        from chatview.install import find_existing_cli

        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "acp_agents": [
                {"name": "custom_bot", "command": "/opt/bin/bot", "args": ["--stdio"]}
            ]
        }.get(key, default)

        with patch("shutil.which", side_effect=lambda cmd: cmd if cmd == "/opt/bin/bot" else None):
            found = find_existing_cli("custom_bot", mock_settings)
            self.assertEqual(found, "/opt/bin/bot")

    def test_find_existing_cli_acp_agents_setting(self):
        from chatview.install import find_existing_cli

        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "acp_agents": {
                "custom_bot": {"command": "/opt/bin/bot", "args": ["--stdio"]}
            }
        }.get(key, default)

        with patch("shutil.which", side_effect=lambda cmd: cmd if cmd == "/opt/bin/bot" else None):
            found = find_existing_cli("custom_bot", mock_settings)
            self.assertEqual(found, "/opt/bin/bot")

    def test_get_available_agents_includes_gemini_and_acp(self):
        from chatview.install import get_available_agents

        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "acp_agents": {
                "custom_bot": {"command": "/opt/bin/bot"}
            }
        }.get(key, default)

        def mock_find(agent, settings=None):
            if agent in ("gemini", "custom_bot", "claude"):
                return f"/bin/{agent}"
            return None

        with patch("chatview.install.find_existing_cli", side_effect=mock_find):
            agents = get_available_agents(mock_settings)
            self.assertIn("claude", agents)
            self.assertIn("gemini", agents)
            self.assertIn("custom_bot", agents)
            self.assertNotIn("codex", agents)

    def test_get_available_agents_order_acp_at_end(self):
        from chatview.install import get_available_agents

        mock_settings = MagicMock()
        mock_settings.get.side_effect = lambda key, default=None: {
            "acp_agents": {
                "custom_bot": {"command": "/opt/bin/bot"}
            }
        }.get(key, default)

        def mock_find(agent, settings=None):
            return f"/bin/{agent}"

        with patch("chatview.install.find_existing_cli", side_effect=mock_find):
            agents = get_available_agents(mock_settings)
            # Native agents first
            self.assertEqual(agents[:4], ["claude", "codex", "opencode", "pi"])
            # ACP clients at the end
            self.assertEqual(set(agents[4:]), {"custom_bot", "gemini"})


