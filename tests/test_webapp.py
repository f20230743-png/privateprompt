import re
import unittest
from pathlib import Path

import webapp
from privacy import PrivacyLayer

PREVIEW_RE = re.compile(r'<pre id="sanitized-output"[^>]*>(.*?)</pre>', re.S)
STAT_RE = re.compile(
    r'data-kind="(?P<kind>[A-Z_]+)">\s*'
    r'<span class="stat-value">(?P<value>\d+)</span>'
)

PROMPT = "Email ada@example.com, call 415-555-2671, key sk-test-0123456789abcdef."
PROJECT_ROOT = Path(webapp.__file__).resolve().parent


def preview_of(body: str):
    match = PREVIEW_RE.search(body)
    return None if match is None else match.group(1)


def counts_of(body: str):
    return {m.group("kind"): int(m.group("value")) for m in STAT_RE.finditer(body)}


class WebAppTestCase(unittest.TestCase):
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

    def csrf_token(self, client=None) -> str:
        client = client or self.client
        body = client.get("/").get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', body)
        return match.group(1)

    def protect(self, prompt: str, client=None, token=None):
        client = client or self.client
        data = {"prompt": prompt, "csrf_token": token or self.csrf_token(client)}
        return client.post("/protect", data=data)

    def clear(self, client=None):
        client = client or self.client
        return client.post("/clear", data={"csrf_token": self.csrf_token(client)})

    def session_state(self, client=None):
        client = client or self.client
        with client.session_transaction() as flask_session:
            sid = flask_session.get("sid")
        return sid, (webapp._SESSIONS[sid] if sid in webapp._SESSIONS else None)


class PageTest(WebAppTestCase):
    def test_index_renders_form_and_privacy_status(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="prompt"', body)
        self.assertIn("Protect my data", body)
        self.assertIn("Demo Mode (no API key)", body)
        self.assertIn("sanitized prompts only", body)
        self.assertIn("127.0.0.1", body)
        self.assertIsNone(preview_of(body))

    def test_security_headers_are_set(self):
        response = self.client.get("/")

        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_templates_and_static_files_bypass_stale_caches(self):
        self.assertTrue(self.app.config["TEMPLATES_AUTO_RELOAD"])
        self.assertEqual(self.app.config["SEND_FILE_MAX_AGE_DEFAULT"], 0)

    def test_no_client_side_storage_libraries_or_network_calls(self):
        sources = [
            (PROJECT_ROOT / "static" / "app.js").read_text(encoding="utf-8"),
            (PROJECT_ROOT / "templates" / "index.html").read_text(encoding="utf-8"),
        ]
        for source in sources:
            for forbidden in (
                "localStorage",
                "sessionStorage",
                "indexedDB",
                "fetch(",
                "XMLHttpRequest",
                "sendBeacon",
            ):
                self.assertNotIn(forbidden, source)


class ProtectTest(WebAppTestCase):
    def test_protect_replaces_every_entity_type(self):
        response = self.protect(PROMPT)
        body = response.get_data(as_text=True)
        preview = preview_of(body)

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(preview)
        self.assertIn("[EMAIL_1]", preview)
        self.assertIn("[PHONE_1]", preview)
        self.assertIn("[API_KEY_1]", preview)
        for original in ("ada@example.com", "415-555-2671", "sk-test-0123456789abcdef"):
            self.assertNotIn(original, preview)

    def test_counts_are_reported_per_type(self):
        prompt = (
            "a@one.com, b@two.org, 415-555-2671, 212-555-0100, "
            "sk-test-0123456789abcdef, AKIAFAKEFAKEFAKE1234"
        )
        body = self.protect(prompt).get_data(as_text=True)

        self.assertEqual(counts_of(body), {"EMAIL": 2, "PHONE": 2, "API_KEY": 2})

    def test_empty_prompt_is_rejected_without_creating_session(self):
        response = self.protect("   ")
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Enter a prompt first", body)
        self.assertEqual(len(webapp._SESSIONS), 0)

    def test_oversized_prompt_is_rejected(self):
        response = self.protect("a" * (webapp.MAX_PROMPT_CHARS + 1))
        body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 400)
        self.assertIn("too long", body)

    def test_user_text_is_html_escaped(self):
        payload = '<script>alert("x")</script>'
        body = self.protect(f"{payload} ada@example.com").get_data(as_text=True)

        self.assertNotIn(payload, body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("[EMAIL_1]", preview_of(body))

    def test_same_value_keeps_one_placeholder_across_requests(self):
        first = preview_of(self.protect("ada@example.com").get_data(as_text=True))
        second = preview_of(self.protect("ada@example.com").get_data(as_text=True))

        self.assertIn("[EMAIL_1]", first)
        self.assertIn("[EMAIL_1]", second)
        _sid, layer = self.session_state()
        self.assertEqual(len(layer), 1)


class ClearTest(WebAppTestCase):
    def test_clear_forgets_mapping_and_clears_ui(self):
        self.protect("ada@example.com")
        sid, layer = self.session_state()
        self.assertIsNotNone(layer)
        self.assertEqual(len(layer), 1)

        response = self.clear()
        self.assertEqual(response.status_code, 303)
        self.assertNotIn(sid, webapp._SESSIONS)
        self.assertEqual(len(layer), 0)
        self.assertEqual(dict(layer.mapping), {})
        self.assertEqual(layer.restore("[EMAIL_1]"), "[EMAIL_1]")

        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('name="prompt"', page)
        self.assertNotIn("Sanitized prompt", page)
        self.assertNotIn("ada@example.com", page)

    def test_clear_without_session_is_safe(self):
        response = self.clear()

        self.assertEqual(response.status_code, 303)


class SessionIsolationTest(WebAppTestCase):
    def test_clients_get_separate_in_memory_layers(self):
        other = self.app.test_client()
        self.protect("ada@example.com")
        other_body = self.protect("grace@example.org", client=other).get_data(as_text=True)

        self.assertIn("[EMAIL_1]", preview_of(other_body))
        self.assertNotIn("ada@example.com", other_body)

        sid_a, layer_a = self.session_state()
        sid_b, layer_b = self.session_state(other)
        self.assertNotEqual(sid_a, sid_b)
        self.assertEqual(dict(layer_a.mapping), {"[EMAIL_1]": "ada@example.com"})
        self.assertEqual(dict(layer_b.mapping), {"[EMAIL_1]": "grace@example.org"})


class CountsSupportTest(unittest.TestCase):
    def test_sanitize_with_counts_matches_sanitize(self):
        layer = PrivacyLayer()
        text = "ada@example.com and ada@example.com, 415-555-2671"

        sanitized, counts = layer.sanitize_with_counts(text)

        self.assertEqual(sanitized, layer.sanitize(text))
        self.assertEqual(counts, {"EMAIL": 2, "PHONE": 1, "API_KEY": 0})


if __name__ == "__main__":
    unittest.main()
