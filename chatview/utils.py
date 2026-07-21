import logging
import difflib
import sublime


LOG = logging.getLogger("TermMate")

def get_log_level(level_name):
    """Maps log level names to logging constants."""
    return getattr(logging, level_name.upper(), logging.ERROR)


def update_log_level(settings):
    """
    Reads the log_level from settings and reconfigures the logger.
    """
    level_name = settings.get("log_level", "ERROR")
    level = get_log_level(level_name)
    LOG.setLevel(level)
    LOG.propagate = False
    if not LOG.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def show_diff(window, old_text, new_text, name):
    """
    Generate and show a git-style unified diff between old and new text.

    Args:
        window: Sublime Text window to create the view in
        old_text: Original text content
        new_text: Modified text content
        name: Name for the diff view tab
    """
    a = old_text.splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)

    # Generate unified diff with context
    diff_lines = list(difflib.unified_diff(
        a, b,
        fromfile="a/" + name,
        tofile="b/" + name,
        lineterm='',
        n=5  # lines of context
    ))

    if not diff_lines:
        sublime.status_message("No changes")
        return

    # Build git-style diff output
    output_parts = []

    # Add git diff header
    output_parts.append(f"diff a/{name} b/{name}\n")
    output_parts.append(f"--- a/{name}\n")
    output_parts.append(f"+++ b/{name}\n")

    # Add the actual diff content (skip the default --- +++ lines from unified_diff)
    for line in diff_lines[2:]:
        if not line.endswith('\n'):
            line += '\n'
        output_parts.append(line)

    difftxt = "".join(output_parts)

    # Create and configure the diff view
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    v.assign_syntax('Packages/Diff/Diff.sublime-syntax')
    v.run_command('append', {'characters': difftxt, 'disable_tab_translation': True})
    v.set_read_only(True)


def show_diff_text(window, diff_text, name):
    """
    Show pre-built unified diff text in a new read-only scratch view.

    Returns the created view.
    """
    v = window.new_file()
    v.set_name(name)
    v.set_scratch(True)
    v.assign_syntax('Packages/Diff/Diff.sublime-syntax')
    v.run_command('append', {'characters': diff_text, 'disable_tab_translation': True})
    v.set_read_only(True)
    return v
