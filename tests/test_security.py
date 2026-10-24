import ast
import logging
import re
import unittest
from pathlib import Path

import webapp

ROOT = Path(webapp.__file__).resolve().parent
EMAIL = "ada@example.com"
PHONE = "415-555-2671"
KEY = "sk-test-0123456789abcdef"
PAYLOAD = f"Email {EMAIL}, call {PHONE}, key {KEY}."

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
FORBIDDEN_IMPORTS = {
    "requests",
    "urllib",
    "urllib3",
    "http",
    "socket",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
}


def csrf_token(client) -> str:
    body = client.get("/").get_data(as_text=True)
    return CSRF_RE.search(body).group(1)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class SecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = webapp.create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        with webapp._LOCK:
            webapp._SESSIONS.clear()

    def tearDown(self):
        with webapp._LOCK:
            for layer in webapp._SESSIONS.values():
                layer.forget()
            webapp._SESSIONS.clear()

    def protect(self, text=PAYLOAD, client=None, token=None):
        client = client or self.client
        return client.post(
            "/protect",
            data={"prompt": text, "csrf_token": token or csrf_token(client)},
        )

    def session_state(self, client=None):
        client = client or self.client
        with client.session_transaction() as flask_session:
            sid = flask_session.get("sid")
        return sid, (webapp._SESSIONS[sid] if sid and sid in webapp._SESSIONS else None)


class CsrfTest(SecurityTestCase):
    def test_protect_without_token_is_rejected(self):
        response = self.client.post("/protect", data={"prompt": PAYLOAD})

        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.get_data(as_text=True).lower())
        self.assertEqual(len(webapp._SESSIONS), 0)

    def test_protect_with_foreign_token_is_rejected(self):
        foreign = csrf_token(self.app.test_client())
        response = self.client.post(
            "/protect", data={"prompt": PAYLOAD, "csrf_token": foreign}
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("[EMAIL_1]", response.get_data(as_text=True))
        self.assertEqual(len(webapp._SESSIONS), 0)

    def test_clear_requires_token_and_keeps_mapping(self):
        self.protect()
        sid, layer = self.session_state()
        self.assertEqual(len(layer), 3)

        response = self.client.post("/clear")

        self.assertEqual(response.status_code, 400)
        self.assertIn(sid, webapp._SESSIONS)
        self.assertEqual(len(layer), 3)


class SessionIsolationTest(SecurityTestCase):
    def test_tampered_cookie_does_not_reveal_another_session(self):
        self.protect()
        _sid, layer = self.session_state()
        cookie = self.client.get_cookie("privateprompt_session")
        self.assertIsNotNone(cookie)

        attacker = self.app.test_client()
        attacker.set_cookie("privateprompt_session", cookie.value[:-3] + "xyz")
        body = attacker.get("/").get_data(as_text=True)

        self.assertNotIn(EMAIL, body)
        attacker_sid, attacker_layer = self.session_state(attacker)
        self.assertIsNone(attacker_sid)
        self.assertIsNone(attacker_layer)
        self.assertEqual(len(layer), 3)

    def test_no_route_accepts_a_session_identifier(self):
        rules = {rule.rule for rule in self.app.url_map.iter_rules()}

        self.assertEqual(
            rules, {"/", "/protect", "/ask", "/demo", "/clear", "/static/<path:filename>"}
        )
        for rule in rules:
            self.assertNotIn("sid", rule.lower())


class LoggingTest(SecurityTestCase):
    def test_sensitive_values_never_reach_logs(self):
        capture = _Capture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            self.protect()
            self.client.post("/clear", data={"csrf_token": csrf_token(self.client)})
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)

        joined = "\n".join(capture.messages)
        for secret in (EMAIL, PHONE, KEY, "EMAIL_1"):
            self.assertNotIn(secret, joined)


class NetworkBoundaryTest(unittest.TestCase):
    def test_python_sources_import_no_network_modules(self):
        for filename in ("webapp.py", "privacy.py", "ai_service.py", "demo_service.py"):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.extend(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module.split(".")[0])
            for module in modules:
                self.assertNotIn(module, FORBIDDEN_IMPORTS, f"{filename}: {module}")

    def test_ui_references_no_external_urls(self):
        for relative in ("templates/index.html", "static/app.js", "static/style.css"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"https?://", text), relative)

    def test_server_binds_loopback_only(self):
        self.assertEqual(webapp.HOST, "127.0.0.1")
        self.assertNotEqual(webapp.HOST, "0.0.0.0")


class ErrorHandlingTest(SecurityTestCase):
    def test_404_and_405_return_safe_pages(self):
        for response in (self.client.get("/missing"), self.client.get("/protect")):
            with self.subTest(status=response.status_code):
                body = response.get_data(as_text=True)
                self.assertIn(response.status_code, (404, 405))
                self.assertIn("PrivatePrompt", body)
                self.assertNotIn("Traceback", body)

    def test_500_hides_details_and_logs_only_the_exception_type(self):
        app = webapp.create_app(
            {"TESTING": False, "PROPAGATE_EXCEPTIONS": False, "SECRET_KEY": "test"}
        )

        @app.get("/boom")
        def boom():
            raise RuntimeError(f"failure for {EMAIL}")

        capture = _Capture()
        logger = app.logger
        previous_handlers = logger.handlers[:]
        previous_level = logger.level
        previous_propagate = logger.propagate
        logger.handlers = [capture]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        try:
            response = app.test_client().get("/boom")
        finally:
            logger.handlers = previous_handlers
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn(EMAIL, body)
        self.assertNotIn("Traceback", body)
        joined = "\n".join(capture.messages)
        self.assertNotIn(EMAIL, joined)
        self.assertIn("Unhandled error", joined)


class InputValidationTest(SecurityTestCase):
    def test_prompt_at_size_limit_is_accepted(self):
        response = self.protect("a" * webapp.MAX_PROMPT_CHARS)

        self.assertEqual(response.status_code, 200)

    def test_non_form_body_is_rejected_without_echoing_it(self):
        response = self.client.post("/protect", json={"prompt": PAYLOAD})
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 400)
        self.assertNotIn(EMAIL, body)
        self.assertEqual(len(webapp._SESSIONS), 0)


class SecretManagementTest(unittest.TestCase):
    def test_secret_key_is_random_per_app_and_cookie_is_hardened(self):
        first = webapp.create_app({"TESTING": True})
        second = webapp.create_app({"TESTING": True})

        self.assertNotEqual(first.config["SECRET_KEY"], second.config["SECRET_KEY"])
        self.assertGreaterEqual(len(first.config["SECRET_KEY"]), 32)
        self.assertTrue(first.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(first.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertFalse(first.config["SESSION_COOKIE_SECURE"])


if __name__ == "__main__":
    unittest.main()
