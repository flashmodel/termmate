import logging
import html
import sublime

from .utils import format_token_count


LOG = logging.getLogger("TermMate")


class TablePhantomManager:
    """Own folded Markdown table regions and their HTML phantoms."""

    REGION_PREFIX = "term_chat_html_table_"

    def __init__(self, view):
        self.view = view
        self.phantom_set = sublime.PhantomSet(view, "chatview_tables")
        self.entries = []
        self.next_id = 0

    def add(self, region, table_html):
        key = self.REGION_PREFIX + str(self.next_id)
        self.next_id += 1
        self.view.add_regions(
            key,
            [region],
            flags=sublime.HIDDEN,
        )
        self.entries.append((key, table_html))
        self.view.fold(region)
        self._update()

    def _update(self):
        phantoms = []
        active_entries = []
        for key, table_html in self.entries:
            regions = self.view.get_regions(key)
            if not regions:
                continue
            region = regions[0]
            active_entries.append((key, table_html))
            # Anchor at the fold's end boundary so the phantom itself is not
            # swallowed by the folded region.
            anchor = sublime.Region(region.end(), region.end())
            phantoms.append(sublime.Phantom(
                anchor,
                table_html,
                sublime.LAYOUT_BLOCK,
            ))
        self.entries = active_entries
        self.phantom_set.update(phantoms)

    def refresh(self):
        """Redraw all table phantoms using their cached HTML."""
        self._update()

    def truncate(self, cut_point):
        kept = []
        for key, table_html in self.entries:
            regions = self.view.get_regions(key)
            if regions and regions[0].end() < cut_point:
                kept.append((key, table_html))
            else:
                self.view.erase_regions(key)
        self.entries = kept
        self._update()

    def clear(self):
        for key, _ in self.entries:
            self.view.erase_regions(key)
        self.entries = []
        self.phantom_set.update([])


class NoticePhantom:
    """Displays a notice without writing it to the buffer."""

    def __init__(self, view, input_start_provider):
        self.view = view
        self.input_start_provider = input_start_provider
        self.phantom_set = sublime.PhantomSet(view, "chatview_notice")
        self.message = None

    def show(self, message):
        self.message = message
        self.update()

    def update(self):
        """Render the current notice immediately above the input controls."""
        if not self.message:
            self.phantom_set.update([])
            return

        input_start = self.input_start_provider()
        region_start = max(0, input_start - 1)
        region = sublime.Region(region_start, input_start)
        message = html.escape(self.message)

        notice_html = f"""
        <body id="chatview-notice" style="margin:0;padding:0">
            <style>
                .notice {{
                    margin: 6px 0;
                    padding: 5px 8px;
                    border-left: 2px solid color(var(--accent) alpha(0.55));
                    background-color: color(var(--accent) alpha(0.06));
                    font-family: var(--font-mono);
                    font-size: 0.85em;
                }}
                .notice-message {{
                    color: var(--foreground);
                    font-weight: bold;
                }}
            </style>
            <div class="notice">
                <span class="notice-message">ℹ️ {message}</span>
            </div>
        </body>
        """
        self.phantom_set.update([sublime.Phantom(
            region,
            notice_html,
            sublime.LAYOUT_BLOCK,
        )])

    def clear(self):
        self.message = None
        self.phantom_set.update([])


class StatusHint:
    """Renders the inline stop-status control shown in the model panel."""

    def __init__(self, text="stopping…"):
        self.text = text
        self.visible = False
        self.stopping = False

    def set_visible(self, visible):
        """Show or hide the control, resetting its stopping state."""
        self.visible = visible
        self.stopping = False

    def set_stopping(self, stopping, text=None):
        """Update stop state and return whether the rendered hint changed."""
        text_changed = text is not None and text != self.text
        if text is not None:
            self.set_text(text)
        state_changed = stopping != self.stopping
        self.stopping = stopping
        return state_changed or (stopping and text_changed)

    def set_text(self, text):
        """Update the text displayed while a stop is being processed."""
        self.text = text

    update = set_text

    def render(self):
        """Return the stop control HTML."""
        if not self.visible:
            return ""

        label = f"■ {self.text}" if self.stopping else "■"
        shortcut = "⌘+Esc" if sublime.platform() == "osx" else "shift+esc"
        title = f"Stop conversation ({shortcut})"
        return (
            f'<a href="stop_conversation" class="stop-hint" title="{title}">{label}</a>'
        )


class LoadingAnimation:
    """
    Manages a loading animation phantom with start/stop control.
    """
    def __init__(self, view):
        self.view = view
        self.phantom_set = sublime.PhantomSet(view, "chatview_loading")
        self.is_loading = False
        self.loading_text = None
        self.frame_index = 0
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def start(self, region, text=None):
        """Start the loading animation at the specified region."""
        # ALWAYS update the region provider, even if already loading
        self.region_provider = region
        self.loading_text = text

        if not self.is_loading:
            self.is_loading = True
            self.frame_index = 0
            self._update_animation()

    def stop(self):
        """Stop the loading animation and clear the phantom."""
        self.is_loading = False
        # Clear on next tick to avoid thread issues if called from background
        sublime.set_timeout(lambda: self.phantom_set.update([]), 0)

    def _update_animation(self):
        """Update the loading animation frame."""
        if not self.is_loading:
            return

        # Resolve current region
        if callable(self.region_provider):
            region = self.region_provider()
        else:
            region = self.region_provider

        frame = self.frames[self.frame_index % len(self.frames)]

        text_html = ""
        if self.loading_text:
            text_html = f" <span style='font-weight: normal; opacity: 0.8;'>{self.loading_text}</span>"

        html = f"""
        <body id="chatview-loading" style="background-color: transparent; margin: 0; padding: 0;">
            <style>
                .loading {{
                    color: var(--accent);
                    background-color: transparent;
                    font-weight: bold;
                    margin-right: 8px;
                    font-family: var(--font-mono);
                }}
            </style>
            <div class="loading">{frame}{text_html}</div>
        </body>
        """

        self.phantom_set.update([sublime.Phantom(
            region,
            html,
            sublime.LAYOUT_BLOCK
        )])

        # Schedule next frame
        self.frame_index += 1
        sublime.set_timeout(lambda: self._update_animation(), 100)


class RewindConfirmPanel:
    """
    Shows a floating popup (above all text) when hovering a prompt gutter dot.
    Confirm triggers the rewind; Cancel or moving away dismisses it.
    """

    def __init__(self, view):
        self.view = view
        self._on_confirm = None
        self._visible = False

    def show(self, region, prompt_index, on_confirm):
        """Show the popup anchored at the start of the prompt region."""
        self._on_confirm = on_confirm
        self._visible = True

        html = f"""
        <body id="chatview-rewind-confirm">
            <style>
                body {{ margin: 0; padding: 0; }}
                .dialog {{
                    padding: 16px 18px 14px 18px;
                    min-width: 260px;
                    background-color: color(var(--background) blend(var(--foreground) 88%));
                    border: 1px solid color(var(--foreground) alpha(0.15));
                    border-radius: 5px;
                }}
                .title {{
                    color: var(--orangish);
                    font-weight: bold;
                    font-size: 1em;
                    font-family: var(--font-mono);
                    margin-bottom: 4px;
                }}
                .subtitle {{
                    color: color(var(--foreground) alpha(0.6));
                    font-size: 0.82em;
                    margin-bottom: 14px;
                }}
                .actions {{
                    margin-top: 4px;
                    margin-bottom: 4px;
                }}
                .btn {{
                    display: inline-block;
                    text-decoration: none;
                    padding: 5px 16px;
                    border-radius: 3px;
                    font-weight: bold;
                    font-size: 0.88em;
                    margin-right: 8px;
                }}
                .btn-confirm {{
                    background-color: var(--orangish);
                    color: var(--background);
                }}
                .btn-cancel {{
                    background-color: color(var(--background) blend(var(--foreground) 75%));
                    color: var(--foreground);
                    border: 1px solid color(var(--foreground) alpha(0.2));
                    margin-left: 8px;
                }}
            </style>
            <div class="dialog">
                <div class="title">↩ Restore conversation</div>
                <div class="subtitle">Restore files and conversaction to this point, later message will be discarded.</div>
                <div class="actions">
                    <a href="confirm" class="btn btn-confirm">Restore</a>
                    <a href="cancel" class="btn btn-cancel">Cancel</a>
                </div>
            </div>
        </body>
        """

        def on_navigate(href):
            callback = self._on_confirm
            self.clear()
            if href == "confirm" and callback:
                callback()

        def on_hide():
            self._visible = False
            self._on_confirm = None

        self.view.show_popup(
            html,
            location=region.begin(),
            flags=0,
            max_width=560,
            on_navigate=on_navigate,
            on_hide=on_hide,
        )

    def clear(self):
        """Dismiss the popup."""
        self._visible = False
        self._on_confirm = None
        self.view.hide_popup()

    @property
    def visible(self):
        return self._visible


class TokenSummaryPhantom:
    """
    Renders a block-level phantom displaying turn token metrics at the
    boundary above the ModelPanel/input prompt once a turn completes.
    Cleared when a new prompt is submitted or conversation is cleared.
    """

    def __init__(self, view, input_start_provider):
        self.view = view
        self.input_start_provider = input_start_provider
        self.phantom_set = sublime.PhantomSet(view, "chatview_token_summary")
        self.token_usage = None

    def show(self, token_usage=None):
        """Display token metrics for the completed turn."""
        if token_usage is not None:
            self.token_usage = token_usage
        self.update()

    def update(self):
        """Render or redraw the phantom at Region(max(0, input_start - 1), input_start)."""
        if not self.token_usage:
            self.phantom_set.update([])
            return

        last = self.token_usage.get("last") or {}
        total = self.token_usage.get("total") or {}
        last_tokens = (
            last.get("totalTokens", 0)
            or (last.get("inputTokens", 0) + last.get("outputTokens", 0))
        )
        total_tokens = (
            total.get("totalTokens", 0)
            or (total.get("inputTokens", 0) + total.get("outputTokens", 0))
        )
        if not last_tokens and not total_tokens:
            self.phantom_set.update([])
            return

        input_start = self.input_start_provider()
        region = sublime.Region(max(0, input_start - 1), input_start)

        html_content = self._render_html(self.token_usage)
        self.phantom_set.update([
            sublime.Phantom(region, html_content, sublime.LAYOUT_BLOCK)
        ])

    def clear(self):
        """Remove the phantom completely."""
        self.token_usage = None
        self.phantom_set.update([])

    def _render_html(self, usage):
        """
        Render minihtml markup displaying turn token metrics.
        Example:
            ⚡ 34.8k token(in:3.1k, out:217, 91% cached)
               Context:13.4%(258k) · Total:338k
        """
        last = usage.get("last") or {}
        total = usage.get("total") or {}
        ctx_window = usage.get("modelContextWindow") or 0

        last_tokens = (
            last.get("totalTokens", 0)
            or (last.get("inputTokens", 0) + last.get("outputTokens", 0))
        )
        input_tokens = last.get("inputTokens", 0)
        output_tokens = last.get("outputTokens", 0)
        cached_tokens = last.get("cachedInputTokens", 0)
        non_cached_input = max(0, input_tokens - cached_tokens)
        total_tokens = (
            total.get("totalTokens", 0)
            or (total.get("inputTokens", 0) + total.get("outputTokens", 0))
        )

        # Thinking tokens
        thinking_html = ""
        thinking_tokens = (
            last.get("reasoningOutputTokens")
            or last.get("thinkingTokens")
            or 0
        )
        if thinking_tokens > 0:
            thinking_html = f", thinking:{format_token_count(thinking_tokens)}"

        # Cache percentage
        cache_html = ""
        if cached_tokens > 0 and input_tokens > 0:
            cache_pct = min(100, round((cached_tokens / input_tokens) * 100))
            cache_html = f", {cache_pct}% cached"

        # Context window percentage
        ctx_html = ""
        ctx_tokens = last.get("activeContextTokens") or input_tokens
        if ctx_window > 0 and ctx_tokens > 0:
            pct = (ctx_tokens / ctx_window) * 100
            ctx_text = f"{pct:.1f}%"
            ctx_html = (
                f' <span class="item">Context:<span class="highlight">{ctx_text}</span>'
                f'<span class="dim">({format_token_count(ctx_window)})</span></span>'
            )

        # Session total
        total_html = ""
        if total_tokens > 0 and total_tokens != last_tokens:
            total_html = (
                f'<span class="sep">·</span>'
                f'<span class="item">Total:{format_token_count(total_tokens)}</span>'
            )

        metrics_html = (
            f'<span class="icon">⚡</span>'
            f'<span class="item"><span class="highlight">'
            f'{format_token_count(last_tokens)}</span> token</span>'
            f'<span class="dim">(in:{format_token_count(non_cached_input)}, '
            f'out:{format_token_count(output_tokens)}{thinking_html}{cache_html})</span>'
            f'{ctx_html}{total_html}'
        )

        return f"""
        <body id="chatview-token-summary" style="margin:0;padding:0;">
            <style>
                .metrics {{
                    margin: 0;
                    padding: 2px 0 4px 0;
                    font-family: var(--font-mono);
                    font-size: 0.85em;
                    color: color(var(--foreground) alpha(0.7));
                }}
                .icon {{ color: color(var(--foreground) alpha(0.6)); padding-right: 5px; }}
                .highlight {{ color: color(var(--foreground) alpha(0.82)); font-weight: bold; }}
                .dim {{ color: color(var(--foreground) alpha(0.5)); font-size: 0.9em; }}
                .sep {{ color: color(var(--foreground) alpha(0.25)); padding: 0 0.15em; }}
            </style>
            <div class="metrics">{metrics_html}</div>
        </body>
        """
