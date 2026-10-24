"""Deterministic, offline demo replies.

This module never imports the OpenAI SDK, never reads an API key, and makes no
network calls. It receives already-sanitized text only, uses it to pick an
intent, and builds a task-specific template. Placeholder tokens are replaced
with category labels before they reach the reply, so no original sensitive
value is ever echoed. ``webapp`` still restores recognized placeholders
locally for any reply that contains them.
"""

from __future__ import annotations

import re

_PLACEHOLDER_RE = re.compile(r"\[(EMAIL|PHONE|API_KEY)_\d+\]")
_LABELS = {
    "EMAIL": "an email address",
    "PHONE": "a phone number",
    "API_KEY": "an API key",
}
_REFERENCE_LABELS = (
    ("EMAIL", "email address"),
    ("PHONE", "phone number"),
    ("API_KEY", "API key"),
)

_SUMMARY_RE = re.compile(
    r"\b(summar\w*|tl;?dr|condense|recap|key points|bullet points)\b", re.I
)
_EMAIL_RE = re.compile(r"\b(e-?mail|draft|compose|letter|subject)\b", re.I)
_REPLY_RE = re.compile(r"\b(reply|respond|message)\b", re.I)
_INTERVIEW_RE = re.compile(
    r"\b(interview|interviewing|behavioral|technical screen|hiring manager)\b",
    re.I,
)

_SUMMARY_COMMAND_RE = re.compile(
    r"^(?:please\s+)?(?:summari[sz]e|summary of|summary|tl;?dr|condense|recap)"
    r"\b[\s,:-]*",
    re.I,
)
_EMAIL_COMMAND_RE = re.compile(
    r"^(?:please\s+)?(?:write|draft|compose|send|prepare)\s+(?:me\s+)?"
    r"(?:a|an)?\s*(?:short|brief|professional|formal|polite)?\s*"
    r"(?:e-?mail|message|note|letter|reply)\b[\s,:-]*",
    re.I,
)
_RECIPIENT_RE = re.compile(
    r"^(?:to|for)\s+(?:an email address|a phone number|an API key)\b[\s,:-]*",
    re.I,
)


def generate_demo_reply(sanitized_prompt: str) -> str:
    """Pick a task-specific template from sanitized intent words only."""
    if _SUMMARY_RE.search(sanitized_prompt):
        return _summary(sanitized_prompt)
    if _EMAIL_RE.search(sanitized_prompt):
        return _email(sanitized_prompt)
    if _INTERVIEW_RE.search(sanitized_prompt):
        return _interview()
    if _REPLY_RE.search(sanitized_prompt):
        return _email(sanitized_prompt)
    return _general()


def _flatten(text: str) -> str:
    return " ".join(text.split())


def _without_placeholders(text: str) -> str:
    return _PLACEHOLDER_RE.sub(lambda match: _LABELS[match.group(1)], text)


def _redacted_references(sanitized_prompt: str) -> str:
    parts = []
    for kind, label in _REFERENCE_LABELS:
        count = len(re.findall(rf"\[{kind}_\d+\]", sanitized_prompt))
        if count:
            parts.append(f"{count} {label}{'' if count == 1 else 's'}")
    return ", ".join(parts) if parts else "none detected"


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-") + "…"


def _email_topic(sanitized_prompt: str) -> str:
    text = _flatten(_without_placeholders(sanitized_prompt))

    about = re.search(r"\b(?:about|regarding|re:)\s+(.+)$", text, re.I)
    if about:
        topic = about.group(1)
    else:
        topic = _EMAIL_COMMAND_RE.sub("", text, count=1)
        topic = _RECIPIENT_RE.sub("", topic, count=1)

    topic = topic.strip(" \t\r\n.,:;-")
    topic = _shorten(topic, 90).strip(" ,.;:-")
    return topic or "our recent discussion"


def _email(sanitized_prompt: str) -> str:
    topic = _email_topic(sanitized_prompt)
    subject = re.sub(r"^(?:the|a|an)\s+", "", topic, flags=re.I)
    subject = _shorten(subject.strip(" ,.;:-"), 60) or "Quick update"
    subject = subject[0].upper() + subject[1:]

    return (
        f"Subject: {subject}\n\n"
        "Hi [Recipient Name],\n\n"
        f"I'm writing about {topic}. I wanted to share a brief update and "
        "confirm the next steps.\n\n"
        "Please let me know if you have any questions or would like to discuss "
        "this further. I am happy to adjust the approach as needed.\n\n"
        "Thank you for your time.\n\n"
        "Best regards,\n"
        "[Your Name]"
    )


def _summary(sanitized_prompt: str) -> str:
    text = _flatten(_without_placeholders(sanitized_prompt))
    text = _SUMMARY_COMMAND_RE.sub("", text, count=1)
    text = re.sub(
        r"^(?:this\s+|the\s+)?(?:e-?mail|message|text|passage|note)\s+"
        r"(?:from|by)\s+(?:an email address|a phone number|an API key)"
        r"\s*[:\-]\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"^(?:this|the following)\b[\s,:-]*", "", text, flags=re.I
    ).strip()

    sentences = re.split(r"(?<=[.!?])\s+", text)
    key_point = _shorten(" ".join(sentences[:2]).strip(), 260)
    if not key_point:
        key_point = "The sanitized request contains only redacted references."

    return (
        "Summary (demo)\n\n"
        f"- Key point: {key_point}\n"
        f"- Redacted references: {_redacted_references(sanitized_prompt)}\n\n"
        "Extracted from the sanitized input only; no new facts were added."
    )


def _interview() -> str:
    return (
        "Interview preparation (demo)\n\n"
        "Sample questions\n"
        "1. Tell me about yourself and what interests you in this role.\n"
        "2. Walk me through a project you are proud of and your specific "
        "contribution.\n"
        "3. Describe a time you handled competing priorities or a tight "
        "deadline.\n"
        "4. Tell me about a disagreement with a teammate and how you resolved "
        "it.\n"
        "5. What questions do you have for us?\n\n"
        "Preparation tips\n"
        "- Use the STAR method (situation, task, action, result) for behavioral "
        "answers.\n"
        "- Prepare two or three concrete examples with measurable outcomes.\n"
        "- Research the team and role, then prepare thoughtful questions.\n"
        "- Practice concise answers out loud and leave time for follow-ups."
    )


def _general() -> str:
    return (
        "Demo assistant (offline)\n\n"
        "This demo supports three request types:\n"
        "- Email writing: ask for a draft, reply, or subject line.\n"
        "- Summarization: ask to summarize or condense a passage.\n"
        "- Interview preparation: ask for sample questions or preparation "
        "tips.\n\n"
        "Rewrite your prompt as one of these, or choose Ask AI when "
        "OPENAI_API_KEY is set. This generic response does not attempt to "
        "interpret unsupported requests."
    )
