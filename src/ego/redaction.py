from __future__ import annotations

import os
import re

SENSITIVE_NAME_PARTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
TOKEN_PATTERNS = (
    re.compile(r"\b(?:sk|ghp|gho|ghu|github_pat)_[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(
        r'(?i)("?(?:api[_-]?key|access[_-]?token|secret|password)"?\s*[:=]\s*["\'])'
        r"[^\"']+"
    ),
)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for name, secret in os.environ.items():
        if (
            secret
            and len(secret) >= 8
            and any(part in name.upper() for part in SENSITIVE_NAME_PARTS)
        ):
            redacted = redacted.replace(secret, "***REDACTED***")
    for pattern in TOKEN_PATTERNS:
        redacted = pattern.sub(
            lambda match: (
                f"{match.group(1)}***REDACTED***" if match.lastindex else "***REDACTED***"
            ),
            redacted,
        )
    return redacted
