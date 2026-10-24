import unittest

from privacy import PrivacyLayer

# Synthetic values that match key formats but are not real credentials.
FAKE_OPENAI = "sk-test-0123456789abcdefXYZ"
FAKE_STRIPE = "sk_live_" + "a" * 24
FAKE_AWS = "AKIAFAKEFAKEFAKE1234"
FAKE_GITHUB = "ghp_" + "0" * 36
FAKE_SLACK = "xoxb-fake-token-123456"
FAKE_GOOGLE = "AIza" + "A" * 35

EMAIL = "ada@example.com"
PHONE = "415-555-2671"


class PhoneDetectorTest(unittest.TestCase):
    def test_phone_number_is_replaced(self):
        layer = PrivacyLayer()
        text = "Call +1 (415) 555-2671 now."

        self.assertEqual(layer.sanitize(text), "Call [PHONE_1] now.")

    def test_same_phone_reuses_one_placeholder(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize(f"{PHONE} then {PHONE}")

        self.assertEqual(sanitized, "[PHONE_1] then [PHONE_1]")
        self.assertEqual(len(layer), 1)

    def test_multiple_phone_numbers_get_distinct_placeholders(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize("415-555-2671 and (212) 555-0100")

        self.assertEqual(sanitized, "[PHONE_1] and [PHONE_2]")
        self.assertEqual(len(layer), 2)

    def test_dates_times_and_short_numbers_are_untouched(self):
        layer = PrivacyLayer()
        text = "Order 2026-10-22 at 3pm, ref 12345."

        self.assertEqual(layer.sanitize(text), text)
        self.assertEqual(len(layer), 0)


class ApiKeyDetectorTest(unittest.TestCase):
    def test_common_key_formats_are_replaced(self):
        for value in (
            FAKE_OPENAI,
            FAKE_STRIPE,
            FAKE_AWS,
            FAKE_GITHUB,
            FAKE_SLACK,
            FAKE_GOOGLE,
        ):
            with self.subTest(value=value):
                layer = PrivacyLayer()
                self.assertEqual(layer.sanitize(value), "[API_KEY_1]")

    def test_assignment_style_key_replaces_only_the_value(self):
        cases = (
            ('api_key = "abcd1234efgh5678"', 'api_key = "[API_KEY_1]"'),
            ("access_token: ZZZZYYYYXXXXWWWW1234", "access_token: [API_KEY_1]"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                layer = PrivacyLayer()
                self.assertEqual(layer.sanitize(text), expected)

    def test_multiple_keys_get_distinct_placeholders(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize(f"{FAKE_OPENAI} and {FAKE_AWS}")

        self.assertEqual(sanitized, "[API_KEY_1] and [API_KEY_2]")
        self.assertEqual(len(layer), 2)


class MixedEntityTest(unittest.TestCase):
    def test_each_type_uses_its_own_prefix(self):
        layer = PrivacyLayer()
        text = f"{EMAIL} {PHONE} {FAKE_OPENAI}"

        self.assertEqual(layer.sanitize(text), "[EMAIL_1] [PHONE_1] [API_KEY_1]")

    def test_numbering_is_independent_per_type(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize("a@b.com, 415-555-2671, c@d.org")

        self.assertEqual(sanitized, "[EMAIL_1], [PHONE_1], [EMAIL_2]")

    def test_sanitized_output_contains_no_originals(self):
        layer = PrivacyLayer()
        text = f"Email {EMAIL}, call +1 (415) 555-2671, key {FAKE_OPENAI}."

        sanitized = layer.sanitize(text)

        for original in (EMAIL, "example.com", "415) 555-2671", FAKE_OPENAI):
            self.assertNotIn(original, sanitized)
        for placeholder in ("[EMAIL_1]", "[PHONE_1]", "[API_KEY_1]"):
            self.assertIn(placeholder, sanitized)

    def test_restore_round_trips_all_types(self):
        layer = PrivacyLayer()
        text = (
            "Email ada@example.com or grace@example.org; "
            "call +1 (415) 555-2671 or 212-555-0100; "
            f"keys {FAKE_OPENAI} and {FAKE_AWS}."
        )

        sanitized = layer.sanitize(text)

        self.assertEqual(layer.restore(sanitized), text)
        self.assertEqual(len(layer), 6)

    def test_forget_clears_all_types_and_resets_numbering(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize(f"{EMAIL} {PHONE} {FAKE_OPENAI}")
        layer.forget()

        self.assertEqual(len(layer), 0)
        self.assertEqual(dict(layer.mapping), {})
        self.assertEqual(layer.restore(sanitized), sanitized)
        self.assertEqual(layer.sanitize(EMAIL), "[EMAIL_1]")

    def test_placeholder_collision_with_existing_text_is_avoided(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize("[PHONE_1] then 415-555-2671")

        self.assertEqual(sanitized, "[PHONE_1] then [PHONE_2]")
        self.assertEqual(dict(layer.mapping), {"[PHONE_2]": "415-555-2671"})


class OverlapTest(unittest.TestCase):
    def test_email_wins_over_phone_inside_local_part(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize("4155552671@example.com")

        self.assertEqual(sanitized, "[EMAIL_1]")
        self.assertNotIn("[PHONE_1]", sanitized)

    def test_assigned_key_wins_over_phone_shaped_value(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize('api_key="415555267112"')

        self.assertEqual(sanitized, 'api_key="[API_KEY_1]"')
        self.assertNotIn("[PHONE_1]", sanitized)


if __name__ == "__main__":
    unittest.main()
