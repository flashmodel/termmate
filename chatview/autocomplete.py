"""
Path-segment autocomplete manager for '@' tag file navigation in TermMate chatview.
Provides multi-level directory drilling, tab completions, and dynamic popups.
"""

import os
from typing import List, Optional, Tuple, NamedTuple

try:
    import sublime
except ImportError:
    sublime = None


# Default ignored directories and build/system artifacts
DEFAULT_IGNORED_NAMES = {
    '.git', '.svn', '.hg', '.DS_Store', 'Thumbs.db',
    'node_modules', '__pycache__', '.venv', 'venv',
    '.idea', '.vscode', '.build', 'dist', 'target'
}


class AtQuery(NamedTuple):
    """Encapsulates parsed @-completion query context."""
    full_query: str     # The entire query after '@', e.g. "src/components/Cha"
    dir_part: str       # Normalized directory prefix, e.g. "src/components/"
    file_filter: str    # Current typing prefix for filtering, e.g. "Cha"
    at_pos: int         # Absolute position of '@' in the view


def parse_at_query_text(text_before_cursor: str) -> Optional[Tuple[str, str, str, int]]:
    """
    Pure string parser: extracts '@' query details from text preceding cursor.
    Returns (full_query, dir_part, file_filter, at_offset) or None if no valid '@' token.
    """
    at_offset = -1
    for i in range(len(text_before_cursor) - 1, -1, -1):
        ch = text_before_cursor[i]
        # An unescaped space breaks the @ tag token
        if ch == ' ' and (i == 0 or text_before_cursor[i - 1] != '\\'):
            break
        if ch == '@':
            at_offset = i
            break

    if at_offset == -1:
        return None

    full_query = text_before_cursor[at_offset + 1:]

    # Separate directory portion and filter prefix
    if '/' in full_query:
        dir_part, _, file_filter = full_query.rpartition('/')
        dir_part += '/'
    else:
        dir_part = ""
        file_filter = full_query

    return full_query, dir_part, file_filter, at_offset


class AutoComplete:
    """Manages file/directory autocompletion triggered by '@'."""

    @staticmethod
    def extract_at_query(view, pos: int, editable_start: int) -> Optional[AtQuery]:
        """
        Scans backward from cursor `pos` to find an active '@' token in the editable prompt area.
        Handles escaped spaces correctly.
        """
        if pos < editable_start:
            return None

        line_region = view.line(pos)
        start_pt = max(line_region.begin(), editable_start)
        if start_pt >= pos:
            return None

        text_before_cursor = view.substr(sublime.Region(start_pt, pos))
        parsed = parse_at_query_text(text_before_cursor)
        if not parsed:
            return None

        full_query, dir_part, file_filter, at_offset = parsed
        at_pos = start_pt + at_offset

        return AtQuery(
            full_query=full_query,
            dir_part=dir_part,
            file_filter=file_filter,
            at_pos=at_pos
        )

    @staticmethod
    def get_workspace_root(window, chat_workspace_key: str = "chatview_active_workspace") -> Optional[str]:
        """Resolves the active workspace root folder."""
        if not window:
            return None

        # Check custom workspace setting on window first
        custom_cwd = window.settings().get(chat_workspace_key)
        if custom_cwd and os.path.isdir(custom_cwd):
            return custom_cwd

        folders = window.folders()
        if folders:
            return folders[0]

        return None

    @staticmethod
    def get_target_directory(workspace_root: str, dir_part: str) -> Optional[str]:
        """
        Resolves the absolute target directory and performs sandbox validation.
        Prevents navigating outside the workspace root.
        """
        if not workspace_root or not os.path.isdir(workspace_root):
            return None

        target_dir = os.path.normpath(os.path.join(workspace_root, dir_part))

        # Security sandbox check: prevent ../ from traversing outside workspace root
        try:
            common = os.path.commonpath([target_dir, workspace_root])
            if common != workspace_root:
                return None
        except ValueError:
            return None

        if os.path.isdir(target_dir):
            return target_dir
        return None

    @classmethod
    def get_open_files_completions(
        cls,
        window,
        workspace_root: str,
        file_filter: str,
        chat_view_flag: str
    ) -> List:
        """Collects currently open views in the editor for root-level '@' completion."""
        if not sublime:
            return []

        completions = []
        seen_paths = set()

        for v in window.views():
            file_path = v.file_name()
            if not file_path or v.settings().get(chat_view_flag, False):
                continue
            if file_path in seen_paths:
                continue
            seen_paths.add(file_path)

            file_name = os.path.basename(file_path)
            if file_path.startswith(workspace_root):
                rel_path = os.path.relpath(file_path, workspace_root)
            else:
                rel_path = file_name

            # Case-insensitive prefix filter if user has started typing
            if file_filter and not rel_path.lower().startswith(file_filter.lower()):
                continue

            completions.append(sublime.CompletionItem(
                trigger=rel_path,
                annotation="📂 open tab",
                completion=rel_path,
                kind=sublime.KIND_VARIABLE
            ))

        return completions

    @classmethod
    def scan_directory(
        cls,
        target_dir: str,
        file_filter: str = ""
    ) -> Tuple[List[str], List[str]]:
        """Scans target_dir and returns sorted (sub_dirs, sub_files)."""
        sub_dirs = []
        sub_files = []

        try:
            for item in os.listdir(target_dir):
                if item.startswith('.') or item in DEFAULT_IGNORED_NAMES:
                    continue

                if file_filter and not item.lower().startswith(file_filter.lower()):
                    continue

                full_item_path = os.path.join(target_dir, item)
                if os.path.isdir(full_item_path):
                    sub_dirs.append(item)
                elif os.path.isfile(full_item_path):
                    sub_files.append(item)
        except OSError:
            pass

        sub_dirs.sort(key=lambda s: s.lower())
        sub_files.sort(key=lambda s: s.lower())
        return sub_dirs, sub_files

    @classmethod
    def generate_completions(
        cls,
        view,
        locations: List[int],
        editable_start: int,
        chat_view_flag: str,
        chat_workspace_key: str = "chatview_active_workspace"
    ) -> Optional[object]:
        """
        Entry point for `on_query_completions`.
        Returns dynamically refreshed CompletionList.
        """
        if not sublime or not locations:
            return None

        pos = locations[0]
        query = cls.extract_at_query(view, pos, editable_start)
        if not query:
            return None

        window = view.window()
        if not window:
            return None

        workspace_root = cls.get_workspace_root(window, chat_workspace_key)
        if not workspace_root:
            return None

        target_dir = cls.get_target_directory(workspace_root, query.dir_part)
        if not target_dir:
            return None

        completions = []

        # 1. Root level: include currently open tabs
        if not query.dir_part:
            completions.extend(
                cls.get_open_files_completions(window, workspace_root, query.file_filter, chat_view_flag)
            )

        # 2. Scan target directory for child folders and files
        sub_dirs, sub_files = cls.scan_directory(target_dir, query.file_filter)

        # Directory candidates with trailing '/'
        for d in sub_dirs:
            completions.append(sublime.CompletionItem(
                trigger=d + "/",
                annotation="📁 folder",
                completion=d + "/",
                kind=sublime.KIND_NAMESPACE
            ))

        # File candidates
        for f in sub_files:
            completions.append(sublime.CompletionItem(
                trigger=f,
                annotation="📄 file",
                completion=f,
                kind=sublime.KIND_VARIABLE
            ))

        # 3. Dynamic completions list
        flags = sublime.INHIBIT_WORD_COMPLETIONS
        if hasattr(sublime, "DYNAMIC_COMPLETIONS"):
            flags |= sublime.DYNAMIC_COMPLETIONS

        return sublime.CompletionList(completions, flags=flags)

    @classmethod
    def check_cascade_trigger(
        cls,
        view,
        editable_start: int
    ) -> None:
        """
        Called in `on_modified_async` to re-trigger auto_complete when a directory '/' is inserted.
        """
        if not sublime:
            return

        sel = view.sel()
        if not sel:
            return

        pos = sel[0].b
        if pos <= editable_start:
            return

        # Check if the character just inserted before cursor is '/'
        prev_char = view.substr(pos - 1)
        if prev_char != '/':
            return

        # Check if we are inside an '@' tag
        query = cls.extract_at_query(view, pos, editable_start)
        if query is not None:
            # Trigger auto_complete after a minimal delay to ensure buffer is updated
            sublime.set_timeout(lambda: view.run_command("auto_complete", {
                "disable_auto_insert": True,
                "next_completion_if_showing": False,
                "auto_complete_commit_on_tab": True,
            }), 10)
