"""Strip anything shaped like a credential from text the board serves.

The board is open on the LAN by the owner's choice, and the texts it relays —
worker recaps, completion notes, operator outcomes, a worker's last turn — are
written by sessions that handle deploy keys and API tokens. One pasted
``Authorization:`` line in a recap would put a live token on every phone in the
house. So every string leaving the server passes through :func:`redact`.

Patterns, not a vault lookup: the server has no business knowing the secrets it
must not show. Deliberately greedy on the shapes that are unambiguous (``sk-…``,
``ghp_…``, ``Bearer …``, a PEM block) and on any long opaque value written right
after a word like *key*/*token*/*secret*/*password*. A false positive costs a
``[redacted]`` in a sentence; a false negative costs a credential.
"""

from __future__ import annotations

import re
from typing import Any

MARK = "[redacted]"

#: The words that announce a credential follows.
_NAMES = r"(?:key|secret|token|passw(?:or)?d|private[_-]?key)"

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # A PEM private key, whole.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
                re.S), MARK),
    # Provider-shaped tokens: Anthropic/OpenAI `sk-…`, GitHub `ghp_/gho_/ghs_/ghu_/ghr_`
    # and fine-grained `github_pat_…`, Slack `xox?-…`, AWS access key ids, Google keys.
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"), MARK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MARK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), MARK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MARK),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), MARK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MARK),
    # A Telegram bot token: `123456789:AA…`.
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}"), MARK),
    # `Bearer <token>` / `Basic <b64>` in an Authorization header or anywhere.
    (re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 " + MARK),
    # `api_key = "…"`, `TOKEN: …`, `secret=…`: keep the name, drop the value —
    # when it is long and opaque enough to be a credential. After a bare space
    # the value must also mix letters and digits, so prose ("the secret
    # ingredient_of_everything") is left alone while `token abc123…` is not.
    (re.compile(
        r"(?i)\b([A-Za-z0-9_.-]*" + _NAMES + r"[A-Za-z0-9_.-]*)"
        r"(\s*[:=]\s*)([\"']?)([A-Za-z0-9+/_.~=-]{16,})\3"
    ), r"\1\2\3" + MARK + r"\3"),
    (re.compile(
        r"(?i)\b([A-Za-z0-9_.-]*" + _NAMES + r"[A-Za-z0-9_.-]*)"
        r"(\s+)(?=[A-Za-z0-9+/_.~=-]*\d)(?=[A-Za-z0-9+/_.~=-]*[A-Za-z])[A-Za-z0-9+/_.~=-]{20,}"
    ), r"\1\2" + MARK),
    # Credentials inside a URL: `https://user:pass@host`.
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+@"), r"\1" + MARK + "@"),
)


def redact(text: str) -> str:
    """``text`` with every credential-shaped run replaced by ``[redacted]``."""
    if not text or not isinstance(text, str):
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def deep(value: Any) -> Any:
    """:func:`redact` applied to every string inside a JSON-shaped value.

    The server runs the whole payload through this once, at the edge, so no
    field added to the board later can forget to.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: deep(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep(v) for v in value]
    return value
