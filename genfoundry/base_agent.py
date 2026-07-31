"""
Unified Data Models and BaseAgent Interface
"""

import abc
from typing import Optional, Dict, Any, AsyncIterator, List, Callable, Union
from enum import Enum

class MessageType(Enum):
    """Types of messages that can be received from an Agent"""
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    STOP = "stop"
    THINKING = "thinking"
    PLAN_DELTA = "plan_delta"

class Message:
    """Represents a message from an Agent"""

    def __init__(self, msg_type: str, content: Any = None, msg_id: Optional[str] = None, **kwargs):
        self.type = msg_type
        self.content = content
        self.id = msg_id
        self.raw_data = kwargs

    def __repr__(self):
        return f"Message(type={self.type}, id={self.id}, content={self.content})"

class TextBlock:
    """Represents a text content block"""

    def __init__(self, text: str):
        self.text = text
        self.type = "text"

    def __repr__(self):
        return f"TextBlock(text={self.text[:50]}...)"


class AssistantMessage:
    """Represents an assistant message with content blocks"""

    def __init__(self, content: List[Union[TextBlock, Any]], msg_id: Optional[str] = None):
        self.content = content
        self.id = msg_id
        self.role = "assistant"
        self.type = "assistant"

    def __repr__(self):
        return f"AssistantMessage(id={self.id}, blocks={len(self.content)})"


class PermissionResultAllow:
    """Result indicating permission is granted"""
    def __init__(self, updated_input: Optional[Dict[str, Any]] = None):
        self.updated_input = updated_input


class PermissionResultDeny:
    """Result indicating permission is denied"""
    def __init__(self, message: str = "Permission denied"):
        self.message = message


class ToolPermissionContext:
    """Context for tool permission requests"""
    def __init__(self, suggestions: Optional[List[Dict[str, Any]]] = None):
        self.suggestions = suggestions or []


class AgentOptions:
    """Unified configuration options for Agents"""

    def __init__(
        self,
        cwd: Optional[str] = None,
        cli_path: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_turns: Optional[int] = None,
        allowed_tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        permission_mode: str = "default",
        model: Optional[str] = None,
        can_use_tool: Optional[Callable] = None,
        plan_mode: bool = False,
        # Codex specific options can be added here
        sandbox_mode: Optional[str] = None,
        approve_mode: Optional[str] = None,
        session_id: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
        add_dirs: Optional[List[str]] = None,
        debug_agent_message: bool = False,
        enable_file_checkpoint: bool = False,
    ):
        import os
        self.cwd = cwd or os.getcwd()
        self.cli_path = cli_path
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.allowed_tools = allowed_tools or []
        self.disallowed_tools = disallowed_tools or []
        self.permission_mode = permission_mode
        self.model = model
        self.can_use_tool = can_use_tool
        self.plan_mode = plan_mode
        self.sandbox_mode = sandbox_mode
        self.approve_mode = approve_mode
        self.session_id = session_id
        self.extra_env = extra_env or {}
        self.add_dirs = add_dirs or []
        self.debug_agent_message = debug_agent_message
        self.enable_file_checkpoint = enable_file_checkpoint


class BaseAgent(abc.ABC):
    """Abstract Base Agent interface for generative tools (Claude CLI, Codex CLI, etc.)"""

    def __init__(self, options: Optional[AgentOptions] = None):
        self.options = options or AgentOptions()

    @abc.abstractmethod
    async def connect(self, prompt: Optional[str] = None) -> None:
        """Connect to the Agent and optionally send an initial prompt"""
        pass

    @abc.abstractmethod
    async def send_message(self, content: str, parent_tool_use_id: Optional[str] = None, proceed_plan: bool = False) -> None:
        """Send a message/prompt to the Agent"""
        pass

    @abc.abstractmethod
    def receive_messages(self) -> AsyncIterator[Message]:
        """Receive a stream of messages from the Agent"""
        pass

    @abc.abstractmethod
    async def steer(self, text: str, proceed_plan: bool = False) -> None:
        """Send a steering message to the Agent (e.g. 'Implement this plan')"""
        pass

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """Interrupt the current agent conversation or turn"""
        pass

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Disconnect and clean up resources"""
        pass

    async def apply_model_update(self, model: Optional[str]) -> None:
        """Live-switch the model mid-conversation. Default: unsupported, no-op.

        Override in subclasses whose CLI protocol supports changing the model
        without reconnecting. Callers (chatview) call this instead of
        branching on agent type.
        """
        pass

    async def apply_plan_mode_update(self, enabled: bool) -> None:
        """Live-switch plan mode mid-conversation. Default: unsupported, no-op.

        Each provider has a different underlying mechanism (permission mode
        string, a plain attribute, a dedicated control message) — subclasses
        translate `enabled` into their own wire format here.
        """
        pass

    async def send_permission_response(
        self,
        request_id: str,
        response_data: Dict[str, Any],
        is_extension_ui: bool = False,
    ) -> None:
        """Send a permission/approval decision back to the agent process.

        Providers disagree on wire shape (control_response vs approval
        response vs extension_ui_response) — subclasses implement their own,
        but should route through `_write_extension_ui_response` when
        `is_extension_ui` is True (the termchat extension's own UI protocol,
        shared across all providers regardless of native protocol).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support permission responses"
        )

    async def _write_extension_ui_response(
        self, request_id: str, response_data: Dict[str, Any]
    ) -> None:
        """Shared wire format for the termchat extension's own UI protocol.

        Used whenever is_extension_ui=True, and natively by providers with
        no control_request/response protocol of their own (e.g. Pi).
        Relies on the subclass's `_write_json` (all current subclasses
        implement it).
        """
        response = {
            "type": "extension_ui_response",
            "id": request_id,
            **response_data,
        }
        await self._write_json(response)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.disconnect()
