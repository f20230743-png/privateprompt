"""Try the privacy layer locally.

Usage:
    python3 demo.py                  # built-in sample text
    echo "hi a@b.com" | python3 demo.py

Only sanitized text and a round-trip check are printed. Original values are
deliberately never echoed, so the demo output is safe to paste into a log.
"""

from __future__ import annotations

import sys

from privacy import PrivacyLayer

SAMPLE = (
    "Hi team, loop in ada@example.com or call +1 (415) 555-2671. "
    "Sandbox key: sk-test-0123456789abcdef; do not share it."
)


def main() -> None:
    text = "" if sys.stdin.isatty() else sys.stdin.read()
    if not text.strip():
        text = SAMPLE

    layer = PrivacyLayer()
    sanitized = layer.sanitize(text)
    restored = layer.restore(sanitized)

    print("--- sanitized (safe to share) ---")
    print(sanitized)
    print()
    print(f"{len(layer)} sensitive value(s) replaced, held in memory for this process only.")
    print(f"round-trip restores exact input: {restored == text}")
    print("(originals are deliberately not printed)")


if __name__ == "__main__":
    main()
