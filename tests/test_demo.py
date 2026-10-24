import os
import re
import unittest
from unittest import mock

import demo_service
import webapp

EMAIL = "ada@example.com"
PHONE = "415-555-2671"
KEY = "sk-test-0123456789abcdef"
PROMPT = f"Email {EMAIL}, call {PHONE}, key {KEY}."
SIMULATED_LABEL = "Simulated response — no AI model was called."

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ANSWER_RE = re.compile(r'<pre id="ai-output"[^>]*>(.*?)</pre>', re.S)


def answer_of(body: str):
    match = ANSWER_RE.search(body)
    return None if match is None else match.group(1)


class DemoTestCase(unittest.TestCase):
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

    def demo(self, prompt=PROMPT, client=None, token=None):
        client = client or self.client
        return client.post(
            "/demo",
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


class DemoTemplateTest(unittest.TestCase):
    def test_email_request_has_full_professional_structure(self):
        text = demo_service.generate_demo_reply(
            "Write a professional email to [EMAIL_1] about the Q4 launch "
            "timeline."
        )

        self.assertIn("Subject: Q4 launch timeline", text)
        self.assertIn("Hi [Recipient Name],", text)
        self.assertIn("I'm writing about the Q4 launch timeline.", text)
        self.assertIn("Please let me know if you have any questions", text)
        self.assertIn("Best regards,", text)
        self.assertTrue(text.endswith("[Your Name]"))
        self.assertNotIn("[EMAIL_1]", text)
        self.assertNotIn("ada@example.com", text)

    def test_summary_uses_only_sanitized_input(self):
        text = demo_service.generate_demo_reply(
            "Summarize this email from [EMAIL_1]: the launch moved to Friday "
            "and the team approved the budget."
        )

        self.assertTrue(text.startswith("Summary (demo)"))
        self.assertIn("the launch moved to Friday", text)
        self.assertIn("the team approved the budget", text)
        self.assertIn("no new facts were added", text)
        self.assertIn("Redacted references: 1 email address", text)
        self.assertNotIn("[EMAIL_1]", text)
        self.assertNotIn("ada@example.com", text)

    def test_interview_request_returns_questions_and_tips(self):
        text = demo_service.generate_demo_reply(
            "Help me prepare for a behavioral interview for a product role."
        )

        self.assertTrue(text.startswith("Interview preparation (demo)"))
        self.assertIn("Sample questions", text)
        self.assertIn("Preparation tips", text)
        self.assertIn("STAR method", text)
        self.assertIn("1. Tell me about yourself", text)

    def test_unsupported_request_returns_generic_demo_menu(self):
        text = demo_service.generate_demo_reply("Help me plan a launch.")

        self.assertTrue(text.startswith("Demo assistant (offline)"))
        self.assertIn("supports three request types", text)
        self.assertIn("Email writing", text)
        self.assertIn("Summarization", text)
        self.assertIn("Interview preparation", text)
        self.assertNotIn("Help me plan a launch.", text)

    def test_same_input_is_deterministic(self):
        prompt = "Write an email to [EMAIL_1] about [PHONE_1]."

        first = demo_service.generate_demo_reply(prompt)
        second = demo_service.generate_demo_reply(prompt)

        self.assertEqual(first, second)
        self.assertLess(len(first), 2000)


class DemoRouteTest(DemoTestCase):
    def test_runs_without_api_key_and_calls_no_provider(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "ai_service.generate_reply"
        ) as provider, mock.patch("ai_service.OpenAI") as client_cls:
            response = self.demo()

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(SIMULATED_LABEL, body)
        self.assertIsNotNone(answer_of(body))
        provider.assert_not_called()
        client_cls.assert_not_called()

    @mock.patch("demo_service.generate_demo_reply", return_value="ok")
    def test_engine_receives_sanitized_text_only(self, fake):
        self.demo()

        args, kwargs = fake.call_args
        self.assertEqual(kwargs, {})
        self.assertEqual(len(args), 1)
        sent = args[0]
        self.assertIn("[EMAIL_1]", sent)
        self.assertIn("[PHONE_1]", sent)
        self.assertIn("[API_KEY_1]", sent)
        for secret in (EMAIL, PHONE, KEY):
            self.assertNotIn(secret, sent)

    def test_original_email_is_not_echoed_or_sent_externally(self):
        synthetic = "regression-demo@example.test"
        prompt = f"Write an email to {synthetic} about the launch."
        seen = {}
        real_generator = demo_service.generate_demo_reply

        def spy(sanitized_prompt):
            seen["argument"] = sanitized_prompt
            seen["response"] = real_generator(sanitized_prompt)
            return seen["response"]

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), mock.patch(
            "demo_service.generate_demo_reply", side_effect=spy
        ), mock.patch("ai_service.generate_reply") as provider, mock.patch(
            "ai_service.OpenAI"
        ) as client_cls:
            body = self.demo(prompt).get_data(as_text=True)

        self.assertIn("[EMAIL_1]", seen["argument"])
        self.assertNotIn(synthetic, seen["argument"])
        self.assertNotIn(synthetic, seen["response"])
        self.assertNotIn("[EMAIL_1]", seen["response"])
        self.assertNotIn(synthetic, answer_of(body))
        self.assertNotIn("[EMAIL_1]", answer_of(body))
        self.assertIn(SIMULATED_LABEL, body)
        provider.assert_not_called()
        client_cls.assert_not_called()

    def test_sensitive_values_absent_for_each_response_type(self):
        synthetic = "per-type-regression@example.test"
        prompts = {
            "email": f"Write an email to {synthetic} about the launch.",
            "summary": (
                f"Summarize: the launch moved to Friday; contact {synthetic}."
            ),
            "interview": (
                f"Help me prepare for a behavioral interview for a product "
                f"role; my address is {synthetic}."
            ),
            "generic": f"Plan a picnic with {synthetic}.",
        }

        for label, prompt in prompts.items():
            with self.subTest(kind=label):
                with mock.patch(
                    "demo_service.generate_demo_reply",
                    wraps=demo_service.generate_demo_reply,
                ) as fake:
                    body = self.demo(prompt).get_data(as_text=True)

                sent = fake.call_args.args[0]
                answer = answer_of(body)
                self.assertIn("[EMAIL_1]", sent)
                self.assertNotIn(synthetic, sent)
                self.assertNotIn(synthetic, answer)
                self.assertNotIn("[EMAIL_1]", answer)
                self.assertIn(SIMULATED_LABEL, body)

    @mock.patch(
        "demo_service.generate_demo_reply",
        return_value="Reference [EMAIL_1], [PHONE_1] and [API_KEY_1].",
    )
    def test_placeholders_are_restored_locally(self, _fake):
        answer = answer_of(self.demo().get_data(as_text=True))

        self.assertIn(EMAIL, answer)
        self.assertIn(PHONE, answer)
        self.assertIn(KEY, answer)
        self.assertNotIn("[EMAIL_1]", answer)

    @mock.patch("demo_service.generate_demo_reply", return_value="[EMAIL_99] stays.")
    def test_unknown_placeholders_are_left_alone(self, _fake):
        answer = answer_of(self.demo().get_data(as_text=True))

        self.assertIn("[EMAIL_99]", answer)

    def test_answers_only_restore_the_current_session(self):
        other = self.app.test_client()

        with mock.patch(
            "demo_service.generate_demo_reply", return_value="Contact [EMAIL_1]."
        ):
            first = answer_of(
                self.demo("Write an email to ada@example.com").get_data(as_text=True)
            )
            second = answer_of(
                self.demo("Write an email to grace@example.org", client=other).get_data(
                    as_text=True
                )
            )

        self.assertIn(EMAIL, first)
        self.assertNotIn("grace@example.org", first)
        self.assertIn("grace@example.org", second)
        self.assertNotIn(EMAIL, second)

    @mock.patch("ai_service.generate_reply", return_value="[EMAIL_1]")
    def test_real_ai_answer_is_not_labeled_simulated(self, _fake):
        body = self.client.post(
            "/ask",
            data={"prompt": PROMPT, "csrf_token": self.csrf()},
        ).get_data(as_text=True)

        self.assertIsNotNone(answer_of(body))
        self.assertNotIn(SIMULATED_LABEL, body)

    def test_demo_requires_csrf(self):
        response = self.client.post("/demo", data={"prompt": PROMPT})

        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.get_data(as_text=True).lower())
        self.assertEqual(len(webapp._SESSIONS), 0)

    @mock.patch("demo_service.generate_demo_reply", return_value="ok")
    def test_invalid_input_never_reaches_demo_service(self, fake):
        cases = [
            ("   ", "Enter a prompt first"),
            ("a" * (webapp.MAX_PROMPT_CHARS + 1), "too long"),
        ]
        for prompt, expected in cases:
            with self.subTest(expected=expected):
                response = self.demo(prompt)
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_data(as_text=True))

        json_response = self.client.post("/demo", json={"prompt": PROMPT})
        self.assertEqual(json_response.status_code, 400)
        fake.assert_not_called()

    def test_clear_removes_demo_answer_and_mapping(self):
        body = self.demo().get_data(as_text=True)
        self.assertIn(SIMULATED_LABEL, body)
        sid, layer = self.session_state()

        self.assertEqual(self.clear().status_code, 303)

        self.assertNotIn(sid, webapp._SESSIONS)
        self.assertEqual(len(layer), 0)
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(SIMULATED_LABEL, page)
        self.assertNotIn("AI response", page)


class DemoPageAssetTest(DemoTestCase):
    def test_demo_returns_full_styled_page_with_result_in_place(self):
        home = self.client.get("/")
        demo = self.demo()
        home_body = home.get_data(as_text=True)
        demo_body = demo.get_data(as_text=True)

        self.assertEqual(demo.status_code, 200)
        self.assertEqual(demo.mimetype, "text/html")
        self.assertTrue(demo_body.lstrip().startswith("<!doctype html>"))
        self.assertIn('<main class="shell">', demo_body)
        self.assertIn('class="card"', demo_body)
        self.assertIn('class="results"', demo_body)
        self.assertIn("Sanitized prompt", demo_body)
        self.assertIn(SIMULATED_LABEL, demo_body)
        self.assertIn("Clear data", demo_body)
        self.assertIsNotNone(answer_of(demo_body))

        head = re.compile(r"<head>.*?</head>", re.S)
        self.assertEqual(head.search(home_body).group(0), head.search(demo_body).group(0))

    def test_demo_assets_load_with_correct_types_and_no_cache(self):
        body = self.demo().get_data(as_text=True)
        urls = re.findall(r'(?:href|src)="(/static/[^"]+)"', body)

        self.assertEqual(len(urls), 2)
        self.assertRegex(body, r"/static/style\.css\?v=\d+")
        self.assertRegex(body, r"/static/app\.js\?v=\d+")

        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    if url.startswith("/static/style.css"):
                        self.assertEqual(response.mimetype, "text/css")
                    elif url.startswith("/static/app.js"):
                        self.assertIn(
                            response.mimetype,
                            {"text/javascript", "application/javascript"},
                        )
                    else:
                        self.fail(f"unexpected asset URL: {url}")
                finally:
                    response.close()

    def test_get_demo_redirects_to_styled_index(self):
        response = self.client.get("/demo")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/")

        followed = self.client.get("/demo", follow_redirects=True)
        body = followed.get_data(as_text=True)
        self.assertEqual(followed.status_code, 200)
        self.assertIn('<main class="shell">', body)
        self.assertIn("/static/style.css?v=", body)
        self.assertNotIn(SIMULATED_LABEL, body)


if __name__ == "__main__":
    unittest.main()
