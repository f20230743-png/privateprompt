"""OpenAI Responses API wrapper for already-sanitized text.

This module never receives the original prompt or the placeholder mapping.
It only receives the sanitized string from the privacy engine, sends it with
non-sensitive parameters, and returns the raw model text. Restoration happens
in ``webapp`` against the current session's in-memory mapping.

Nothing here logs, prints, or persists prompts or responses.
"""

from __future__ import annotations

import os
import re

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

API_KEY_ENV = "OPENAI_API_KEY"
MODEL_ENV = "OPENAI_MODEL"
DEFAULT_MODEL = "gpt-4.1-mini"
TIMEOUT_SECONDS = 20.0

_MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class AIError(Exception):
    """Base class for safe, generic provider failures."""


class AINotConfigured(AIError):
    """No API key is set, or the provider rejected the key."""


class AIRateLimited(AIError):
    """The provider asked us to slow down."""


class AITimeout(AIError):
    """The provider did not answer in time."""


class AIBadResponse(AIError):
    """The provider returned no usable text."""


class AIUnavailable(AIError):
    """Connection or provider-side failure."""


def _model_name() -> str:
    candidate = os.environ.get(MODEL_ENV, "").strip()
    if _MODEL_RE.match(candidate):
        return candidate
    return DEFAULT_MODEL


def generate_reply(sanitized_prompt: str) -> str:
    """Send one sanitized prompt via the Responses API and return model text."""
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise AINotConfigured()

    client = OpenAI(api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=0)
    try:
        response = client.responses.create(
            model=_model_name(),
            input=sanitized_prompt,
            store=False,
        )
    except APITimeoutError:
        raise AITimeout() from None
    except RateLimitError:
        raise AIRateLimited() from None
    except AuthenticationError:
        raise AINotConfigured() from None
    except APIConnectionError:
        raise AIUnavailable() from None
    except APIStatusError:
        raise AIUnavailable() from None
    except OpenAIError:
        raise AIUnavailable() from None

    text = getattr(response, "output_text", None)
    if not isinstance(text, str) or not text.strip():
        raise AIBadResponse()
    return text.strip()
