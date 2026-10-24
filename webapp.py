"""Local Flask UI for the PrivatePrompt engine.

Everything stays on this machine: the server binds to 127.0.0.1, makes no
outbound calls, and keeps mappings in process memory keyed by an opaque
session id. No prompt, mapping, or detected value is ever logged.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from collections import OrderedDict
from typing import Dict, Optional

from flask import Flask, redirect, render_template, request, session, url_for

import ai_service
import demo_service
from privacy import PrivacyLayer

MAX_PROMPT_CHARS = 20_000
MAX_SESSIONS = 128
HOST = "127.0.0.1"
DEFAULT_PORT = 5001


def _asset_version() -> str:
    """Fingerprint the static assets so HTML cannot pair with old CSS/JS."""
    latest = 0.0
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    for name in ("style.css", "app.js"):
        try:
            latest = max(latest, os.path.getmtime(os.path.join(static_dir, name)))
        except OSError:
            continue
    return str(int(latest)) if latest else "1"


ASSET_VERSION = _asset_version()

# Opaque session id (in a signed cookie) -> in-memory privacy layer.
# Nothing here is written to disk.
_SESSIONS: "OrderedDict[str, PrivacyLayer]" = OrderedDict()
_LOCK = threading.Lock()


class PrivacyFlask(Flask):
    """Flask that logs only the exception type, never messages or tracebacks.

    Flask's default handler logs a full traceback, which can contain request
    values if application code embeds them in an exception message.
    """

    def log_exception(self, exc_info) -> None:
        exc_type = exc_info[0] if exc_info else None
        self.logger.error(
            "Unhandled error: %s", getattr(exc_type, "__name__", "Unknown")
        )


def create_app(test_config: Optional[Dict[str, object]] = None) -> Flask:
    app = PrivacyFlask(__name__)
    app.config.update(
        # Regenerated per process: restarting invalidates every session.
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_NAME="privateprompt_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Local http://127.0.0.1 only, so Secure would block the cookie.
        SESSION_COOKIE_SECURE=False,
        MAX_CONTENT_LENGTH=256 * 1024,
        # The app is a local prototype and is often edited while it runs.
        # Reload templates from disk instead of caching them until restart.
        TEMPLATES_AUTO_RELOAD=True,
        SEND_FILE_MAX_AGE_DEFAULT=0,
    )
    if test_config:
        app.config.update(test_config)

    # Requests are logged by Werkzeug as method/path/status only; never bodies.
    app.logger.setLevel(logging.WARNING)

    @app.after_request
    def add_security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    @app.get("/")
    def index():
        return _render()

    @app.post("/protect")
    def protect():
        prompt = request.form.get("prompt", "")
        if not prompt.strip():
            return _render(
                prompt=prompt,
                message="Enter a prompt first, then choose “Protect my data”.",
                status=400,
            )
        if len(prompt) > MAX_PROMPT_CHARS:
            return _render(
                message=f"Prompt is too long (limit {MAX_PROMPT_CHARS:,} characters).",
                status=400,
            )
        if not _csrf_is_valid():
            return _render(
                message="This form expired. Reload the page and try again.",
                status=400,
            )

        with _LOCK:
            layer = _layer_for_session(create=True)
            sanitized, counts = layer.sanitize_with_counts(prompt)

        return _render(prompt=prompt, sanitized=sanitized, counts=counts)

    @app.post("/ask")
    def ask():
        prompt = request.form.get("prompt", "")
        if not prompt.strip():
            return _render(
                prompt=prompt,
                message="Enter a prompt first, then choose “Ask AI”.",
                status=400,
            )
        if len(prompt) > MAX_PROMPT_CHARS:
            return _render(
                message=f"Prompt is too long (limit {MAX_PROMPT_CHARS:,} characters).",
                status=400,
            )
        if not _csrf_is_valid():
            return _render(
                message="This form expired. Reload the page and try again.",
                status=400,
            )

        # Sanitize under the lock, then release it for the network call so one
        # slow request cannot block every browser session.
        with _LOCK:
            layer = _layer_for_session(create=True)
            sid = session.get("sid")
            sanitized, counts = layer.sanitize_with_counts(prompt)

        try:
            reply = ai_service.generate_reply(sanitized)
        except ai_service.AINotConfigured:
            return _render(
                prompt=prompt,
                sanitized=sanitized,
                counts=counts,
                message=(
                    "AI is not configured on this server. Set OPENAI_API_KEY "
                    "and restart the app."
                ),
                status=503,
            )
        except ai_service.AIRateLimited:
            return _render(
                prompt=prompt,
                sanitized=sanitized,
                counts=counts,
                message="The AI provider is rate limiting requests. Try again shortly.",
                status=429,
            )
        except ai_service.AITimeout:
            return _render(
                prompt=prompt,
                sanitized=sanitized,
                counts=counts,
                message="The AI request timed out. Only the sanitized prompt was sent.",
                status=504,
            )
        except ai_service.AIError:
            return _render(
                prompt=prompt,
                sanitized=sanitized,
                counts=counts,
                message=(
                    "The AI request could not be completed. Only the sanitized "
                    "prompt was sent."
                ),
                status=502,
            )

        # Restore only if this is still the same live session; a concurrent
        # Clear data must not resurrect mappings.
        with _LOCK:
            current = _SESSIONS.get(sid) if sid else None
            answer = current.restore(reply) if current is layer else reply

        return _render(
            prompt=prompt,
            sanitized=sanitized,
            counts=counts,
            ai_answer=answer,
        )

    @app.get("/demo")
    def demo_page():
        # A bookmark, link, or refresh must never land on a method-error page.
        return redirect(url_for("index"), code=303)

    @app.post("/demo")
    def demo():
        prompt = request.form.get("prompt", "")
        if not prompt.strip():
            return _render(
                prompt=prompt,
                message="Enter a prompt first, then choose “Demo Mode”.",
                status=400,
            )
        if len(prompt) > MAX_PROMPT_CHARS:
            return _render(
                message=f"Prompt is too long (limit {MAX_PROMPT_CHARS:,} characters).",
                status=400,
            )
        if not _csrf_is_valid():
            return _render(
                message="This form expired. Reload the page and try again.",
                status=400,
            )

        with _LOCK:
            layer = _layer_for_session(create=True)
            sid = session.get("sid")
            sanitized, counts = layer.sanitize_with_counts(prompt)

        # Local template only: no SDK, no key, no network.
        reply = demo_service.generate_demo_reply(sanitized)

        with _LOCK:
            current = _SESSIONS.get(sid) if sid else None
            answer = current.restore(reply) if current is layer else reply

        return _render(
            prompt=prompt,
            sanitized=sanitized,
            counts=counts,
            ai_answer=answer,
            simulated=True,
        )

    @app.post("/clear")
    def clear():
        if not _csrf_is_valid():
            return _render(
                message="This form expired. Reload the page and try again.",
                status=400,
            )
        with _LOCK:
            layer = _layer_for_session(create=False)
            if layer is not None:
                layer.forget()
            sid = session.get("sid")
            if sid is not None:
                _SESSIONS.pop(sid, None)
            session.clear()
        return redirect(url_for("index"), code=303)

    @app.errorhandler(404)
    def not_found(_error):
        return _render(message="Page not found.", status=404)

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return _render(message="That action is not available here.", status=405)

    @app.errorhandler(413)
    def request_too_large(_error):
        return _render(
            message="That request is too large. Try a shorter prompt.",
            status=413,
        )

    @app.errorhandler(500)
    def internal_error(_error):
        # PrivacyFlask.log_exception already logged the type only.
        return _render(
            message="Something went wrong. Nothing was stored.",
            status=500,
        )

    return app


def _render(
    prompt: str = "",
    sanitized: Optional[str] = None,
    counts: Optional[Dict[str, int]] = None,
    message: Optional[str] = None,
    ai_answer: Optional[str] = None,
    simulated: bool = False,
    status: int = 200,
):
    return (
        render_template(
            "index.html",
            prompt=prompt,
            sanitized=sanitized,
            counts=counts,
            message=message,
            ai_answer=ai_answer,
            simulated=simulated,
            max_chars=MAX_PROMPT_CHARS,
            csrf_token=_csrf_token(),
            asset_version=ASSET_VERSION,
        ),
        status,
    )


def _csrf_token() -> str:
    """Per-browser CSRF token; lives in the signed session cookie."""
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_is_valid() -> bool:
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token", "")
    if not isinstance(expected, str) or not expected:
        return False
    return secrets.compare_digest(expected, provided)


def _layer_for_session(create: bool) -> Optional[PrivacyLayer]:
    """Return this browser's layer; caller must hold ``_LOCK``."""
    sid = session.get("sid")
    if sid and sid in _SESSIONS:
        _SESSIONS.move_to_end(sid)
        return _SESSIONS[sid]
    if not create:
        return None

    sid = secrets.token_urlsafe(24)
    session["sid"] = sid
    layer = PrivacyLayer()
    _SESSIONS[sid] = layer

    while len(_SESSIONS) > MAX_SESSIONS:
        _old_sid, evicted = _SESSIONS.popitem(last=False)
        evicted.forget()
    return layer


if __name__ == "__main__":
    port = int(os.environ.get("PRIVATEPROMPT_PORT", str(DEFAULT_PORT)))
    create_app().run(
        host=HOST,
        port=port,
        debug=False,
        use_reloader=False,
    )
