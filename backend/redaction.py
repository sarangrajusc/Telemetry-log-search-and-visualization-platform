"""
Credential-exposure remediation for ingested logs.

Applications routinely log things they shouldn't: bearer tokens, `password=...`,
connection strings with embedded credentials, cloud keys. Once such a line is indexed
it is searchable by anyone who can query the platform (and by an AI assistant over
MCP). This module scrubs known secret shapes before a log is stored, replacing each
with `[REDACTED:<kind>]` so the line stays useful for debugging.

It is pattern-based, so it catches common formats rather than every possible secret.
Treat it as defence in depth, not a substitute for fixing the code that logs secrets.
Disable with REDACT_SECRETS=0 (not recommended).
"""
import os
import re

ENABLED = os.environ.get("REDACT_SECRETS", "1").lower() not in ("0", "false", "no")

# Order matters: more specific shapes first so they win over the generic key=value rule.
_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("url-credentials", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")),
    ("bearer-token", re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("secret-assignment", re.compile(
        r"(?i)\b(password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?token|auth[_-]?token|token)"
        r"(\"?\s*[:=]\s*\"?)([^\s\"',;&]+)"
    )),
]


def redact(text: str) -> str:
    if not ENABLED or not text:
        return text
    out = text
    for kind, rx in _PATTERNS:
        if kind in ("bearer-token",):
            out = rx.sub(lambda m: f"{m.group(1)} [REDACTED:{kind}]", out)
        elif kind == "secret-assignment":
            out = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:secret]", out)
        else:
            out = rx.sub(f"[REDACTED:{kind}]", out)
    return out


def redact_value(value):
    """Recursively redact strings inside dicts/lists (used for log `tags`)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: ("[REDACTED:secret]" if _sensitive_key(k) and isinstance(v, str) else redact_value(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


_SENSITIVE_KEY = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential)")


def _sensitive_key(key) -> bool:
    return bool(ENABLED and isinstance(key, str) and _SENSITIVE_KEY.search(key))
