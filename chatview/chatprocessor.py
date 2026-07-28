import difflib
import logging
import os
import re
import xml.etree.ElementTree

import sublime

from . import utils
from .md_render import MarkdownFormatter

LOG = logging.getLogger("TermMate")

def _make_tool_file_re(tool_names):
    alt = "|".join(re.escape(n) for n in tool_names)
    return re.compile(rf'^⏺ (?:{alt}) (.+?)(?:#L(\d+)(?:-L(\d+))?)?(?:,.*)?$')


def _resolve_path(path_part, cwd):
    return path_part if os.path.isabs(path_part) else os.path.normpath(os.path.join(cwd, path_part))


def _resolve_rel_path(path, cwd):
    """Return (abs_path, rel_path) for a file path against cwd."""
    abs_path = _resolve_path(path, cwd)
    try:
        rel = os.path.relpath(abs_path, cwd)
    except Exception:
        rel = path
    return abs_path, rel


def _parse_tool_file_line(line_text, tool_file_re, cwd):
    m = tool_file_re.match(line_text.strip())
    if not m:
        return None
    line_start = int(m.group(2)) if m.group(2) else None
    return (_resolve_path(m.group(1).strip(), cwd), line_start)


def _find_line_in_file(file_path, old_text, cwd):
    """Return 1-based line number where old_text starts in file, or None."""
    try:
        abs_path = file_path if os.path.isabs(file_path) else os.path.join(cwd, file_path)
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        idx = text.find(old_text)
        if idx != -1:
            return text[:idx].count("\n") + 1
    except Exception:
        pass
    return None


def _make_edit_diff(old_str, new_str, rel_path, start_line=None):
    """Return unified diff text (without --- +++ header lines), or None.

    If start_line is given, hunk headers are shifted so line numbers reflect
    the actual position in the file rather than starting at 1.
    """
    if old_str and not old_str.endswith("\n"): old_str += "\n"
    if new_str and not new_str.endswith("\n"): new_str += "\n"
    lines = list(difflib.unified_diff(
        old_str.splitlines(keepends=True),
        new_str.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    ))
    if len(lines) <= 2:
        return None
    diff = "".join(lines[2:])
    if start_line and start_line > 1:
        offset = start_line - 1
        def _shift_hunk(m):
            old_s = int(m.group(1)) + offset
            old_c = m.group(2)
            new_s = int(m.group(3)) + offset
            new_c = m.group(4)
            old_part = f"{old_s}" if old_c is None else f"{old_s},{old_c}"
            new_part = f"{new_s}" if new_c is None else f"{new_s},{new_c}"
            return f"@@ -{old_part} +{new_part} @@"
        diff = re.sub(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', _shift_hunk, diff)
    return diff


def _diff_from_structured_patch(patches):
    """Build unified diff hunk text from Claude structuredPatch data, or None."""
    parts = []
    for hunk in patches or []:
        try:
            header = "@@ -{},{} +{},{} @@".format(
                hunk["oldStart"], hunk["oldLines"], hunk["newStart"], hunk["newLines"])
        except (KeyError, TypeError):
            continue
        lines = hunk.get("lines") or []
        parts.append("\n".join([header] + list(lines)))
    return "\n".join(parts) + "\n" if parts else None


def _diff_start_line(diff_text):
    """Return the destination start line from the first @@ hunk, or None."""
    m = re.search(r'^@@ -\d+(?:,\d+)? \+(\d+)', diff_text, re.MULTILINE)
    return int(m.group(1)) if m else None


_HUNK_LINE_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')


class BaseChatMessageProcessor:
    """
    Handles buffering, formatting, and displaying messages from the agent.
    """
    _TOOL_FILE_NAMES = ()

    def __init__(self, session):
        self.session = session
        self.markdown_formatter = MarkdownFormatter()
        self.last_is_tool_call = False
        self._plan_text = ""
        self._tool_file_re = _make_tool_file_re(self._TOOL_FILE_NAMES) if self._TOOL_FILE_NAMES else None

    def handle_message(self, message):
        """Dispatch agent message to appropriate handler."""
        # Handle error strings passed from thread wrapper
        if message == "error":
            pass

        # Check for error tuple/custom protocol from AgentThread
        if isinstance(message, tuple) and message[0] == "error":
            self.append_error(message[1])
            self.session.stop_loading()
            return

        # Check for reset_complete message
        if isinstance(message, tuple) and message[0] == "reset_complete":
            LOG.info("Session reset completed successfully")
            return

        if hasattr(message, "type"):
            self._handle_typed_message(message)

    def _handle_typed_message(self, message):
        raise NotImplementedError

    def _render_diff_block(self, diff_text):
        if not diff_text:
            return ""
        return f"````diff\n{diff_text.rstrip()}\n````"

    def _format_tool_block(self, block):
        name = block.get("name")
        return f"⏺ {name}" if name else ""

    def append_content(self, text, flush=False):
        """Format and append text to the chat view."""
        formatted_text = self.markdown_formatter.format(text, flush=flush)
        if formatted_text:
            sublime.set_timeout(
                lambda: self.session.chat_view.run_command("term_chat_output_append",
                    {"text": formatted_text}),
                0
            )

    def append_error(self, error_msg):
        """Append error message to chat view."""
        sublime.set_timeout(
            lambda: self.session.chat_view.run_command("term_chat_output_append",
                {"text": f"\n\nError: {error_msg}\n"}),
            0
        )

    def open_tool_file(self, line_text, window, view=None, point=None):
        """Parse a tool-call line and open the referenced file. Returns True if handled."""
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd)
        if not cwd:
            return False
        result = _parse_tool_file_line(line_text, self._tool_file_re, cwd) if self._tool_file_re else None
        if result is None:
            result = self._parse_diff_hunk_line(line_text, view, point, cwd)
        if result is None:
            return False
        abs_path, line_start = result
        if line_start is not None:
            window.open_file(f"{abs_path}:{line_start}:0", sublime.ENCODED_POSITION)
        else:
            window.open_file(abs_path)
        return True

    def _parse_diff_hunk_line(self, line_text, view, point, cwd):
        """Resolve a diff hunk header (@@ -a,b +c,d @@) to (abs_path, line).

        The hunk line carries no file path, so scan upward in the view for the
        nearest tool-call header line above the diff block and take its path.
        """
        if view is None or point is None or self._tool_file_re is None:
            return None
        m = _HUNK_LINE_RE.match(line_text.strip())
        if not m:
            return None
        dest_line = int(m.group(1))
        row = view.rowcol(point)[0]
        for r in range(row - 1, max(row - 200, -1), -1):
            text = view.substr(view.line(view.text_point(r, 0))).strip()
            parsed = _parse_tool_file_line(text, self._tool_file_re, cwd)
            if parsed is not None:
                return (parsed[0], dest_line)
            if text.startswith("⏺"):
                return None
        return None

class ClaudeMessageProcessor(BaseChatMessageProcessor):
    _TOOL_FILE_NAMES = ("Read", "Edit", "Write")

    def _handle_typed_message(self, message):
        if message.type == "assistant":
            self.session.start_loading()

            # Extract text content
            text_content = ""
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        if self.last_is_tool_call:
                            text_content += "\n"
                        self.last_is_tool_call = False

                        text_content += block.text

                    elif isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") in ("Edit", "Write"):
                            pass  # defer to tool_use_result
                        else:
                            if not self.last_is_tool_call:
                                text_content += "\n"
                            self.last_is_tool_call = True
                            text_content += self._format_tool_block(block)

            if text_content:
                self.append_content(text_content + "\n")
        elif message.type == "system":
            raw = getattr(message, "raw_data", {}) or {}
            if raw.get("subtype") == "status" and raw.get("status") == "compacting":
                self.session.start_loading(text="compacting")
            elif raw.get("subtype") == "thinking_tokens":
                self.session.start_loading(text="thinking")
            elif hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id and message.content.get("subtype") == "init":
                    LOG.info(f"system session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)

        elif message.type == "control_request":
            # Handle permission request directly
            request = message.content.get("request", {})
            subtype = request.get("subtype")
            if subtype == "can_use_tool":
                tool_name = request.get("tool_name")
                input_data = request.get("input", {})
                request_id = message.content.get("request_id")

                # Store input data for later use in response
                self.session.permission_requests[request_id] = (tool_name, input_data)

                self.session.show_permission_phantom(request_id, tool_name, input_data)

        elif message.type == "user":
            inner = message.content if isinstance(message.content, dict) else {}
            # Only update the rewind UUID for actual user prompts, not tool_result echo messages.
            # Tool results have a list content (array of tool_use_result blocks); user prompts
            # have a string or a dict with role/content keys.
            msg_content = inner.get("message", {}).get("content") if isinstance(inner.get("message"), dict) else inner.get("content")
            is_tool_result = isinstance(msg_content, list)
            user_uuid = message.id  # set from data.get("uuid") in _parse_message
            if user_uuid and not is_tool_result:
                self.session.update_last_prompt_uuid(user_uuid)
            user_content = inner.get("content") or inner.get("message", {}).get("content", "")
            if isinstance(user_content, str) and user_content.startswith("<local-command-stdout>"):
                local_output = xml.etree.ElementTree.fromstring(user_content)
                self.append_content(local_output.text)
                self.append_content("\n")

            # Render Edit/Write from tool_use_result (has filePath, oldString, newString, structuredPatch)
            tool_use_result = inner.get("tool_use_result")
            if isinstance(tool_use_result, dict) and tool_use_result.get("filePath"):
                rendered = self._format_edit_result(tool_use_result)
                if rendered:
                    self.last_is_tool_call = True
                    self.append_content("\n" + rendered + "\n")
                self._record_file_change(tool_use_result)

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            # Stop loading on turn completion (heuristic)
            self.session.stop_loading()
            self.append_content("\n")
            # Show collapsed file-changes artifact after the turn output settles
            sublime.set_timeout(self.session.show_file_changes_artifact, 0)

        elif message.type == "control_response":
            if hasattr(message, "content") and isinstance(message.content, dict):
                response_outer = message.content.get("response", {})
                if response_outer.get("subtype") == "success":
                    response_data = response_outer.get("response", {})
                    models = response_data.get("models", [])
                    if models:
                        self.session.available_models = models

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.session.available_models = models

    def _format_tool_block(self, block):
        name = block.get("name")
        input_data = block.get("input", {})

        if name == "Read":
            file_path = input_data.get("file_path", "")
            if file_path:
                try:
                    rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                except Exception:
                    rel_path = file_path

                # Extract offset and limit for line number display
                offset = input_data.get("offset")
                limit = input_data.get("limit")

                if offset is not None and limit is not None:
                    start_line = offset
                    end_line = offset + limit - 1
                    return f"⏺ Read {rel_path}#L{start_line}-L{end_line}"
                elif offset is not None:
                    return f"⏺ Read {rel_path}#L{offset}"
                else:
                    return f"⏺ Read {rel_path}"

        elif name in ("Agent", "Task"):
            description = input_data.get("description", "")
            subagent_type = input_data.get("subagent_type", "")

            parts = [f"⏺ {name}"]
            if subagent_type:
                parts.append(subagent_type)
            if description:
                parts.append(description)

            return " ".join(parts)

        elif name == "Bash":
            command = input_data.get("command", "")
            if command:
                lines = command.rstrip().splitlines()
                if len(lines) > 1:
                    first_line = lines[0]
                    indented_rest = "\n".join("    " + line for line in lines[1:])
                    return f"⏺ Bash {first_line}\n\n{indented_rest}\n"
                elif len(lines) == 1:
                    return f"⏺ Bash {lines[0]}"

        elif name in ("Grep", "Glob"):
            pattern = input_data.get("pattern")
            if pattern:
                return f"⏺ {name} {pattern}"
        elif name == "WebFetch":
            url = input_data.get("url", "")
            if url:
                return f"⏺ WebFetch {url}"
        elif name == "WebSearch":
            query = input_data.get("query", "")
            if query:
                return f"⏺ WebSearch ({query})"

        return f"⏺ {name}" if name else ""

    def _format_edit_result(self, tool_use_result):
        """Render Edit/Write header + diff from tool_use_result data."""
        file_path = tool_use_result.get("filePath", "")
        if not file_path:
            return None
        cwd = self.session.agent_thread.cwd if self.session.agent_thread else ""
        _, rel_path = _resolve_rel_path(file_path, cwd)

        patches = tool_use_result.get("structuredPatch") or []
        start_line = patches[0].get("newStart") if patches else None

        # Write creates a new file (no oldString); Edit has both
        old_str = tool_use_result.get("oldString")
        new_str = tool_use_result.get("newString")
        is_edit = old_str is not None

        name = "Edit" if is_edit else "Write"
        header = f"⏺ {name} {rel_path}#L{start_line}" if start_line else f"⏺ {name} {rel_path}"

        if is_edit:
            diff = _make_edit_diff(old_str, new_str or "", rel_path, start_line=start_line)
            if diff:
                return header + "\n\n" + self._render_diff_block(diff)

        return header

    def _record_file_change(self, tool_use_result):
        """Record an Edit/Write diff for the end-of-turn file changes artifact."""
        file_path = tool_use_result.get("filePath")
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd) or ""
        abs_path, rel_path = _resolve_rel_path(file_path, cwd)

        patches = tool_use_result.get("structuredPatch")
        diff = _diff_from_structured_patch(patches)
        if diff is None:
            old_str = tool_use_result.get("oldString")
            new_str = tool_use_result.get("newString")
            if old_str is not None:
                start_line = patches[0].get("newStart") if patches else None
                diff = _make_edit_diff(old_str, new_str or "", rel_path, start_line=start_line)
            else:
                # Write with no patch data: treat as a new file
                content = tool_use_result.get("content") or ""
                if content:
                    lines = content.splitlines()
                    diff = ("@@ -0,0 +1,{} @@\n".format(len(lines))
                            + "\n".join("+" + l for l in lines) + "\n")

        self.session.record_file_change(abs_path, rel_path, diff)


class CodexMessageProcessor(BaseChatMessageProcessor):
    _TOOL_FILE_NAMES = ("fileChange",)

    def _handle_typed_message(self, message):
        if message.type == "assistant":
            self.session.start_loading()

            # Extract text content
            text_content = ""
            if hasattr(message, "content"):
                for block in message.content:
                    if hasattr(block, "text"):
                        if self.last_is_tool_call:
                            text_content += "\n"
                        self.last_is_tool_call = False

                        text_content += block.text

            if text_content:
                self.append_content(text_content + "\n")

        elif message.type == "tool_use":
            if not self.last_is_tool_call:
                self.append_content("\n")
            self.last_is_tool_call = True
            self.append_content(self._format_tool_block(message.content) + "\n")
            if message.content.get("name") == "fileChange":
                self._record_file_changes(message.content)

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "control_request":
            # Handle permission request directly
            request = message.content.get("request", {})
            subtype = request.get("subtype")
            if subtype == "can_use_tool":
                tool_name = request.get("tool_name")
                input_data = request.get("input", {})
                request_id = message.content.get("request_id")

                # Store input data for later use in response
                self.session.permission_requests[request_id] = (tool_name, input_data)

                self.session.show_permission_phantom(request_id, tool_name, input_data)

        elif message.type == "thread_started":
            if hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id:
                    LOG.info(f"Codex thread session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.session.available_models = models

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            # Stop loading on turn completion (heuristic)
            self.session.stop_loading()
            self.append_content("\n")

        elif message.type == "plan_delta":
            # Codex plan mode: accumulate <proposed_plan> content
            content = message.content if isinstance(message.content, str) else ""
            if content:
                self._plan_text += content

        elif message.type == "turn_started":
            # Extract turnId if available
            self._active_turn_id = message.content.get("turnId")
            turn_index = message.content.get("turnIndex")
            if turn_index is not None:
                self.session.update_last_prompt_uuid(str(turn_index))
            self.session.start_loading()

        elif message.type in ("thinking", "text"):
            self.session.start_loading()

        elif message.type == "stop":
            # Codex agent sends "stop" when its process completes
            self.append_content("", flush=True)
            self.session.stop_loading()
            self.append_content("\n")
            # If Codex plan mode produced a plan, open it in a new view
            if self._plan_text:
                plan_text = self._plan_text
                self._plan_text = ""

                self.append_content("\n")
                self.append_content(plan_text)
                self.append_content("\n", flush=True)

                # Add Implement button if in plan mode
                if self.session.agent_thread and self.session.agent_thread.anthropic_config.get("plan_mode"):
                    sublime.set_timeout(lambda pt=plan_text: self.session.show_implement_plan_button(pt), 0)
            # Show the artifact last so later appends don't land at its fold boundary
            sublime.set_timeout(self.session.show_file_changes_artifact, 0)

    def _format_tool_block(self, block):
        name = block.get("name")

        if name == "command_execution":
            command = block.get("command", "")
            if command:
                lines = command.rstrip().splitlines()
                if len(lines) > 1:
                    first_line = lines[0]
                    indented_rest = "\n".join("    " + line for line in lines[1:])
                    return f"⏺ command ({first_line})\n\n{indented_rest}\n"
                elif len(lines) == 1:
                    return f"⏺ command ({lines[0]})"
            return "⏺ command"
        elif name == "fileChange":
            changes = block.get("changes", [])
            cwd = (self.session.agent_thread.cwd
                   if self.session.agent_thread else self.session.cwd) or ""

            file_parts = []
            diffs = []
            for change in changes:
                path = change.get("path", "")
                diff_text = change.get("diff") or ""

                if path:
                    _, rel = _resolve_rel_path(path, cwd)
                    line_no = _diff_start_line(diff_text) if diff_text else None
                    file_parts.append(f"{rel}#L{line_no}" if line_no else rel)

                if diff_text:
                    rendered = self._render_diff_block(diff_text)
                    if rendered:
                        diffs.append(rendered)

            header = f"⏺ fileChange {', '.join(file_parts)}" if file_parts else "⏺ fileChange"

            if diffs:
                return header + "\n\n" + "\n\n".join(diffs)

            return header

        return f"⏺ {name}" if name else ""

    def _record_file_changes(self, block):
        """Record fileChange diffs for the end-of-turn file changes artifact."""
        changes = block.get("changes", [])
        cwd = (self.session.agent_thread.cwd
               if self.session.agent_thread else self.session.cwd) or ""
        for change in changes:
            path = change.get("path", "")
            if not path:
                continue
            abs_path, rel_path = _resolve_rel_path(path, cwd)
            diff_text = change.get("diff") or ""
            self.session.record_file_change(abs_path, rel_path, diff_text or None)

class PiMessageProcessor(BaseChatMessageProcessor):
    _TOOL_FILE_NAMES = ("read", "edit", "write")

    def __init__(self, session):
        super().__init__(session)
        self._in_plan = False
        self._plan_text = ""
        self._text_buffer = ""
        self._pending_edits = {}  # toolCallId -> block dict

    def _handle_typed_message(self, message):
        if message.type == "text_delta":
            self.session.start_loading()
            if self.last_is_tool_call:
                self.append_content("\n")
                self.last_is_tool_call = False
            
            self._text_buffer += message.content

            while True:
                if not self._in_plan:
                    if "<proposed_plan>" in self._text_buffer:
                        parts = self._text_buffer.split("<proposed_plan>", 1)
                        if parts[0]:
                            self.append_content(parts[0])
                        self._in_plan = True
                        self._text_buffer = parts[1]
                        self.append_content("\n\n**Proposed Plan**\n\n")
                    else:
                        flush_index = len(self._text_buffer)
                        for i in range(len(self._text_buffer)):
                            if "<proposed_plan>".startswith(self._text_buffer[i:]):
                                flush_index = i
                                break
                        if flush_index > 0:
                            self.append_content(self._text_buffer[:flush_index])
                            self._text_buffer = self._text_buffer[flush_index:]
                        break
                else:
                    if "</proposed_plan>" in self._text_buffer:
                        parts = self._text_buffer.split("</proposed_plan>", 1)
                        self._plan_text += parts[0]
                        self.append_content(parts[0])
                        self._in_plan = False
                        self._text_buffer = parts[1]
                    else:
                        flush_index = len(self._text_buffer)
                        for i in range(len(self._text_buffer)):
                            if "</proposed_plan>".startswith(self._text_buffer[i:]):
                                flush_index = i
                                break
                        if flush_index > 0:
                            self._plan_text += self._text_buffer[:flush_index]
                            self.append_content(self._text_buffer[:flush_index])
                            self._text_buffer = self._text_buffer[flush_index:]
                        break
            return
            
        if message.type == "text_end":
            if self._text_buffer:
                if self._in_plan:
                    self._plan_text += self._text_buffer
                self.append_content(self._text_buffer)
                self._text_buffer = ""
            return
            
        if message.type in ("thinking_start", "thinking_delta"):
            self.session.start_loading(text="thinking")
            return

        if message.type == "assistant":
            self.session.start_loading()

            # Extract tool blocks
            if hasattr(message, "content"):
                for block in message.content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") in ("edit", "write"):
                            tool_call_id = block.get("id")
                            if tool_call_id:
                                args = block.get("arguments", {})
                                self._pending_edits[tool_call_id] = {
                                    "file_path": args.get("path") or args.get("file_path") or "",
                                    "old_text": args.get("oldText"),
                                    "new_text": args.get("newText"),
                                    "edits": args.get("edits"),
                                }
                            # defer to toolResult
                        else:
                            if not self.last_is_tool_call:
                                self.append_content("\n")
                            self.last_is_tool_call = True
                            content = self._format_tool_block(block)
                            if content:
                                self.append_content(content + "\n")

        elif message.type == "system":
            if hasattr(message, "content") and isinstance(message.content, dict):
                session_id = message.content.get("session_id")
                if session_id and message.content.get("subtype") == "init":
                    LOG.info(f"system session_id: {session_id}")
                    # Store session_id in view settings for persistence across restarts
                    self.session.set_view_session_id(self.session.chat_view, session_id)



        elif message.type == "extension_ui_request":
            request = message.content
            method = request.get("method")
            request_id = request.get("id")
            title = request.get("title", method)
            
            if method == "confirm" and isinstance(title, str) and title.startswith("Tool Permission: "):
                try:
                    import json
                    parsed_msg = json.loads(request.get("message", "{}"))
                    real_tool_name = parsed_msg.get("toolName")
                    real_input = parsed_msg.get("input", {})
                    if real_tool_name:
                        # Use the real tool name for the phantom so it formats correctly
                        # Capitalize to match Claude/Codex conventions like "Bash", "Edit"
                        display_tool_name = real_tool_name.capitalize() if real_tool_name in ("bash", "read", "write", "edit", "grep", "find", "ls") else real_tool_name
                        self.session.permission_requests[request_id] = ("termchat_tool_permission", request)
                        self.session.show_permission_phantom(request_id, display_tool_name, real_input)
                        return
                except Exception as e:
                    LOG.error(f"Failed to parse termchat tool permission request: {e}")

            tool_name = f"extension_ui_{method}"
            self.session.permission_requests[request_id] = (tool_name, request)
            
            # Show in permission phantom
            self.session.show_permission_phantom(request_id, f"Extension UI: {title}", request)

        elif message.type == "error":
            self.append_error(message.content)
            self.session.stop_loading()

        elif message.type == "result":
            # Flush markdown formatter buffer
            self.append_content("", flush=True)
            # Stop loading on turn completion (heuristic)
            self.session.stop_loading()
            self.append_content("\n")

        elif message.type == "message_end":
            data = message.content if isinstance(message.content, dict) else {}
            if data.get("role") == "toolResult" and data.get("toolName") in ("edit", "write") and not data.get("isError"):
                tool_call_id = data.get("toolCallId")
                pending = self._pending_edits.pop(tool_call_id, None) if tool_call_id else None
                rendered = self._format_pi_edit_result(pending, data)
                if rendered:
                    self.last_is_tool_call = True
                    self.append_content("\n" + rendered + "\n")

            if isinstance(message.content, dict) and message.content.get("customType") == "proposed-plan":
                plan_text = message.content.get("content", "")
                if plan_text.startswith("**Proposed Plan**\n\n"):
                    plan_text = plan_text[len("**Proposed Plan**\n\n"):]
                
                plan_text = plan_text.strip()
                sublime.set_timeout(lambda pt=plan_text: self.session.show_implement_plan_button(pt, tool_name="ImplementPlan"), 0)

        elif message.type == "control_response":
            if hasattr(message, "content") and isinstance(message.content, dict):
                response_outer = message.content.get("response", {})
                if response_outer.get("subtype") == "success":
                    response_data = response_outer.get("response", {})
                    models = response_data.get("models", [])
                    if models:
                        self.session.available_models = models

        elif message.type == "models_update":
            if hasattr(message, "content") and isinstance(message.content, dict):
                models = message.content.get("models", [])
                if models:
                    self.session.available_models = models

    def _format_tool_block(self, block):
        name = block.get("name")
        input_data = block.get("arguments", {})

        if name == "read":
            file_path = input_data.get("file_path") or input_data.get("path") or ""
            if file_path:
                try:
                    if os.path.isabs(file_path):
                        rel_path = os.path.relpath(file_path, self.session.agent_thread.cwd)
                    else:
                        rel_path = file_path
                except Exception:
                    rel_path = file_path

                offset = input_data.get("offset")
                limit = input_data.get("limit")

                if offset is not None and limit is not None:
                    start_line = offset
                    end_line = offset + limit - 1
                    return f"⏺ read {rel_path}#L{start_line}-L{end_line}"
                elif offset is not None:
                    return f"⏺ read {rel_path}#L{offset}"
                else:
                    return f"⏺ read {rel_path}"

        elif name in ("agent", "task"):
            description = input_data.get("description", "")
            subagent_type = input_data.get("subagent_type", "")

            parts = [f"⏺ {name}"]
            if subagent_type:
                parts.append(subagent_type)
            if description:
                parts.append(description)

            return " ".join(parts)

        elif name == "bash":
            command = input_data.get("command", "")
            if command:
                lines = command.rstrip().splitlines()
                if len(lines) > 1:
                    first_line = lines[0]
                    indented_rest = "\n".join("    " + line for line in lines[1:])
                    return f"⏺ bash {first_line}\n\n{indented_rest}\n"
                elif len(lines) == 1:
                    return f"⏺ bash {lines[0]}"

        elif name in ("grep", "find", "ls"):
            pattern = input_data.get("pattern") or input_data.get("query") or input_data.get("path") or input_data.get("regex") or ""
            return f"⏺ {name} {pattern}".strip()

        return f"⏺ {name}" if name else ""

    def _format_pi_edit_result(self, pending, tool_result_data):
        """Render edit/write header + diff from Pi toolResult data."""
        file_path = (pending or {}).get("file_path", "")
        if not file_path:
            return None
        cwd = self.session.agent_thread.cwd if self.session.agent_thread else ""
        _, rel_path = _resolve_rel_path(file_path, cwd)

        name = tool_result_data.get("toolName", "edit")
        first_line = (tool_result_data.get("details") or {}).get("firstChangedLine")
        header = f"⏺ {name} {rel_path}#L{first_line}" if first_line else f"⏺ {name} {rel_path}"

        if name == "edit" and pending:
            edits = pending.get("edits")
            old_text = pending.get("old_text")
            new_text = pending.get("new_text")

            if edits and isinstance(edits, list):
                diffs = [d for e in edits
                         if (d := _make_edit_diff(e.get("oldText") or "", e.get("newText") or "", rel_path,
                                                  start_line=first_line))]
                if diffs:
                    return header + "\n" + self._render_diff_block("\n".join(diffs))
            elif old_text is not None:
                diff = _make_edit_diff(old_text, new_text or "", rel_path, start_line=first_line)
                if diff:
                    return header + "\n" + self._render_diff_block(diff)

        return header
