"""Redaction for anything that leaves the business's own view: support
cases, assistant evidence and stored error text (ADR 0020)."""

from __future__ import annotations

import re

_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1[redacted]"),
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|client_secret|token|api[_-]?key|secret|password)"
            r"(\"?\s*[:=]\s*\"?)[^\s\"&,}]+"
        ),
        r"\1\2[redacted]",
    ),
    (re.compile(r"\bpat-[a-z0-9-]{10,}\b", re.I), "[redacted]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+"), "[redacted]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[email]"),
    (re.compile(r"\+?\d[\d\s().-]{8,}\d"), "[number]"),
    (re.compile(r"\b[A-Za-z0-9_-]{40,}\b"), "[redacted]"),
]


def redact(text: str, limit: int = 500) -> str:
    out = str(text or "")
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out[:limit]
