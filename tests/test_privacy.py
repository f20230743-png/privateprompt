import unittest

from privacy import PrivacyLayer

ORIGINAL = "ada@example.com"


class SanitizeTest(unittest.TestCase):
    def test_original_email_is_absent_from_sanitized_output(self):
        layer = PrivacyLayer()
        text = f"Ping {ORIGINAL} about the launch, please."

        sanitized = layer.sanitize(text)

        self.assertNotIn(ORIGINAL, sanitized)
        self.assertNotIn("example.com", sanitized)
        self.assertIn("[EMAIL_1]", sanitized)

    def test_placeholder_round_trips_back_to_original(self):
        layer = PrivacyLayer()
        text = f"Contact {ORIGINAL}."

        self.assertEqual(layer.restore(layer.sanitize(text)), text)

    def test_multiple_emails_get_distinct_placeholders(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize("a@one.com, b@two.org, c@three.net")

        self.assertEqual(sanitized, "[EMAIL_1], [EMAIL_2], [EMAIL_3]")
        self.assertEqual(len(layer), 3)

    def test_same_email_reuses_one_placeholder(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize(f"{ORIGINAL} and {ORIGINAL} again")

        self.assertEqual(sanitized, "[EMAIL_1] and [EMAIL_1] again")
        self.assertEqual(len(layer), 1)

    def test_text_without_email_is_unchanged(self):
        layer = PrivacyLayer()
        text = "No addresses here, just [EMAIL_1]-free text."

        self.assertEqual(layer.sanitize(text), text)
        self.assertEqual(len(layer), 0)

    def test_punctuation_and_case_are_preserved(self):
        layer = PrivacyLayer()
        text = "Ask Ada@Example.COM, thanks."

        self.assertEqual(layer.sanitize(text), "Ask [EMAIL_1], thanks.")

    def test_mapping_is_read_only_and_kept_in_memory(self):
        layer = PrivacyLayer()
        layer.sanitize(ORIGINAL)

        with self.assertRaises(TypeError):
            layer.mapping["[EMAIL_1]"] = "tampered@example.com"  # type: ignore[index]

    def test_forget_makes_mapping_empty(self):
        layer = PrivacyLayer()
        sanitized = layer.sanitize(ORIGINAL)
        layer.forget()

        self.assertEqual(len(layer), 0)
        self.assertEqual(layer.restore(sanitized), sanitized)

    def test_unknown_placeholders_are_left_alone(self):
        layer = PrivacyLayer()
        layer.sanitize(ORIGINAL)

        self.assertEqual(layer.restore("[EMAIL_9]"), "[EMAIL_9]")


if __name__ == "__main__":
    unittest.main()
