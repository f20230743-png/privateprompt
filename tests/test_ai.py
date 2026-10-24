import logging
import os
import re
import unittest
from unittest import mock

import httpx
import openai

import ai_service
import webapp

EMAIL = "ada@example.com"
PHONE = "415-555-2671"
KEY = "sk-test-0123456789abcdef"
PROMPT = f"Email {EMAIL}, call {PHONE}, key {KEY}."
FAKE_PROVIDER_KEY = "sk-not-a-real-key-used-only-in-tests"

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ANSWER_RE = re.compile(r'<pre id="ai-output"[^>]*>(.*?)</pre>', re.S)
REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def answer_of(body: str):
    match = ANSWER_RE.search(body)
    return None if match is None else match.group(1)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class AITestCase(unittest.TestCase):
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

    def csrf(self, client=None):
        client = client or self.client
        body = client.get("/").get_data(as_text=True)
        return CSRF_RE.search(body).group(1)

    def ask(self, prompt=PROMPT, client=None, token=None):
        client = client or self.client
        return client.post(
            "/ask",
            data={"prompt": prompt, "csrf_token": token or self.csrf(client)},
        )

    def clear(self, client=None):
        client = client or self.client
        return client.post("/clear", data={"csrf_token": self.csrf(client)})

    def session_state(self, client=None):
        client = client or self.client
        with client.session_transaction() as flask_session:
            sid = flask_session.get("sid")
        return sid, (webapp._SESSIONS[sid] if sid and sid in webapp._SESSIONS else None)


class ProviderArgumentTest(AITestCase):
    @mock.patch("ai_service.generate_reply", return_value="ok")
    def test_provider_receives_placeholders_not_originals(self, fake):
        response = self.ask()

        self.assertEqual(response.status_code, 200)
        fake.assert_called_once()
        args, kwargs = fake.call_args
        self.assertEqual(kwargs, {})
        self.assertEqual(len(args), 1)
        sent = args[0]

        self.assertIn("[EMAIL_1]", sent)
        self.assertIn("[PHONE_1]", sent)
        self.assertIn("[API_KEY_1]", sent)
        for secret in (EMAIL, PHONE, KEY, "example.com"):
            self.assertNotIn(secret, sent)

    @mock.patch("ai_service.generate_reply", return_value="ok")
    def test_mapping_is_not_passed_to_provider(self, fake):
        self.ask()

        args, kwargs = fake.call_args
        self.assertEqual(kwargs, {})
        self.assertEqual(len(args), 1)
        rendered_args = repr(fake.call_args)
        for secret in (EMAIL, PHONE, KEY):
            self.assertNotIn(secret, rendered_args)
        self.assertNotIn("mapping", rendered_args)


class RestorationTest(AITestCase):
    @mock.patch(
        "ai_service.generate_reply",
        return_value="Sure: [EMAIL_1], [PHONE_1], [API_KEY_1].",
    )
    def test_recognized_placeholders_are_restored_locally(self, _fake):
        body = self.ask().get_data(as_text=True)
        answer = answer_of(body)

        self.assertIsNotNone(answer)
        self.assertIn(EMAIL, answer)
        self.assertIn(PHONE, answer)
        self.assertIn(KEY, answer)
        self.assertNotIn("[EMAIL_1]", answer)

    @mock.patch("ai_service.generate_reply", return_value="Unknown [EMAIL_99] stays.")
    def test_unknown_placeholders_are_left_alone(self, _fake):
        answer = answer_of(self.ask().get_data(as_text=True))

        self.assertIn("[EMAIL_99]", answer)
        self.assertIn("Unknown", answer)

    def test_answers_only_restore_the_current_session(self):
        other = self.app.test_client()
        replies = iter(["Contact [EMAIL_1].", "Contact [EMAIL_1]."])

        with mock.patch("ai_service.generate_reply", side_effect=replies):
            first = answer_of(self.ask(PROMPT).get_data(as_text=True))
            second = answer_of(
                self.ask("Mail grace@example.org", client=other).get_data(as_text=True)
            )

        self.assertIn(EMAIL, first)
        self.assertNotIn("grace@example.org", first)
        self.assertIn("grace@example.org", second)
        self.assertNotIn(EMAIL, second)


class InputAndCsrfTest(AITestCase):
    @mock.patch("ai_service.generate_reply", return_value="ok")
    def test_ask_requires_csrf(self, fake):
        response = self.client.post("/ask", data={"prompt": PROMPT})

        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.get_data(as_text=True).lower())
        fake.assert_not_called()
        self.assertEqual(len(webapp._SESSIONS), 0)

    @mock.patch("ai_service.generate_reply", return_value="ok")
    def test_invalid_input_never_reaches_provider(self, fake):
        cases = [
            ("   ", "Enter a prompt first"),
            ("a" * (webapp.MAX_PROMPT_CHARS + 1), "too long"),
        ]
        for prompt, expected in cases:
            with self.subTest(expected=expected):
                response = self.ask(prompt)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_data(as_text=True))

        json_response = self.client.post("/ask", json={"prompt": PROMPT})
        self.assertEqual(json_response.status_code, 400)
        fake.assert_not_called()

    @mock.patch("ai_service.generate_reply", return_value="ok")
    def test_clear_removes_answer_and_mapping(self, _fake):
        body = self.ask().get_data(as_text=True)
        self.assertIsNotNone(answer_of(body))
        sid, layer = self.session_state()

        self.assertEqual(self.clear().status_code, 303)

        self.assertNotIn(sid, webapp._SESSIONS)
        self.assertEqual(len(layer), 0)
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("AI response", page)
        self.assertNotIn("Sanitized prompt", page)


class ProviderFailureTest(AITestCase):
    def test_missing_api_key_is_reported_without_calling_provider(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "ai_service.OpenAI"
        ) as client_cls:
            response = self.ask()

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", body.lower())
        self.assertIsNone(answer_of(body))
        client_cls.assert_not_called()

    def test_provider_failures_return_generic_errors(self):
        cases = (
            (ai_service.AIRateLimited, 429, "rate limiting"),
            (ai_service.AITimeout, 504, "timed out"),
            (ai_service.AIUnavailable, 502, "could not be completed"),
            (ai_service.AIBadResponse, 502, "could not be completed"),
        )
        for error, status, phrase in cases:
            with self.subTest(error=error.__name__):
                with mock.patch("ai_service.generate_reply", side_effect=error):
                    response = self.ask()
                body = response.get_data(as_text=True)
                self.assertEqual(response.status_code, status)
                self.assertIn(phrase, body.lower())
                self.assertIsNone(answer_of(body))
                self.assertNotIn(FAKE_PROVIDER_KEY, body)

    def test_failures_log_no_prompts_values_or_keys(self):
        capture = _Capture()
        root = logging.getLogger()
        previous = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            with mock.patch.dict(
                os.environ, {"OPENAI_API_KEY": FAKE_PROVIDER_KEY}
            ), mock.patch(
                "ai_service.generate_reply", side_effect=ai_service.AIUnavailable
            ):
                body = self.ask().get_data(as_text=True)
        finally:
            root.removeHandler(capture)
            root.setLevel(previous)

        joined = "\n".join(capture.messages)
        for secret in (EMAIL, PHONE, KEY, FAKE_PROVIDER_KEY):
            self.assertNotIn(secret, joined)
        self.assertNotIn(FAKE_PROVIDER_KEY, body)


class ProviderClientTest(unittest.TestCase):
    def test_sdk_receives_only_model_and_sanitized_input(self):
        sent = "Email [EMAIL_1], key [API_KEY_1]."
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": FAKE_PROVIDER_KEY, "OPENAI_MODEL": "test-model"},
            clear=True,
        ), mock.patch("ai_service.OpenAI") as client_cls:
            create = client_cls.return_value.responses.create
            create.return_value.output_text = "  hello  "
            reply = ai_service.generate_reply(sent)

        self.assertEqual(reply, "hello")
        self.assertEqual(
            create.call_args.kwargs,
            {"model": "test-model", "input": sent, "store": False},
        )
        constructor = client_cls.call_args.kwargs
        self.assertEqual(constructor["api_key"], FAKE_PROVIDER_KEY)
        self.assertEqual(constructor["max_retries"], 0)
        self.assertEqual(constructor["timeout"], ai_service.TIMEOUT_SECONDS)

    def test_missing_key_never_constructs_a_client(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "ai_service.OpenAI"
        ) as client_cls:
            with self.assertRaises(ai_service.AINotConfigured):
                ai_service.generate_reply("[EMAIL_1]")

        client_cls.assert_not_called()

    def test_empty_provider_text_is_a_bad_response(self):
        with mock.patch.dict(
            os.environ, {"OPENAI_API_KEY": FAKE_PROVIDER_KEY}, clear=True
        ), mock.patch("ai_service.OpenAI") as client_cls:
            client_cls.return_value.responses.create.return_value.output_text = "   "
            with self.assertRaises(ai_service.AIBadResponse):
                ai_service.generate_reply("[EMAIL_1]")

    def test_sdk_errors_map_to_safe_errors(self):
        cases = (
            (openai.APITimeoutError(request=REQUEST), ai_service.AITimeout),
            (
                openai.AuthenticationError(
                    "bad key", response=httpx.Response(401, request=REQUEST), body=None
                ),
                ai_service.AINotConfigured,
            ),
            (
                openai.RateLimitError(
                    "slow down", response=httpx.Response(429, request=REQUEST), body=None
                ),
                ai_service.AIRateLimited,
            ),
            (openai.APIConnectionError(request=REQUEST), ai_service.AIUnavailable),
            (
                openai.APIStatusError(
                    "server error",
                    response=httpx.Response(500, request=REQUEST),
                    body=None,
                ),
                ai_service.AIUnavailable,
            ),
        )
        for error, expected in cases:
            with self.subTest(expected=expected.__name__):
                with mock.patch.dict(
                    os.environ, {"OPENAI_API_KEY": FAKE_PROVIDER_KEY}, clear=True
                ), mock.patch("ai_service.OpenAI") as client_cls:
                    client_cls.return_value.responses.create.side_effect = error
                    with self.assertRaises(expected):
                        ai_service.generate_reply("[EMAIL_1]")

    def test_model_defaults_and_falls_back_when_invalid(self):
        def model_for(configured):
            env = {"OPENAI_API_KEY": FAKE_PROVIDER_KEY}
            if configured is not None:
                env["OPENAI_MODEL"] = configured
            with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                "ai_service.OpenAI"
            ) as client_cls:
                create = client_cls.return_value.responses.create
                create.return_value.output_text = "ok"
                ai_service.generate_reply("[EMAIL_1]")
                return create.call_args.kwargs["model"]

        self.assertEqual(model_for(None), ai_service.DEFAULT_MODEL)
        self.assertEqual(model_for("custom-model-1"), "custom-model-1")
        self.assertEqual(model_for("bad model name!!"), ai_service.DEFAULT_MODEL)


class ProviderKeyExposureTest(AITestCase):
    def test_api_key_never_appears_in_pages_or_answers(self):
        with mock.patch.dict(
            os.environ,
            {"OPENAI_API_KEY": FAKE_PROVIDER_KEY, "OPENAI_MODEL": "test-model"},
        ), mock.patch("ai_service.OpenAI") as client_cls:
            client_cls.return_value.responses.create.return_value.output_text = (
                "Reply with [EMAIL_1]."
            )
            body = self.ask().get_data(as_text=True)
            index = self.client.get("/").get_data(as_text=True)

        self.assertNotIn(FAKE_PROVIDER_KEY, body)
        self.assertNotIn(FAKE_PROVIDER_KEY, index)
        self.assertIn(EMAIL, answer_of(body))


if __name__ == "__main__":
    unittest.main()
