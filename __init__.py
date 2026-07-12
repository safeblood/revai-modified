import re
import json
import os
import traceback
from aqt import mw, gui_hooks
from aqt.utils import showWarning, tooltip
from aqt.operations import CollectionOp

from .config_dialog import (
    ConfigDialog, CONFIG_API_KEY, CONFIG_DEFAULT_MODEL,
    CONFIG_REVIEWER_ACTIONS, CONFIG_MODE,
    CONFIG_DIRECT_API_URL, CONFIG_DIRECT_API_KEY,
    CONFIG_DIRECT_API_NO_PROXY,
)
from .openrouter_client import OpenRouterClient
from .backend_client import BackendClient, AuthError, CreditsExhaustedError, NetworkError
from .auth_dialog import AuthDialog
from .markdown_converter import markdown_to_html
from .streaming_debug import log_debug, clear_debug_log

try:
    from aqt.qt import QThread, pyqtSignal
except Exception:
    try:
        from PyQt6.QtCore import QThread, pyqtSignal
    except Exception:
        try:
            from PyQt5.QtCore import QThread, pyqtSignal
        except Exception:
            QThread = None
            pyqtSignal = None

ADDON_PACKAGE = __name__.split(".")[0]

# Configuration key for the streaming toggle.
CONFIG_ENABLE_STREAMING = "enable_streaming"

# Track if a generation is in progress to prevent double-clicks
_generating = False

# Track the currently running streaming worker so it can be cleaned up.
_current_worker = None

# Cache the last typed answer from the reviewer so AI buttons can access it
# even when mw.reviewer.typedAnswer is no longer available.
_last_typed_answer = ""

# ---------------------------------------------------------------------------
# CSS for injected buttons and content
# ---------------------------------------------------------------------------
INJECTED_CSS = """
#ai-reviewer-buttons, .reviewai-auto-buttons {
    text-align: center; margin: 15px 0; width: 100%;
}
.reviewai-action-block { margin: 10px 0; }
.reviewai-action-bar {
    display: flex; gap: 8px; justify-content: center; flex-wrap: wrap;
}
.reviewai-btn {
    background: rgba(100,126,234,0.85); border: 1px solid rgba(100,126,234,0.6);
    color: white; padding: 8px 18px; border-radius: 18px; font-size: 13px;
    font-weight: 500; cursor: pointer; transition: all 0.2s ease;
}
.reviewai-btn:hover {
    background: rgba(100,126,234,1); transform: translateY(-1px);
    box-shadow: 0 3px 8px rgba(100,126,234,0.4);
}
.reviewai-btn:disabled {
    opacity: 0.5; cursor: wait; transform: none;
}
.reviewai-btn-clear {
    background: rgba(200,80,80,0.7); border: 1px solid rgba(200,80,80,0.5);
    color: white; padding: 8px 18px; border-radius: 18px; font-size: 13px;
    font-weight: 500; cursor: pointer; transition: all 0.2s ease;
}
.reviewai-btn-clear:hover {
    background: rgba(200,80,80,0.9); transform: translateY(-1px);
}
.reviewai-content {
    background: rgba(0,0,0,0.06); border-radius: 10px; padding: 14px 18px;
    margin-top: 8px; text-align: left; line-height: 1.5;
    border: 1px solid rgba(0,0,0,0.08);
    font-size: 14px !important; max-width: 600px; margin-left: auto; margin-right: auto;
}
.reviewai-content strong { color: #4a6fa5; }
.reviewai-content em { color: #6a7a4a; }
.reviewai-content h1, .reviewai-content h2, .reviewai-content h3 {
    margin: 0.3em 0;
}
.reviewai-content ul { padding-left: 1.3em; margin: 0.3em 0; }
.reviewai-content p { margin: 0.3em 0; }
.reviewai-streaming { white-space: pre-wrap; }
.reviewai-login-prompt {
    text-align: center; margin: 15px 0; padding: 12px;
    background: rgba(0,0,0,0.04); border-radius: 10px;
    font-size: 13px; color: #666;
}
.reviewai-login-prompt a { color: #4a6fa5; cursor: pointer; }
.night_mode .reviewai-content {
    background: rgba(255,255,255,0.06); border-color: rgba(255,255,255,0.08);
    color: #d0d0d0;
}
.night_mode .reviewai-content strong { color: #7eb8da; }
.night_mode .reviewai-content em { color: #a8c97a; }
.night_mode .reviewai-btn {
    background: rgba(80,110,200,0.7); border-color: rgba(80,110,200,0.5);
}
.night_mode .reviewai-login-prompt {
    background: rgba(255,255,255,0.05); color: #999;
}
"""


def get_config():
    try:
        return mw.addonManager.getConfig(ADDON_PACKAGE)
    except Exception:
        return None


def construct_prompt(template, note_data):
    def replacer(m):
        return note_data.get(m.group(1), f"{{Field '{m.group(1)}' not found}}")
    return re.sub(r"\{\{([\w\s-]+?)\}\}", replacer, template)


def is_authenticated(config):
    auth = config.get("auth", {})
    return bool(auth.get("access_token") and auth.get("refresh_token"))


def _re_enable_buttons():
    """Re-enable buttons and reset generating flag."""
    global _generating
    _generating = False
    try:
        if mw.reviewer and mw.reviewer.web:
            mw.reviewer.web.eval(
                "document.querySelectorAll('.reviewai-btn').forEach(b => b.disabled = false);"
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Build buttons HTML for matching actions
# ---------------------------------------------------------------------------
def _build_buttons_html(actions, note, card_template=""):
    """Build HTML for action buttons and their content areas.
    If the target field is already in the card template, only show the button (no content area).
    """
    blocks = []
    for action in actions:
        aid = action.get("id", "")
        label = action.get("button_label", "AI Action")
        target_field = action.get("target_field_name", "")

        if not aid or not target_field:
            continue

        # Check if the field is already rendered by the card template
        field_in_template = f"{{{{{target_field}}}}}" in card_template

        # Read existing field content
        field_content = ""
        try:
            if target_field in note.keys():
                field_content = note[target_field] or ""
        except Exception:
            pass

        has_content = bool(field_content.strip())
        clear_display = "inline-block" if has_content else "none"

        # Only show content area below button if field is NOT in the template
        if field_in_template:
            content_html = ""
        else:
            content_display = "block" if has_content else "none"
            safe_content = field_content if has_content else ""
            content_html = (
                f'  <div class="reviewai-content" id="reviewai-content-{aid}" '
                f'    style="display:{content_display}">{safe_content}</div>'
            )

        # Always create a hidden streaming area so live tokens have a target
        # even when the field is rendered by the card template.
        streaming_html = (
            f'  <div class="reviewai-content reviewai-streaming" id="reviewai-streaming-{aid}" '
            f'    style="display:none"></div>'
        )

        blocks.append(
            f'<div class="reviewai-action-block">'
            f'  <div class="reviewai-action-bar">'
            f'    <button class="reviewai-btn" onclick="this.disabled=true;pycmd(\'reviewai_action:{aid}\')">'
            f'{label}</button>'
            f'    <button class="reviewai-btn-clear" style="display:{clear_display}" '
            f'      onclick="pycmd(\'reviewai_clear:{target_field}\')">Clear</button>'
            f'  </div>'
            f'{content_html}'
            f'{streaming_html}'
            f'</div>'
        )

    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Card display hook — inject buttons
# ---------------------------------------------------------------------------
def on_card_will_show(text, card, kind):
    """Inject AI action buttons into cards on the answer side."""
    try:
        return _on_card_will_show_inner(text, card, kind)
    except Exception:
        # Never crash card display — log and return original text
        print(f"RevAI: Error in card_will_show: {traceback.format_exc()}")
        return text


def _on_card_will_show_inner(text, card, kind):
    global _last_typed_answer, _generating

    # Reset per-review state when a new question is shown so a stuck
    # generation flag from a previous card doesn't block the current one.
    # Also capture the typed answer from the question-side input if present.
    if kind == "reviewQuestion":
        _generating = False
        _last_typed_answer = ""
        try:
            if mw.reviewer and mw.reviewer.web:
                typed = mw.reviewer.web.eval(
                    "(function(){var el=document.getElementById('typeans');return el&&(el.value||el.textContent)||'';})();"
                ) or ""
                _last_typed_answer = typed
                log_debug(f"card_will_show question: cached typeans='{typed}'")
        except Exception as e:
            log_debug(f"card_will_show question: failed to read typeans: {e}")

    # Capture typed answer when the answer side is shown; this is the most
    # reliable point to read mw.reviewer.typedAnswer. Keep the question-side
    # value as a fallback if typedAnswer is not yet populated.
    if kind == "reviewAnswer":
        try:
            typed = mw.reviewer.typedAnswer or ""
        except Exception as e:
            log_debug(f"card_will_show: failed to read typedAnswer: {e}")
            typed = ""
        if not typed and _last_typed_answer:
            typed = _last_typed_answer
        elif not typed:
            typed = _get_typed_answer_from_dom()
        _last_typed_answer = typed
        log_debug(f"card_will_show: captured typedAnswer='{_last_typed_answer}'")

    # Only inject on answer side
    if kind not in ("reviewAnswer",):
        return text

    config = get_config()
    if not config:
        return text

    note = card.note()
    note_type_name = note.note_type()["name"]
    actions = [
        a for a in config.get(CONFIG_REVIEWER_ACTIONS, [])
        if a.get("note_type_name") == note_type_name
    ]

    if not actions:
        return text

    # Check if user is authenticated (for backend mode)
    mode = config.get(CONFIG_MODE, "backend")
    if mode == "backend" and not is_authenticated(config):
        style_block = f"<style>{INJECTED_CSS}</style>"
        login_html = (
            '<div class="reviewai-login-prompt">'
            "RevAI: <a onclick=\"pycmd('reviewai_login')\">Sign in</a> "
            "to enable AI buttons"
            "</div>"
        )
        placeholder = '<div id="ai-reviewer-buttons"></div>'
        if placeholder in text:
            return text.replace(placeholder, style_block + login_html)
        else:
            return text + style_block + login_html

    # Get the card template source to check if fields are already referenced
    card_template = card.template().get("afmt", "")

    # Build buttons HTML
    style_block = f"<style>{INJECTED_CSS}</style>"
    buttons_html = _build_buttons_html(actions, note, card_template)

    placeholder = '<div id="ai-reviewer-buttons"></div>'
    if placeholder in text:
        return text.replace(placeholder, style_block + buttons_html)
    else:
        return text + style_block + '<div class="reviewai-auto-buttons">' + buttons_html + '</div>'


gui_hooks.card_will_show.append(on_card_will_show)


# ---------------------------------------------------------------------------
# Streaming generation worker
# ---------------------------------------------------------------------------
if QThread is not None and pyqtSignal is not None:
    class GenerationWorker(QThread):
        """Runs a streaming LLM request in a background thread.

        Signals are automatically marshalled to the receiver's thread, so UI
        updates connected to this worker will run on the main thread.
        """

        token_received = pyqtSignal(str)
        finished = pyqtSignal(str)
        error = pyqtSignal(str)
        started = pyqtSignal()

        def __init__(self, mode, config, action_config, prompt, note_id):
            super().__init__()
            self.mode = mode
            self.config = config
            self.action_config = action_config
            self.prompt = prompt
            self.note_id = note_id

        def run(self):
            log_debug("GenerationWorker.run: started")
            try:
                self.started.emit()
                full_text = self._generate()
                log_debug(f"GenerationWorker.run: finished, len={len(full_text)}")
                self.finished.emit(full_text)
            except Exception as e:
                try:
                    import traceback
                    log_debug(f"GenerationWorker.run: error={e}\n{traceback.format_exc()}")
                except Exception:
                    pass
                self.error.emit(str(e))

        def _generate(self):
            mode = self.mode
            config = self.config

            if mode == "byok":
                api_key = config.get(CONFIG_API_KEY)
                client = OpenRouterClient(api_key)
                return client.generate_stream(
                    config.get(CONFIG_DEFAULT_MODEL),
                    self.prompt,
                    self.token_received.emit,
                )
            elif mode == "direct":
                base_url = config.get(CONFIG_DIRECT_API_URL, "http://127.0.0.1:8081/v1")
                api_key = config.get(CONFIG_DIRECT_API_KEY, "")
                no_proxy = config.get(CONFIG_DIRECT_API_NO_PROXY, True)
                client = OpenRouterClient(api_key, base_url=base_url, no_proxy=no_proxy)
                model = config.get(CONFIG_DEFAULT_MODEL, "")
                # Direct endpoints often use bare model names.
                if "/" in model:
                    model = model.split("/", 1)[1]
                return client.generate_stream(model, self.prompt, self.token_received.emit)
            else:
                auth = config.get("auth", {})
                client = BackendClient(auth.get("access_token"), auth.get("refresh_token"))
                model = config.get(CONFIG_DEFAULT_MODEL)
                full_text, _meta = client.generate_stream(
                    self.prompt,
                    self.token_received.emit,
                    model=model if model else None,
                )
                # Persist refreshed tokens if they changed.
                if client.access_token != auth.get("access_token"):
                    config["auth"]["access_token"] = client.access_token
                    config["auth"]["refresh_token"] = client.refresh_token
                    try:
                        mw.addonManager.writeConfig(ADDON_PACKAGE, config)
                    except Exception:
                        pass
                return full_text
else:
    GenerationWorker = None


# ---------------------------------------------------------------------------
# pycmd handler — AI generation, clear, login
# ---------------------------------------------------------------------------
def on_webview_message(handled_tuple, message, context):
    if not isinstance(message, str):
        return handled_tuple

    if message.startswith("reviewai_action:"):
        action_id = message.split(":", 1)[1]
        log_debug(f"on_webview_message: received reviewai_action:{action_id}")
        _handle_ai_action(action_id)
        return (True, None)

    if message.startswith("reviewai_clear:"):
        field_name = message.split(":", 1)[1]
        log_debug(f"on_webview_message: received reviewai_clear:{field_name}")
        _handle_clear_field(field_name)
        return (True, None)

    if message == "reviewai_login":
        log_debug("on_webview_message: received reviewai_login")
        _handle_login()
        return (True, None)

    return handled_tuple


def _handle_login():
    """Show the auth dialog from the reviewer."""
    try:
        dialog = AuthDialog(mw)
        if dialog.exec():
            tooltip("Signed in! Click an AI button to get started.", period=3000)
            if mw.reviewer:
                mw.reviewer.refresh_if_needed()
    except Exception as e:
        print(f"RevAI: Login error: {e}")
        showWarning(f"Login failed: {e}", title="RevAI")


def _escape_js_string(text):
    """Escape a string for safe inclusion in a JavaScript string literal."""
    return json.dumps(text, ensure_ascii=False)


def _get_typed_answer_from_dom():
    """Fallback: try to read the user's typed answer from the reviewer DOM.

    Anki's built-in type-in-answer may clear mw.reviewer.typedAnswer by the time
    the AI button is clicked. This function reads from the question-side input
    or the answer-side comparison markup.
    """
    try:
        if not mw.reviewer or not mw.reviewer.web:
            return ""
        js = r"""
        (function(){
            // Question-side input.
            var input = document.getElementById('typeans');
            if (input && (input.tagName === 'INPUT' || input.tagName === 'TEXTAREA')) {
                return input.value || '';
            }
            // Answer-side comparison markup.
            for (var cls of ['typeBad','typeMiss']) {
                var els = document.querySelectorAll('.' + cls);
                for (var i = 0; i < els.length; i++) {
                    var txt = els[i].textContent.trim();
                    if (txt) return txt;
                }
            }
            // If spelled correctly, the typed answer equals the correct word.
            var goodEls = document.querySelectorAll('.typeGood');
            for (var j = 0; j < goodEls.length; j++) {
                var txt = goodEls[j].textContent.trim();
                if (txt) return txt;
            }
            return '';
        })();
        """
        return mw.reviewer.web.eval(js) or ""
    except Exception as e:
        log_debug(f"_get_typed_answer_from_dom: error={e}")
        return ""


def _update_streaming_content(action_id, accumulated_text):
    """Show the raw accumulated text in the streaming area while tokens arrive.

    Markdown is only rendered once at the end (_finalize_streaming_content).
    Rendering incomplete markdown during streaming produces broken symbols and
    prevents the smooth, character-by-character effect.
    """
    try:
        if not mw.reviewer or not mw.reviewer.web:
            log_debug("_update_streaming_content: no reviewer/web")
            return
        js = (
            "(function(){"
            f"var el=document.getElementById('reviewai-streaming-{action_id}');"
            "if(!el){"
            f"  el=document.getElementById('reviewai-content-{action_id}');"
            "}"
            "if(!el)return;"
            "el.style.display='block';"
            f"el.textContent={_escape_js_string(accumulated_text)};"
            "})();"
        )
        mw.reviewer.web.eval(js)
        log_debug(f"_update_streaming_content: action_id={action_id}, accumulated_len={len(accumulated_text)}")
    except Exception as e:
        log_debug(f"_update_streaming_content: error={e}")


def _finalize_streaming_content(action_id, html_content):
    """Replace the content area with the final rendered HTML and hide the streaming area."""
    try:
        if not mw.reviewer or not mw.reviewer.web:
            return
        js = (
            "(function(){"
            f"var contentEl=document.getElementById('reviewai-content-{action_id}');"
            f"var streamingEl=document.getElementById('reviewai-streaming-{action_id}');"
            "if(contentEl){"
            "  contentEl.style.display='block';"
            f"  contentEl.innerHTML={_escape_js_string(html_content)};"
            "}"
            "if(streamingEl){"
            "  streamingEl.style.display='none';"
            "}"
            "})();"
        )
        mw.reviewer.web.eval(js)
    except Exception:
        pass


def _save_note_field(note_id, field_name, html_content, typed_answer=""):
    """Save generated HTML (and optional WrongSpelling) to a note field."""
    def op(col):
        n = col.get_note(note_id)
        if not n:
            raise Exception(f"Note {note_id} not found.")
        # Auto-fill WrongSpelling from the typed answer for dictation cards.
        if "WrongSpelling" in n.keys():
            try:
                correct = n["Word"].strip().lower()
            except Exception:
                correct = ""
            typed = typed_answer.strip().lower()
            if typed and typed != correct:
                n["WrongSpelling"] = typed_answer
            else:
                n["WrongSpelling"] = ""
        n[field_name] = html_content
        return col.update_note(n)

    def on_success(_):
        tooltip(f"'{field_name}' updated.", period=2000)
        try:
            if mw.reviewer:
                mw.reviewer.refresh_if_needed()
        except Exception:
            pass

    def on_failure(exc):
        showWarning(f"Failed to save field:\n{exc}", title="RevAI")

    CollectionOp(parent=mw, op=op).success(on_success).failure(on_failure).run_in_background()


def _clear_target_field(note_id, field_name):
    """Clear the target field in the note and refresh the reviewer.

    Called at the start of streaming so the previous analysis disappears
    immediately instead of lingering until the new response is complete.
    """
    def op(col):
        n = col.get_note(note_id)
        if not n:
            raise Exception(f"Note {note_id} not found.")
        n[field_name] = ""
        return col.update_note(n)

    def on_success(_):
        try:
            if mw.reviewer:
                mw.reviewer.refresh_if_needed()
        except Exception:
            pass

    def on_failure(exc):
        showWarning(f"Failed to clear field:\n{exc}", title="RevAI")

    CollectionOp(parent=mw, op=op).success(on_success).failure(on_failure).run_in_background()


def _start_streaming_worker(mode, config, action_config, prompt, note_id, target, label, typed_answer):
    """Start a streaming generation worker and wire up its signals."""
    global _generating, _current_worker

    log_debug("_start_streaming_worker: entering")
    if GenerationWorker is None:
        log_debug("_start_streaming_worker: GenerationWorker is None, aborting")
        _generating = False
        _re_enable_buttons()
        showWarning("Streaming is not available in this Anki/Qt environment.", title="RevAI")
        return

    action_id = action_config.get("id", "")

    worker = GenerationWorker(mode, config, action_config, prompt, note_id)
    _current_worker = worker
    log_debug("_start_streaming_worker: worker created")

    accumulated_text = ""

    def on_started():
        log_debug("worker.on_started")
        _clear_target_field(note_id, target)
        _update_streaming_content(action_id, "Generating...\n\n")

    def on_token(token):
        nonlocal accumulated_text
        accumulated_text += token
        log_debug(f"worker.on_token: len={len(token)}, accumulated={len(accumulated_text)}")
        _update_streaming_content(action_id, accumulated_text)

    def on_finished(full_text):
        global _generating, _current_worker
        log_debug(f"worker.on_finished: len={len(full_text)}")
        _current_worker = None
        _re_enable_buttons()
        html_content = markdown_to_html(full_text)
        _finalize_streaming_content(action_id, html_content)
        _save_note_field(note_id, target, html_content, typed_answer)
        tooltip(f"'{target}' updated.", period=3000)

    def on_error(msg):
        global _generating, _current_worker
        log_debug(f"worker.on_error: {msg[:200]}")
        _current_worker = None
        _re_enable_buttons()
        # Unwrap known error types emitted as strings if possible; otherwise
        # show a generic message.
        if "credits" in msg.lower() or "no credits" in msg.lower():
            showWarning(
                "You've used all your free credits!\n\n"
                "Options:\n"
                "- Redeem a coupon code in RevAI Config\n"
                "- Switch to 'Own API Key' mode with an OpenRouter key\n"
                "- Wait for monthly credit reset (1st of each month)",
                title="RevAI - No Credits",
            )
        elif "not logged in" in msg.lower() or "session expired" in msg.lower():
            showWarning(
                f"Authentication error: {msg}\n\n"
                "Please sign in again via Tools > RevAI Config.",
                title="RevAI",
            )
        elif "timed out" in msg.lower() or "network" in msg.lower() or "connect" in msg.lower():
            showWarning(
                f"Connection problem:\n{msg}\n\n"
                "Check your internet connection and try again.",
                title="RevAI - Network Error",
            )
        else:
            if len(msg) > 500:
                msg = msg[:500] + "..."
            showWarning(f"AI generation failed:\n{msg}", title="RevAI")

    worker.started.connect(on_started)
    worker.token_received.connect(on_token)
    worker.finished.connect(on_finished)
    worker.error.connect(on_error)
    worker.start()
    log_debug("_start_streaming_worker: worker.start() called")


def _handle_ai_action(action_id):
    global _generating

    # Prevent double-click
    if _generating:
        tooltip("Already generating... please wait.", period=2000)
        return
    _generating = True

    def _reset_state():
        global _generating
        _generating = False
        try:
            _re_enable_buttons()
        except Exception:
            pass

    def _run():
        config = get_config()
        if not config:
            _generating = False
            showWarning("RevAI configuration not found.", title="RevAI")
            return

        mode = config.get(CONFIG_MODE, "backend")

        # Validate setup based on mode
        if mode == "byok":
            api_key = config.get(CONFIG_API_KEY)
            model = config.get(CONFIG_DEFAULT_MODEL)
            if not api_key:
                _generating = False
                showWarning("OpenRouter API Key is not configured.\n\n"
                            "Go to Tools > RevAI Config > API Config.", title="RevAI")
                return
            if not model:
                _generating = False
                showWarning("No model selected.\n\n"
                            "Go to Tools > RevAI Config > Model Config.", title="RevAI")
                return
        elif mode == "direct":
            model = config.get(CONFIG_DEFAULT_MODEL)
            if not model:
                _generating = False
                showWarning("No model selected.\n\n"
                            "Go to Tools > RevAI Config > Model Config.", title="RevAI")
                return
        else:
            if not is_authenticated(config):
                _generating = False
                _handle_login()
                return

        reviewer = mw.reviewer
        if not reviewer or not reviewer.card:
            _generating = False
            showWarning("No card is currently being reviewed.", title="RevAI")
            return

        card = reviewer.card
        note = card.note()
        note_type_name = note.note_type()["name"]

        # Find matching action
        action_config = None
        for a in config.get(CONFIG_REVIEWER_ACTIONS, []):
            if a.get("id") == action_id:
                if a.get("note_type_name") == note_type_name:
                    action_config = a
                else:
                    _generating = False
                    tooltip(
                        f"Action '{a.get('button_label')}' is for note type "
                        f"'{a.get('note_type_name')}', not '{note_type_name}'.",
                        period=3000,
                    )
                    return

        if not action_config:
            _generating = False
            showWarning(f"Action '{action_id}' not found in configuration.", title="RevAI")
            return

        label = action_config.get("button_label", "")
        tooltip(f"Generating '{label}'...", period=2000)

        # Disable the buttons visually
        try:
            mw.reviewer.web.eval(
                "document.querySelectorAll('.reviewai-btn').forEach(b => b.disabled = true);"
            )
        except Exception:
            pass

        note_id = note.id
        target = action_config.get("target_field_name")

        if target not in note.keys():
            _generating = False
            showWarning(
                f"Field '{target}' not found on note type '{note_type_name}'.\n"
                f"Create it in Anki via Manage Note Types first.",
                title="RevAI",
            )
            return

        # Capture the user's typed answer. Prefer the cached value captured when
        # the answer side was shown; fall back to reading it directly from the reviewer
        # or from the DOM.
        typed_answer = _last_typed_answer
        if not typed_answer:
            try:
                typed_answer = reviewer.typedAnswer or ""
                log_debug(f"handle_ai_action: fallback typedAnswer='{typed_answer}'")
            except Exception as e:
                log_debug(f"handle_ai_action: fallback typedAnswer failed: {e}")
                typed_answer = ""
        if not typed_answer:
            typed_answer = _get_typed_answer_from_dom()
            log_debug(f"handle_ai_action: DOM fallback typedAnswer='{typed_answer}'")
        else:
            log_debug(f"handle_ai_action: using cached typedAnswer='{typed_answer}'")

        # Select prompt template. If the action defines separate correct/incorrect
        # templates, route based on the captured typed answer to eliminate
        # conditional reasoning inside the model.
        try:
            word_value = note["Word"] if "Word" in note.keys() else ""
        except Exception:
            word_value = ""
        typed_lower = typed_answer.strip().lower()
        correct_lower = word_value.strip().lower()
        has_dual_prompts = bool(
            action_config.get("prompt_template_correct")
            or action_config.get("prompt_template_incorrect")
        )
        if has_dual_prompts:
            if typed_lower and typed_lower != correct_lower:
                chosen_template = action_config.get(
                    "prompt_template_incorrect", action_config.get("prompt_template", "")
                )
                log_debug("handle_ai_action: using prompt_template_incorrect")
            else:
                chosen_template = action_config.get(
                    "prompt_template_correct", action_config.get("prompt_template", "")
                )
                log_debug("handle_ai_action: using prompt_template_correct")
        else:
            chosen_template = action_config.get("prompt_template", "")
            log_debug("handle_ai_action: using legacy prompt_template")

        # Build prompt from the currently reviewed note fields.
        note_data = {key: note[key] for key in note.keys()}

        # Inject the captured typed answer into WrongSpelling so dictation
        # prompts can see the user's actual spelling while generating.
        if "WrongSpelling" in note_data:
            typed_lower = typed_answer.strip().lower()
            correct_lower = word_value.strip().lower()
            if typed_answer and typed_lower != correct_lower:
                note_data["WrongSpelling"] = typed_answer
                log_debug(f"handle_ai_action: injected WrongSpelling='{typed_answer}'")
            else:
                note_data["WrongSpelling"] = ""
                log_debug("handle_ai_action: cleared WrongSpelling (correct or empty)")

        prompt = construct_prompt(chosen_template, note_data)

        if not prompt.strip():
            _generating = False
            showWarning(
                "Prompt is empty after field substitution. Check your prompt template.",
                title="RevAI",
            )
            return

        streaming_enabled = (
            config.get(CONFIG_ENABLE_STREAMING, True)
            and QThread is not None
            and GenerationWorker is not None
        )
        log_debug(
            f"handle_ai_action: mode={mode}, model={config.get(CONFIG_DEFAULT_MODEL)}, "
            f"streaming_enabled={streaming_enabled}, QThread={QThread is not None}, "
            f"GenerationWorker={GenerationWorker is not None}"
        )

        if streaming_enabled:
            _start_streaming_worker(
                mode, config, action_config, prompt, note_id, target, label, typed_answer
            )
            return

        # -----------------------------------------------------------------------
        # Non-streaming fallback path
        # -----------------------------------------------------------------------
        def background_op(col):
            n = col.get_note(note_id)
            if not n:
                raise Exception(f"Note {note_id} not found.")

            note_model = n.note_type()
            field_names = col.models.field_names(note_model)
            if target not in field_names:
                raise Exception(
                    f"Field '{target}' not found on note type '{note_model['name']}'.\n"
                    f"Create it in Anki via Manage Note Types first."
                )

            # Auto-fill WrongSpelling from the typed answer for dictation cards.
            if "WrongSpelling" in field_names:
                try:
                    correct = n["Word"].strip().lower()
                except Exception:
                    correct = ""
                typed = typed_answer.strip().lower()
                if typed and typed != correct:
                    n["WrongSpelling"] = typed_answer
                else:
                    n["WrongSpelling"] = ""

            # Call AI based on mode
            if mode == "byok":
                client = OpenRouterClient(config.get(CONFIG_API_KEY))
                raw_response = client.generate(config.get(CONFIG_DEFAULT_MODEL), prompt)
            elif mode == "direct":
                base_url = config.get(CONFIG_DIRECT_API_URL, "http://127.0.0.1:8081/v1")
                api_key = config.get(CONFIG_DIRECT_API_KEY, "")
                no_proxy = config.get(CONFIG_DIRECT_API_NO_PROXY, True)
                client = OpenRouterClient(api_key, base_url=base_url, no_proxy=no_proxy)
                model = config.get(CONFIG_DEFAULT_MODEL, "")
                # Direct endpoints (DeepSeek, local proxies, etc.) often use bare model
                # names like "deepseek-v4-flash" rather than "provider/model".
                if "/" in model:
                    model = model.split("/", 1)[1]
                raw_response = client.generate(model, prompt)
            else:
                auth = config.get("auth", {})
                client = BackendClient(auth.get("access_token"), auth.get("refresh_token"))
                model = config.get(CONFIG_DEFAULT_MODEL)
                raw_response, meta = client.generate(prompt, model=model if model else None)

                # Save refreshed tokens if they changed
                if client.access_token != auth.get("access_token"):
                    config["auth"]["access_token"] = client.access_token
                    config["auth"]["refresh_token"] = client.refresh_token
                    mw.addonManager.writeConfig(ADDON_PACKAGE, config)

            # Convert markdown to HTML and store
            html_content = markdown_to_html(raw_response)
            n[target] = html_content
            return col.update_note(n)

        def on_success(op_changes):
            _re_enable_buttons()
            tooltip(f"'{target}' updated.", period=3000)
            try:
                if mw.reviewer:
                    mw.reviewer.refresh_if_needed()
            except Exception:
                pass

        def on_failure(exc):
            _re_enable_buttons()

            # Unwrap Anki's exception wrapper if needed
            actual_exc = exc
            if hasattr(exc, '__cause__') and exc.__cause__:
                actual_exc = exc.__cause__

            if isinstance(actual_exc, CreditsExhaustedError):
                showWarning(
                    "You've used all your free credits!\n\n"
                    "Options:\n"
                    "- Redeem a coupon code in RevAI Config\n"
                    "- Switch to 'Own API Key' mode with an OpenRouter key\n"
                    "- Wait for monthly credit reset (1st of each month)",
                    title="RevAI - No Credits",
                )
            elif isinstance(actual_exc, AuthError):
                showWarning(
                    f"Authentication error: {actual_exc}\n\n"
                    "Please sign in again via Tools > RevAI Config.",
                    title="RevAI",
                )
            elif isinstance(actual_exc, NetworkError):
                showWarning(
                    f"Connection problem:\n{actual_exc}\n\n"
                    "Check your internet connection and try again.",
                    title="RevAI - Network Error",
                )
            else:
                msg = str(actual_exc)
                # Truncate very long error messages
                if len(msg) > 500:
                    msg = msg[:500] + "..."
                showWarning(f"AI generation failed:\n{msg}", title="RevAI")
                print(f"RevAI: Generation error: {traceback.format_exc()}")

        op = CollectionOp(parent=mw, op=background_op)
        op.success(on_success)
        op.failure(on_failure)
        op.run_in_background()



    try:
        return _run()
    except Exception as _e:
        log_debug(f"handle_ai_action: unhandled exception: {traceback.format_exc()}")
        try:
            showWarning("RevAI encountered an error:\n" + str(_e)[:500], title="RevAI")
        except Exception:
            pass
    finally:
        _reset_state()


def _handle_clear_field(field_name):
    """Clear the specified field on the current note."""
    reviewer = mw.reviewer
    if not reviewer or not reviewer.card:
        return

    note = reviewer.card.note()
    if field_name not in note.keys():
        return

    note_id = note.id

    def background_op(col):
        n = col.get_note(note_id)
        if not n:
            raise Exception(f"Note {note_id} not found.")
        n[field_name] = ""
        return col.update_note(n)

    def on_success(op_changes):
        tooltip(f"'{field_name}' cleared.", period=2000)
        if mw.reviewer:
            mw.reviewer.refresh_if_needed()

    def on_failure(exc):
        showWarning(f"Failed to clear field:\n{exc}", title="RevAI")

    op = CollectionOp(parent=mw, op=background_op)
    op.success(on_success)
    op.failure(on_failure)
    op.run_in_background()


gui_hooks.webview_did_receive_js_message.append(on_webview_message)


# ---------------------------------------------------------------------------
# Config menu entry
# ---------------------------------------------------------------------------
def show_config_dialog():
    try:
        ConfigDialog(mw).exec()
    except Exception as e:
        print(f"RevAI: Config dialog error: {traceback.format_exc()}")
        showWarning(f"Failed to open RevAI Config:\n{e}", title="RevAI")


action = mw.form.menuTools.addAction("RevAI Config...")
action.triggered.connect(show_config_dialog)
mw.addonManager.setConfigAction(ADDON_PACKAGE, show_config_dialog)


# ---------------------------------------------------------------------------
# Default config on profile load
# ---------------------------------------------------------------------------
def on_profile_loaded():
    log_debug(f"RevAI plugin loaded at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        current = mw.addonManager.getConfig(ADDON_PACKAGE)
        if current is None:
            try:
                config_path = os.path.join(os.path.dirname(__file__), "config.json")
                with open(config_path, "r", encoding="utf-8") as f:
                    defaults = json.load(f)
                mw.addonManager.writeConfig(ADDON_PACKAGE, defaults)
            except Exception:
                mw.addonManager.writeConfig(ADDON_PACKAGE, {
                    CONFIG_MODE: "backend",
                    CONFIG_API_KEY: "",
                    CONFIG_DIRECT_API_URL: "http://127.0.0.1:8081/v1",
                    CONFIG_DIRECT_API_KEY: "",
                    CONFIG_DIRECT_API_NO_PROXY: True,
                    CONFIG_DEFAULT_MODEL: "",
                    CONFIG_ENABLE_STREAMING: True,
                    CONFIG_REVIEWER_ACTIONS: [],
                    "auth": {"access_token": "", "refresh_token": "", "email": ""},
                })
    except Exception:
        print(f"RevAI: Profile load error: {traceback.format_exc()}")


gui_hooks.profile_did_open.append(on_profile_loaded)
