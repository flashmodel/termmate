"""
End-of-turn artifact.
"""
import logging
import os
import re

import sublime

from . import utils
from . import diffview

LOG = logging.getLogger("TermMate")

ARTIFACT_REGION_KEY = "chatview_artifact_files"
ARTIFACT_REGION_SCOPE = "region.bluish"
ARTIFACT_REGION_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.HIDDEN
DIFF_VIEW_PATH_KEY = diffview.DIFF_VIEW_PATH_KEY


def diff_view_click(view, abs_path, click_point):
    """Handle a double-click in a diff view by delegating to diffview module."""
    return diffview.handle_diff_view_click(view, abs_path, click_point)



def _agent_data_dirs(extra_env=None):
    """Return absolute paths of agent-private data dirs (memory, sessions, config).

    Resolution matches how agents are spawned: settings ``env`` (extra_env)
    overrides the plugin process environment, then falls back to defaults.
    """
    env = dict(os.environ)
    env.update(extra_env or {})
    dirs = [
        env.get("CLAUDE_CONFIG_DIR") or "~/.claude",
        env.get("CODEX_HOME") or "~/.codex",
        "~/.pi",  # pi CLI has no dir override (see genfoundry/pi_agent.py)
        env.get("GEMINI_CLI_DIR") or "~/.gemini/antigravity-cli",
        "~/.gemini/antigravity-ide",
        "~/.gemini",
    ]
    return [os.path.realpath(os.path.expanduser(d)) for d in dirs]


def is_agent_data_path(abs_path, extra_env=None):
    """True if abs_path is inside an agent's private data directory."""
    if not abs_path:
        return False
    try:
        real = os.path.realpath(abs_path)
    except (OSError, ValueError):
        return False
    for d in _agent_data_dirs(extra_env):
        if real == d or real.startswith(d + os.sep):
            return True
    return False


class FileChangesArtifact:
    """
    Per-session store and renderer for the file changes artifact.

    Records edit diffs during a conversation turn and renders a collapsed,
    clickable raw-text list of changed files in the chat view when the
    conversation stops.

    :param view: the chat view the artifact is rendered into.
    :param window: the Sublime window (used to open diff views).
    :param input_start_fn: callable(view) returning the chat input start position.
    """

    def __init__(self, view, window, input_start_fn):
        self.view = view
        self.window = window
        self.input_start_fn = input_start_fn
        self.file_changes = {}  # abs_path -> {"rel_path": str, "diffs": [], "add": int, "del": int}
        self.pending_changed_files = []  # abs paths changed since last render
        self.file_regions = []  # List of (Region, abs_path, rel_path, diffs_snapshot) for rendered file lines
        self.plan_artifacts = {}  # abs_path -> {"name": str, "summary": str, "content": str, "can_execute": bool}
        self.pending_plan_artifacts = []
        self.plan_regions = []  # List of (Region, abs_path, item, action)

    @staticmethod
    def _count_diff_lines(diff_text):
        """Return (additions, deletions) from unified diff text."""
        add = del_ = 0
        for line in (diff_text or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                add += 1
            elif line.startswith("-") and not line.startswith("---"):
                del_ += 1
        return add, del_

    def record(self, abs_path, rel_path, diff_text, extra_env=None):
        """Record an edit diff so the file is listed in the end-of-turn artifact.

        Edits inside agent-private data dirs (e.g. Claude memory files under
        ~/.claude) are agent housekeeping, not project changes, and are skipped.
        """
        if is_agent_data_path(abs_path, extra_env):
            LOG.debug("Artifact: skipping agent data file %s", abs_path)
            return
        entry = self.file_changes.setdefault(
            abs_path, {"rel_path": rel_path, "diffs": [], "add": 0, "del": 0})
        if diff_text:
            a, d = self._count_diff_lines(diff_text)
            if a or d:
                entry["diffs"].append(diff_text)
                entry["add"] += a
                entry["del"] += d
        if abs_path not in self.pending_changed_files:
            self.pending_changed_files.append(abs_path)

    def record_plan(self, abs_path, name, summary="", content="", can_execute=True, icon=None):
        """Record an agent-generated plan or user artifact to list in the end-of-turn artifact."""
        existing = self.plan_artifacts.get(abs_path, {})
        default_icon = "📋" if (can_execute if can_execute is not None else True) else "📖"
        self.plan_artifacts[abs_path] = {
            "name": name or existing.get("name") or os.path.basename(abs_path),
            "summary": summary or existing.get("summary", ""),
            "content": content or existing.get("content", ""),
            "can_execute": can_execute if can_execute is not None else existing.get("can_execute", True),
            "icon": icon or existing.get("icon") or default_icon,
        }
        if abs_path not in self.pending_plan_artifacts:
            self.pending_plan_artifacts.append(abs_path)

    def show(self):
        """Append a collapsed raw-text list of files and plans changed/created this turn.

        Each file name line gets a clickable region that opens a diff view;
        each plan line gets clickable regions to view the plan or execute it.
        The list is folded by default and expands via the gutter fold arrow.
        """
        if not self.pending_changed_files and not self.pending_plan_artifacts:
            return
        files = self.pending_changed_files
        self.pending_changed_files = []
        plans = self.pending_plan_artifacts
        self.pending_plan_artifacts = []

        view = self.view
        # term_chat_output_append inserts at input_start - 1
        base = self.input_start_fn(view) - 1

        text = "\n"

        def _artifact_priority(abs_path):
            item = self.plan_artifacts.get(abs_path, {})
            name = (item.get("name") or os.path.basename(abs_path) or "").lower()
            if "implementation_plan" in name or name == "plan.md":
                return 0
            if "walkthrough" in name:
                return 1
            return 2

        plans.sort(key=_artifact_priority)

        new_plan_regions = []
        for abs_path in plans:
            item = self.plan_artifacts.pop(abs_path, {})
            name = item.get("name") or os.path.basename(abs_path) or "plan.md"

            row_view = f"▣ {name}"
            start_view = base + len(text)
            text += row_view + "\n"
            new_plan_regions.append((sublime.Region(start_view, start_view + len(row_view)), abs_path, item, "view"))

        new_regions = []
        fold_start = None
        fold_end = None
        if files:
            file_header = f"▣ {len(files)} file{'s' if len(files) != 1 else ''} changed"
            header_start = base + len(text)
            text += file_header + "\n"
            fold_start = header_start + len(file_header)

            for abs_path in files:
                entry = self.file_changes.pop(abs_path, {})
                rel = entry.get("rel_path") or abs_path
                add = entry.get("add", 0)
                del_ = entry.get("del", 0)
                stat = "  +{} -{}".format(add, del_) if (add or del_) else ""
                row = "    " + rel + stat
                start = base + len(text)
                text += row + "\n"
                new_regions.append((sublime.Region(start, start + len(row)), abs_path, rel, list(entry.get("diffs", []))))

            # Fold from end of file header line to end of last file line (excl. trailing \n)
            fold_end = base + len(text) - 1

            # Non-blank zero-indent terminator on the line after the gutter fold arrow
            # The char below is (NBSP): renders blank, not indent whitespace.
            text += " "

        view.run_command("term_chat_output_append", {"text": text})

        self.plan_regions.extend(new_plan_regions)
        self.file_regions.extend(new_regions)
        self._redraw_regions()
        if fold_start is not None and fold_end is not None:
            view.fold(sublime.Region(fold_start, fold_end))

    def _redraw_regions(self):
        """Redraw underline regions for all artifact file name lines and plan items."""
        all_regions = [r for r, *_ in self.file_regions] + [r for r, *_ in self.plan_regions]
        if all_regions:
            self.view.add_regions(
                ARTIFACT_REGION_KEY,
                all_regions,
                ARTIFACT_REGION_SCOPE,
                "",
                ARTIFACT_REGION_FLAGS
            )
        else:
            self.view.erase_regions(ARTIFACT_REGION_KEY)

    def is_execute_at(self, point):
        """True if point is on an artifact [Execute] button."""
        if point is None:
            return False
        for region, abs_path, item, action in self.plan_regions:
            if action == "execute" and region.contains(point):
                return True
        return False

    def is_plan_at(self, point):
        """True if point is on any artifact plan region (view or execute)."""
        if point is None:
            return False
        for region, abs_path, item, action in self.plan_regions:
            if region.contains(point):
                return True
        return False

    def open_artifact_at(self, point):
        """If point is on an artifact (plan or file diff), open its view or execute. Returns True if handled."""
        if point is None:
            return False
        # Let clicks on a folded (collapsed) artifact expand it natively
        if hasattr(self.view, "folded_regions"):
            for folded in self.view.folded_regions():
                if folded.contains(point):
                    return False

        for region, abs_path, item, action in self.plan_regions:
            if region.contains(point):
                if action == "execute":
                    name = item.get("name") or os.path.basename(abs_path) or "plan"
                    if sublime.ok_cancel_dialog(f"Implement {name}?", "Execute"):
                        self.window.run_command("term_chat_implement_plan")
                    return True
                elif action == "view":
                    self._open_plan_view(abs_path, item)
                    return True

        for region, abs_path, rel_path, diffs in self.file_regions:
            if region.contains(point):
                self._open_diff(abs_path, rel_path, diffs)
                return True
        return False

    def open_diff_at(self, point):
        """If point is on an artifact file name, open its diff view. Returns True if handled."""
        return self.open_artifact_at(point)

    def _open_plan_view(self, abs_path, item):
        """Open or focus a view displaying the plan markdown."""
        if abs_path and os.path.isfile(abs_path):
            self.window.open_file(abs_path)
            return

        for v in self.window.views():
            if (v.settings().get("chatview_plan_path") == abs_path
                    or (abs_path and v.file_name() == abs_path)):
                self.window.focus_view(v)
                return

        content = item.get("content", "")
        v = self.window.new_file()
        name = item.get("name") or "implementation_plan.md"
        v.set_name(name)
        if abs_path:
            v.settings().set("chatview_plan_path", abs_path)
        v.run_command("append", {"characters": content or ""})
        v.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
        v.set_scratch(True)
        v.set_read_only(True)

    def _open_diff(self, abs_path, rel_path, diffs):
        """Open read-only diff view for diffs, or open source file if no diffs recorded."""
        if not any(d and d.strip() for d in (diffs or [])):
            target = diffview._resolve_file_path(self.window, rel_path, abs_path)
            if target and os.path.isfile(target):
                self.window.open_file(target)
            elif abs_path:
                self.window.open_file(abs_path)
            else:
                sublime.status_message("Source file not found: " + rel_path)
            return

        parts = ["diff a/{0} b/{0}\n--- a/{0}\n+++ b/{0}\n".format(rel_path)]
        for d in diffs:
            parts.append(d if d.endswith("\n") else d + "\n")

        # Close a stale diff view for the same file before reopening
        for v in self.window.views():
            if v.settings().get(DIFF_VIEW_PATH_KEY) == abs_path:
                v.close()
                break

        name = os.path.basename(rel_path) + " (changes)"
        diffview.show_diff_text(self.window, "".join(parts), name, abs_path=abs_path)

    def truncate(self, cut_point):
        """Drop artifact file regions that fall inside a truncated tail (rewind)."""
        self.pending_changed_files = []
        self.pending_plan_artifacts = []
        self.file_changes = {}
        self.plan_artifacts = {}
        self.file_regions = [
            (r, p, rel, diffs) for r, p, rel, diffs in self.file_regions if r.end() < cut_point
        ]
        self.plan_regions = [
            (r, p, item, act) for r, p, item, act in self.plan_regions if r.end() < cut_point
        ]
        self._redraw_regions()

    def clear(self):
        """Reset all recorded file changes and artifact regions."""
        self.file_changes = {}
        self.pending_changed_files = []
        self.plan_artifacts = {}
        self.pending_plan_artifacts = []
        self.file_regions = []
        self.plan_regions = []
        self.view.erase_regions(ARTIFACT_REGION_KEY)
