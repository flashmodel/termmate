"""
Plan usage limits shown in the Agent / Model / PlanMode bar (Claude Code only).

After each reply the Claude CLI sends a `rate_limit_event` whose `unifiedWindows` hold one entry per limit
(`five_hour`, `seven_day`, and per-model weekly windows when they apply) with `utilization` (0 to 1) and
`resetsAt` (unix seconds). This module turns those into the bar tag and the popup; it has no Sublime dependency.
"""
import html
import time

BAR_WIDTH = 20  # minimum; bars widen to the popup's widest heading line
# Windows shown first in the bar tag (session · weekly); the other weekly windows follow, numbers only, so the
# tag stays short; the popup lists all of them with full labels
TAG_WINDOWS = ("five_hour", "seven_day")
MODEL_PREFIX = "seven_day_"

NO_DATA_HTML = (
    '<body style="margin:0.5em"><b style="opacity:0.6">Plan usage limits</b><br><br>'
    'Shown after the first reply of this chat.</body>'
)

_FIXED_LABELS = {
    "five_hour": "Session limit",
    "seven_day": "Weekly · all models",
}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def parse_windows(info):
    """Return {window: (utilization, resets_at)} from a rate_limit_info dict; windows without a number are skipped."""
    windows = {}
    if not isinstance(info, dict):
        return windows
    for key, win in (info.get("unifiedWindows") or {}).items():
        if not isinstance(win, dict):
            continue
        util = win.get("utilization")
        if isinstance(util, bool) or not isinstance(util, (int, float)):
            continue
        resets = win.get("resetsAt")
        if isinstance(resets, bool) or not isinstance(resets, (int, float)):
            resets = None
        windows[key] = (min(max(float(util), 0.0), 1.0), resets)
    return windows


def model_name(key):
    """`seven_day_opus` becomes `Opus`; "" for windows that are not per-model."""
    if key.startswith(MODEL_PREFIX) and len(key) > len(MODEL_PREFIX):
        return key[len(MODEL_PREFIX):].replace("_", " ").title()
    return ""


def label(key):
    """Display name of a window key: `seven_day_opus` becomes `Weekly · Opus`."""
    if key in _FIXED_LABELS:
        return _FIXED_LABELS[key]
    model = model_name(key)
    if model:
        return "Weekly · " + model
    return key.replace("_", " ").capitalize()


def _sort_key(key):
    order = {"five_hour": 0, "seven_day": 1}
    return (order.get(key, 2), key)


def live_windows(windows, now=None):
    """Windows still open: one whose reset time has passed is stale until the next event reports it."""
    now = time.time() if now is None else now
    return {k: v for k, v in windows.items() if v[1] is None or v[1] > now}


def percent(util):
    return int(round(util * 100))


def format_reset(resets_at, now=None):
    """`Resets in 1 hr 25 min` within a day, else `Resets Mon 4:00 AM` (local time)."""
    if resets_at is None:
        return ""
    now = time.time() if now is None else now
    seconds = int(resets_at - now)
    if seconds <= 0:
        return "Resets now"
    if seconds < 24 * 3600:
        hours, minutes = divmod(seconds // 60, 60)
        if hours:
            return f"Resets in {hours} hr {minutes} min"
        return f"Resets in {max(minutes, 1)} min"
    local = time.localtime(resets_at)
    hour = local.tm_hour % 12 or 12
    suffix = "AM" if local.tm_hour < 12 else "PM"
    return f"Resets {_WEEKDAYS[local.tm_wday]} {hour}:{local.tm_min:02d} {suffix}"


def level_color(util):
    """Color-scheme color for a filling window: red from 90%, orange from 70%, yellow from 60%, else None (the
    accent)."""
    used = percent(util)
    if used >= 90:
        return "var(--redish)"
    if used >= 70:
        return "var(--orangish)"
    if used >= 60:
        return "var(--yellowish)"
    return None


def _colored_percent(util):
    color = level_color(util)
    text = f"{percent(util)}%"
    return f'<span style="color:{color}">{text}</span>' if color else text


def tag_text(windows, now=None):
    """`17% · 8% · 50%` for the bar tag (session, weekly, then each other weekly window, e.g. per model), or ""
    when nothing is known; the popup names them. HTML: a percentage from 60% is wrapped in a colored span."""
    live = live_windows(windows, now)
    parts = [_colored_percent(live[k][0]) for k in TAG_WINDOWS if k in live]
    parts += [_colored_percent(live[k][0]) for k in sorted(live, key=_sort_key) if model_name(k)]
    return " · ".join(parts)


def popup_html(windows, now=None):
    """HTML for the popup: one row per window with its percentage, a bar and the reset time."""
    live = live_windows(windows, now)
    heads = [(key, label(key), format_reset(live[key][1], now), f"{percent(live[key][0])}%")
             for key in sorted(live, key=_sort_key)]
    # Bars as wide as the widest heading line, so they span the popup (its font is the view's monospace one)
    width = max([BAR_WIDTH] + [len(name) + len(reset) + len(pct) + 4 for _, name, reset, pct in heads])
    rows = []
    for key, name, reset, _ in heads:
        util = live[key][0]
        filled = int(round(util * width))
        bar = (f'<span style="color:{level_color(util) or "var(--accent)"}">{"█" * filled}</span>'
               f'<span style="color:color(var(--foreground) alpha(0.25))">{"█" * (width - filled)}</span>')
        rows.append(
            f'<div style="margin-bottom:0.6em">'
            f'<b>{html.escape(name)}</b>&nbsp;&nbsp;'
            f'<span style="opacity:0.6">{html.escape(reset)}&nbsp;&nbsp;</span><b>{_colored_percent(util)}</b>'
            f'<br>{bar}</div>'
        )
    if not rows:
        return ""
    return '<body style="margin:0.5em"><b style="opacity:0.6">Plan usage limits</b><br><br>' + "".join(rows) + "</body>"
