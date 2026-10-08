"""Optional model help for automation (ADR 0020).

Every feature that uses the model also works without it: when no key is set
or the model fails, callers fall back to deterministic rules. The model's
output is data to validate, never instructions or settings.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger("exaconnect.commai.automation")


def available(settings: Any) -> bool:
    return bool(getattr(settings, "llm_api_key", ""))


def complete_json(settings: Any, system: str, user: str, max_tokens: int = 1500) -> Any | None:
    """Ask for a JSON answer. None when there is no key or no usable answer."""
    if not available(settings):
        return None
    from ...ai import ask

    try:
        text = ask.chat(
            system,
            user,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            max_tokens=max_tokens,
            temperature=0.1,
        )
    except ask.AskError as e:
        log.info("model unavailable for automation: %s", e)
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        return None
    try:
        return json.loads(text[start:])
    except ValueError:
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except ValueError:
            return None
