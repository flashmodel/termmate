"""
Path-segment autocomplete manager for '@' tag file navigation in TermMate chatview.
Provides multi-level directory drilling, multi-workspace routing, and dynamic popups.
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
    dir_part: str       # Normalized directory prefix, e.g. "src/components/" or "backend/cmd/"
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
    """Manages file/directory autocompletion triggered by '@' with multi-workspace support."""

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
    def get_workspace_info(
        window,
        chat_workspace_key: str = "chatview_active_workspace"
    ) -> Tuple[Optional[str], List[str]]:
        """
        Resolves (active_cwd, other_workspaces).
        - active_cwd: The agent's current working directory (window custom setting or first folder).
        - other_workspaces: Any other folders in window.folders().
        """
        if not window:
            return None, []

        folders = window.folders() or []
        custom_cwd = window.settings().get(chat_workspace_key)

        active_cwd = None
        if custom_cwd and os.path.isdir(custom_cwd):
            active_cwd = os.path.normpath(custom_cwd)
        elif folders:
            active_cwd = os.path.normpath(folders[0])

        if not active_cwd:
            return None, []

        other_workspaces = [
            os.path.normpath(f) for f in folders
            if os.path.normpath(f) != active_cwd and os.path.isdir(f)
        ]

        return active_cwd, other_workspaces

    @classmethod
    def resolve_target_dir(
        cls,
        active_cwd: str,
        other_workspaces: List[str],
        dir_part: str
    ) -> Optional[str]:
        """
        Resolves dir_part to an absolute physical directory path with sandbox boundary checks.
        1. If dir_part begins with an other workspace's folder name (e.g. "backend/cmd/"),
           routes to that other workspace.
        2. Otherwise, resolves directly relative to active_cwd (e.g. "src/components/").
        """
        if not dir_part:
            return active_cwd

        first_seg, sep, remaining = dir_part.partition('/')

        # 1. Check if first_seg routes to an other workspace folder
        for ws_folder in other_workspaces:
            ws_name = os.path.basename(ws_folder.rstrip('/\\'))
            if ws_name == first_seg:
                sub_path = remaining if sep else ""
                target = os.path.normpath(os.path.join(ws_folder, sub_path))
                try:
                    if os.path.commonpath([target, ws_folder]) == ws_folder and os.path.isdir(target):
                        return target
                except ValueError:
                    pass

        # 2. Default: route within active_cwd
        target = os.path.normpath(os.path.join(active_cwd, dir_part))
        try:
            if os.path.commonpath([target, active_cwd]) == active_cwd and os.path.isdir(target):
                return target
        except ValueError:
            pass

        return None

    @classmethod
    def get_open_files_completions(
        cls,
        window,
        active_cwd: str,
        other_workspaces: List[str],
        file_filter: str,
        chat_view_flag: str
    ) -> List:
        """Collects currently open views in the editor across all workspace folders."""
        if not sublime or not window:
            return []

        completions = []
        seen_paths = set()
        all_roots = [active_cwd] + other_workspaces

        for v in window.views():
            file_path = v.file_name()
            if not file_path or v.settings().get(chat_view_flag, False):
                continue
            if file_path in seen_paths:
                continue
            seen_paths.add(file_path)

            file_name = os.path.basename(file_path)
            rel_path = file_name

            for root in all_roots:
                if file_path.startswith(root):
                    if root == active_cwd:
                        rel_path = os.path.relpath(file_path, active_cwd)
                    else:
                        ws_name = os.path.basename(root.rstrip('/\\'))
                        rel_path = f"{ws_name}/{os.path.relpath(file_path, root)}"
                    break

            # Case-insensitive prefix filter
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
        Returns dynamically refreshed CompletionList with CWD subdirectories prioritized.
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

        active_cwd, other_workspaces = cls.get_workspace_info(window, chat_workspace_key)

        # Dynamic completions flag: inhibit buffer words and explicit/snippet completions
        flags = sublime.INHIBIT_WORD_COMPLETIONS
        if hasattr(sublime, "INHIBIT_EXPLICIT_COMPLETIONS"):
            flags |= sublime.INHIBIT_EXPLICIT_COMPLETIONS
        if hasattr(sublime, "DYNAMIC_COMPLETIONS"):
            flags |= sublime.DYNAMIC_COMPLETIONS

        if not active_cwd:
            return sublime.CompletionList([], flags=flags)

        completions = []

        # 1. Root Level Completion (query.dir_part is empty)
        #    Priority order:
        #    1) CWD Subdirectories (Highest priority!)
        #    2) CWD Files
        #    3) Other Workspace Root Folders (e.g. backend/)
        #    4) Open Tabs across all projects
        if not query.dir_part:
            # 1.1 Priority 1: CWD Subdirectories
            cwd_dirs, cwd_files = cls.scan_directory(active_cwd, query.file_filter)
            for d in cwd_dirs:
                completions.append(sublime.CompletionItem(
                    trigger=d + "/",
                    annotation="📁 folder",
                    completion=d + "/",
                    kind=sublime.KIND_NAMESPACE
                ))

            # 1.2 Priority 2: CWD Files
            for f in cwd_files:
                completions.append(sublime.CompletionItem(
                    trigger=f,
                    annotation="📄 file",
                    completion=f,
                    kind=sublime.KIND_VARIABLE
                ))

            # 1.3 Priority 3: Other Workspace Root Folders
            for ws_folder in other_workspaces:
                ws_name = os.path.basename(ws_folder.rstrip('/\\'))
                if query.file_filter and not ws_name.lower().startswith(query.file_filter.lower()):
                    continue
                completions.append(sublime.CompletionItem(
                    trigger=ws_name + "/",
                    annotation=f"📦 {ws_name} (workspace)",
                    completion=ws_name + "/",
                    kind=sublime.KIND_NAMESPACE
                ))

            # 1.4 Priority 4: Open Tabs
            completions.extend(
                cls.get_open_files_completions(
                    window, active_cwd, other_workspaces, query.file_filter, chat_view_flag
                )
            )

        # 2. Subdirectory Level Completion (query.dir_part is specified)
        else:
            target_dir = cls.resolve_target_dir(active_cwd, other_workspaces, query.dir_part)
            if not target_dir:
                return sublime.CompletionList([], flags=flags)

            sub_dirs, sub_files = cls.scan_directory(target_dir, query.file_filter)
            if not sub_dirs and not sub_files:
                return sublime.CompletionList([], flags=flags)

            for d in sub_dirs:
                completions.append(sublime.CompletionItem(
                    trigger=d + "/",
                    annotation="📁 folder",
                    completion=d + "/",
                    kind=sublime.KIND_NAMESPACE
                ))

            for f in sub_files:
                completions.append(sublime.CompletionItem(
                    trigger=f,
                    annotation="📄 file",
                    completion=f,
                    kind=sublime.KIND_VARIABLE
                ))

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
