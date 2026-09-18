"""Lint the prompts that steer a run for sentences the code has made false.

A prompt is the one input every worker trusts without checking, so a single
false sentence in it is paid for in every session that reads it. Two such
sentences are typical: "Run it synchronously" (an ``Agent`` spawn returns in
a second, so workers invent hand-rolled waiting to fill the gap)
and "none of the statuses sends a Telegram" (some do, and the owner is told
there is nothing waiting on them while holding the ping). Which statuses ping
is read from :data:`statuses.PINGS`, so the rule moves when the code does.

Two severities, and only one of them fails ``swarm check``:

* ``contradicted`` — the prompt states something the code or the tools say is
  false. A worker that believes it does the wrong thing.
* ``wasteful`` — the prompt tells a worker to do something that works but was
  measured to burn time or tokens (sleep loops, ``git status`` polling,
  heredoc string-replace edits). Advice, not a failure.

Every rule is a line-local pattern, and a line that negates the claim ("no
``sleep`` loops", "not synchronously") is not a finding: the fixed prompts say
the right thing *about* the wrong thing, and must lint clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import statuses

CONTRADICTED = "contradicted"
WASTEFUL = "wasteful"
SEVERITIES = (CONTRADICTED, WASTEFUL)


@dataclass(frozen=True)
class Finding:
    """One suspect line. ``line`` is 1-based, ``excerpt`` the stripped source."""

    line: int
    severity: str
    rule: str
    message: str
    excerpt: str


#: A line carrying any of these is talking *against* the pattern it mentions.
_NEGATION = re.compile(
    r"\b(?:not|never|no|nothing|none|don't|do not|doesn't|does not|isn't|avoid|"
    r"ban(?:ned)?|instead of|rather than|stop)\b",
    re.I,
)
_FENCE = re.compile(r"^\s*(```|~~~)")
_SPAN = re.compile(r"`([^`\n]+)`")
_SUBCOMMAND = re.compile(r"(?<![\w./-])swarm\s+([a-z][a-z0-9_-]*)")
_DELEGATE = re.compile(r"\b(?:agent|subagent|sub-agent|teammate|survey)s?\b", re.I)
_SYNC = re.compile(r"\b(?:synchronous(?:ly)?|blocks? until|wait for (?:it|them) to (?:return|finish))\b", re.I)
_SLEEP = re.compile(r"\bsleep\s+\d")
_POLL = re.compile(r"\b(?:poll(?:ing)?|repeatedly|every \d+\s*(?:s|sec|seconds|min|minutes)\b|until)\b", re.I)
_GIT_STATUS = re.compile(r"\bgit(?:\s+-C\s+\S+)?\s+status\b")
_HEREDOC_EDIT = re.compile(r"\bsed\s+-i\b|<<\s*['\"]?PY\b.*\.replace\(|\.replace\(.*<<", re.I)
_PINGING = re.compile(r"^(?:%s)$" % "|".join(map(re.escape, sorted(statuses.PINGS))))
_SILENT = re.compile(
    r"\b(?:no|never|none|nothing|doesn't|does not|don't|do not|without)\b[^.]*\btelegram",
    re.I,
)


def _code_lines(text: str) -> list[tuple[int, str, list[str]]]:
    """``(lineno, line, code)`` per line; ``code`` is what sits inside backticks.

    A fenced block counts as code in full. Prose outside a span is not: "the
    swarm has a supervisor" must never be read as a call to ``swarm has``.
    """
    out: list[tuple[int, str, list[str]]] = []
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            out.append((n, line, []))
            continue
        out.append((n, line, [line] if fenced else _SPAN.findall(line)))
    return out


def lint(text: str, known_commands: set[str] | frozenset[str] | None = None) -> list[Finding]:
    """Every finding in ``text``, in line order.

    ``known_commands`` is the parser's own subcommand set (``cli._known_commands``);
    without it the unknown-command rule is skipped rather than guessed at.
    """
    found: list[Finding] = []

    def add(n: int, severity: str, rule: str, message: str, line: str) -> None:
        found.append(Finding(n, severity, rule, message, line.strip()))

    prev = ""
    for n, line, code in _code_lines(text):
        # Markdown wraps a sentence across lines, so the subject or the "not" is
        # often on the line above the claim ("spawn a survey agent. ⏎ Run it
        # synchronously"); a blank line ends the sentence.
        context = f"{prev} {line}"
        prev = line if line.strip() else ""
        negated = bool(_NEGATION.search(context))

        if known_commands:
            for chunk in code:
                for cmd in _SUBCOMMAND.findall(chunk):
                    if cmd not in known_commands:
                        add(n, CONTRADICTED, "unknown-command",
                            f"`swarm {cmd}` is not a swarm subcommand", line)

        if _SYNC.search(line) and _DELEGATE.search(context) and not negated:
            add(n, CONTRADICTED, "agent-synchronous",
                "an Agent spawn returns at once and reports back later; nothing blocks on it", line)

        pinging = sorted({c.strip() for c in code if _PINGING.match(c.strip())})
        if pinging and _SILENT.search(line):
            add(n, CONTRADICTED, "telegram-silent",
                f"`swarm done … {pinging[0]}` telegrams the owner", line)

        if negated:
            continue
        if any(_SLEEP.search(c) for c in code):
            add(n, WASTEFUL, "sleep-wait",
                "sleep-waiting on a delegate burns time for nothing; use Monitor", line)
        if _POLL.search(line) and (_GIT_STATUS.search(line) or any(_GIT_STATUS.search(c) for c in code)):
            add(n, WASTEFUL, "status-poll",
                "polling `git status` asks a question nothing has answered yet; wait on a signal", line)
        if any(_HEREDOC_EDIT.search(c) for c in code):
            add(n, WASTEFUL, "heredoc-edit",
                "heredoc/sed edits failed 6x more often than Edit/Write", line)

    return found


def render(findings: list[Finding], path: str = "") -> str:
    """Human-readable report for ``swarm check``: a header, then one block per finding."""
    if not findings:
        return f"{path}: clean" if path else "clean"
    bad = sum(f.severity == CONTRADICTED for f in findings)
    head = f"{path}: " if path else ""
    lines = [f"{head}{len(findings)} finding(s), {bad} contradicted"]
    for f in findings:
        lines.append(f"  {f.line:>4}  {f.severity:<12} {f.rule}: {f.message}")
        lines.append(f"        > {f.excerpt[:160]}")
    return "\n".join(lines)
