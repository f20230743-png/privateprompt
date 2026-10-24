"""Local-only redaction with reversible placeholders.

Originals never leave the process: the placeholder mapping lives in memory for
the lifetime of a :class:`PrivacyLayer` instance and is never written to disk.

Detected entity types and placeholders:

* email addresses   -> ``[EMAIL_1]``
* phone numbers     -> ``[PHONE_1]``
* API keys/tokens   -> ``[API_KEY_1]``

Numbering is independent per type. Detection is regex-based and imperfect; see
the README for known gaps. This is a prototype, not a security boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Dict, List, Mapping, Optional, Tuple

# --------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------

# Conservative pattern: requires a dotted domain with an alphabetic TLD, so
# things like "user@localhost" are intentionally left alone.
EMAIL_RE = re.compile(
    r"(?<![\w.%+-])"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    r"(?![\w-])"
)

# Candidate phone-shaped runs; digits are counted afterwards so dates, times
# and short numbers are not touched.
PHONE_RE = re.compile(r"(?<![\w.+])\+?[\d(][\d\s().-]{6,20}\d(?![\w.])")


def _looks_like_phone(value: str) -> bool:
    digits = sum(char.isdigit() for char in value)
    # E.164 allows at most 15 digits; 10 is the shortest common number.
    if not 10 <= digits <= 15:
        return False
    return value.count("(") == value.count(")")


# Common API-key shapes. This list is illustrative, not exhaustive: providers
# invent formats constantly, so unknown keys will slip through.
API_KEY_PATTERNS: Tuple[re.Pattern[str], ...] = (
    # OpenAI / Anthropic-style "sk-" keys (also sk-proj-...).
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    # Stripe live/test secret and restricted keys.
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    # AWS access key IDs.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # GitHub tokens.
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    # Slack tokens.
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
)


@dataclass(frozen=True)
class _Detector:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0
    priority: int = 100
    validate: Optional[Callable[[str], bool]] = None


# Lower priority wins when two detectors cover overlapping text.
DETECTORS: Tuple[_Detector, ...] = (
    *(_Detector("API_KEY", pattern, priority=0) for pattern in API_KEY_PATTERNS),
    # Assignment-style keys: replace only the value, keep `api_key = "` intact.
    _Detector(
        "API_KEY",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token)"
            r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{12,})[\"']?"
        ),
        group=1,
        priority=0,
    ),
    _Detector("EMAIL", EMAIL_RE, priority=1),
    _Detector("PHONE", PHONE_RE, priority=2, validate=_looks_like_phone),
)

DEFAULT_PREFIXES: Dict[str, str] = {"EMAIL": "EMAIL", "PHONE": "PHONE", "API_KEY": "API_KEY"}


class PrivacyLayer:
    """Replaces emails, phone numbers and API keys with reversible placeholders.

    The same value always maps to the same placeholder for the lifetime of the
    instance, so a model sees a stable token across turns. Values are held in
    memory only.
    """

    def __init__(
        self, prefix: str = "EMAIL", prefixes: Optional[Mapping[str, str]] = None
    ) -> None:
        # ``prefix`` is kept for backwards compatibility and renames email
        # placeholders only; ``prefixes`` can rename any entity type.
        resolved = dict(DEFAULT_PREFIXES)
        resolved["EMAIL"] = prefix
        if prefixes:
            for kind, value in prefixes.items():
                if kind not in resolved:
                    raise ValueError(f"unknown entity type: {kind!r}")
                resolved[kind] = value

        for value in resolved.values():
            if not value or not value.replace("_", "").isalnum():
                raise ValueError("prefixes must be alphanumeric (underscores allowed)")

        self._prefixes = resolved
        self._to_placeholder: Dict[Tuple[str, str], str] = {}
        self._to_original: Dict[str, str] = {}
        self._counters: Dict[str, int] = {kind: 1 for kind in resolved}

    def sanitize(self, text: str) -> str:
        """Return ``text`` with every detected sensitive value replaced."""
        return self.sanitize_with_counts(text)[0]

    def sanitize_with_counts(self, text: str) -> Tuple[str, Dict[str, int]]:
        """Like :meth:`sanitize`, plus per-type occurrence counts for this text.

        Counts describe occurrences replaced in this call (a value used twice
        counts twice) and never contain the original values.
        """
        detections = self._detections(text)
        counts: Dict[str, int] = {kind: 0 for kind in self._prefixes}
        if not detections:
            return text, counts

        parts: List[str] = []
        cursor = 0
        for start, end, kind, value in detections:
            parts.append(text[cursor:start])
            parts.append(self._placeholder_for(kind, value, text))
            cursor = end
            counts[kind] += 1
        parts.append(text[cursor:])
        return "".join(parts), counts

    def restore(self, text: str) -> str:
        """Swap known placeholders back for their originals.

        Unknown placeholders (e.g. ones invented by a model) are left untouched.
        """
        if not self._to_original:
            return text
        # Longest first so [EMAIL_10] is never matched by a [EMAIL_1] prefix.
        pattern = re.compile(
            "|".join(
                re.escape(token)
                for token in sorted(self._to_original, key=len, reverse=True)
            )
        )
        return pattern.sub(lambda match: self._to_original[match.group(0)], text)

    @property
    def mapping(self) -> Mapping[str, str]:
        """Read-only snapshot of ``placeholder -> original`` (memory only)."""
        return MappingProxyType(dict(self._to_original))

    def forget(self) -> None:
        """Drop all mappings. Placeholders in returned text become unrestorable."""
        self._to_placeholder.clear()
        self._to_original.clear()
        for kind in self._counters:
            self._counters[kind] = 1

    def __len__(self) -> int:
        return len(self._to_original)

    # -- internals ---------------------------------------------------------

    def _detections(self, text: str) -> List[Tuple[int, int, str, str]]:
        """Collect non-overlapping matches, highest priority first."""
        candidates: List[Tuple[int, int, int, str, str]] = []
        for detector in DETECTORS:
            for match in detector.pattern.finditer(text):
                value = match.group(detector.group)
                if detector.validate is not None and not detector.validate(value):
                    continue
                start, end = match.span(detector.group)
                candidates.append((detector.priority, start, -end, detector.kind, value))

        candidates.sort()
        accepted: List[Tuple[int, int, str, str]] = []
        for _priority, start, negative_end, kind, value in candidates:
            end = -negative_end
            overlaps = any(
                start < other_end and other_start < end
                for other_start, other_end, _kind, _value in accepted
            )
            if overlaps:
                continue
            accepted.append((start, end, kind, value))

        accepted.sort()
        return accepted

    def _placeholder_for(self, kind: str, value: str, text: str) -> str:
        existing = self._to_placeholder.get((kind, value))
        if existing is not None:
            return existing

        prefix = self._prefixes[kind]
        while True:
            index = self._counters[kind]
            placeholder = f"[{prefix}_{index}]"
            self._counters[kind] = index + 1
            if placeholder not in self._to_original and placeholder not in text:
                break

        self._to_placeholder[(kind, value)] = placeholder
        self._to_original[placeholder] = value
        return placeholder
