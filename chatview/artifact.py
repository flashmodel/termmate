"""
End-of-turn artifact.
"""
import logging
import os
import re

import sublime

from . import utils

LOG = logging.getLogger("TermMate")

ARTIFACT_REGION_KEY = "chatview_artifact_files"
ARTIFACT_REGION_SCOPE = "region.bluish"
ARTIFACT_REGION_FLAGS = sublime.DRAW_NO_FILL | sublime.DRAW_NO_OUTLINE | sublime.HIDDEN
DIFF_VIEW_PATH_KEY = "chatview_artifact_diff_path"

_DIFF_HEADER_RE = re.compile(r'^diff [ab]/(.+?) [ab]/.+$|^[+-]{3} [ab]/(.+)$')
_DIFF_HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def diff_view_click(view, abs_path, click_point):
    """Handle a double-click in a diff view.

    Tries to open the file named on the clicked header line, or jump to the
    source line if the clicked line is a hunk range header.

    Returns True if the click was handled (caller should suppress the default
    drag_select command), False otherwise.
    """
    stripped = view.substr(view.line(click_point)).strip()

    m = _DIFF_HEADER_RE.match(stripped)
    if m:
        rel = m.group(1) or m.group(2)
        for folder in (view.window().folders() or []):
            p = os.path.normpath(os.path.join(folder, rel))
            if os.path.isfile(p):
                view.window().open_file(p)
                return True
        sublime.status_message("File not found: " + rel)
        return True

    hunk_m = _DIFF_HUNK_RE.match(stripped)
    if hunk_m:
        line_no = int(hunk_m.group(1))
        if os.path.isfile(abs_path):
            view.window().open_file(
                "{}:{}".format(abs_path, line_no),
                sublime.ENCODED_POSITION)
        else:
            sublime.status_message("File not found")
        return True

    return False


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
            entry["diffs"].append(diff_text)
            a, d = self._count_diff_lines(diff_text)
            entry["add"] += a
            entry["del"] += d
        if abs_path not in self.pending_changed_files:
            self.pending_changed_files.append(abs_path)

    def show(self):
        """Append a collapsed raw-text list of files changed this turn.

        Each file name line gets a clickable region that opens a diff view;
        the list is folded by default and expands via the gutter fold arrow.
        """
        if not self.pending_changed_files:
            return
        files = self.pending_changed_files
        self.pending_changed_files = []

        view = self.view
        # term_chat_output_append inserts at input_start - 1
        base = self.input_start_fn(view) - 1

        count = len(files)
        header = "▣ {} file{} changed".format(count, "s" if count != 1 else "")
        text = header + "\n"
        new_regions = []
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

        # Fold from end of header line to end of last file line (excl. trailing \n)
        fold_start = base + len(header)
        fold_end = base + len(text) - 1

        # Non-blank zero-indent terminator on the line after the gutter fold arrow
        # The char below is (NBSP): renders blank, not indent whitespace.
        text += " "

        view.run_command("term_chat_output_append", {"text": text})

        self.file_regions.extend(new_regions)  # (Region, abs_path, rel_path, diffs_snapshot)
        self._redraw_regions()
        view.fold(sublime.Region(fold_start, fold_end))

    def _redraw_regions(self):
        """Redraw underline regions for all artifact file name lines."""
        if self.file_regions:
            self.view.add_regions(
                ARTIFACT_REGION_KEY,
                [r for r, *_ in self.file_regions],
                ARTIFACT_REGION_SCOPE,
                "",
                ARTIFACT_REGION_FLAGS
            )
        else:
            self.view.erase_regions(ARTIFACT_REGION_KEY)

    def open_diff_at(self, point):
        """If point is on an artifact file name, open its diff view. Returns True if handled."""
        if point is None:
            return False
        # Let clicks on a folded (collapsed) artifact expand it natively
        if hasattr(self.view, "folded_regions"):
            for folded in self.view.folded_regions():
                if folded.contains(point):
                    return False
        for region, abs_path, rel_path, diffs in self.file_regions:
            if region.contains(point):
                self._open_diff(abs_path, rel_path, diffs)
                return True
        return False

    def _open_diff(self, abs_path, rel_path, diffs):
        """Open a read-only diff view for the given turn's diffs."""
        if not diffs:
            sublime.status_message("No recorded changes for this file")
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
        diff_view = utils.show_diff_text(self.window, "".join(parts), name)
        diff_view.settings().set(DIFF_VIEW_PATH_KEY, abs_path)

    def truncate(self, cut_point):
        """Drop artifact file regions that fall inside a truncated tail (rewind)."""
        self.pending_changed_files = []
        self.file_changes = {}
        self.file_regions = [
            (r, p, rel, diffs) for r, p, rel, diffs in self.file_regions if r.end() < cut_point
        ]
        self._redraw_regions()

    def clear(self):
        """Reset all recorded file changes and artifact regions."""
        self.file_changes = {}
        self.pending_changed_files = []
        self.file_regions = []
        self.view.erase_regions(ARTIFACT_REGION_KEY)
