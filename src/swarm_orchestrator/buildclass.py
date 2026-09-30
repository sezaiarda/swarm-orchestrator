"""Which ``swarm build`` commands need the gate, and which can just go out.

The gate exists to keep heavy work -- compiling, testing, bundling, building
images -- to ``[build].max_concurrent`` at a time. A ``cargo update``, a
``git status`` or a ``docker buildx bake --print`` does none of that, yet it used
to wait in the same queue as a full test suite. :func:`classify` reads the
command the way the shell would run it and says ``heavy`` (queue) or ``light``
(run now).

The rules are deliberately one-sided: **anything not known to be light is
heavy.** An unknown program, a script whose contents cannot be read, a command
built from a variable, ``eval``, command substitution, a heredoc, a shell
function -- all heavy. A wrong "light" breaks the one-heavy-build-at-a-time
promise; a wrong "heavy" only costs a wait.

What is understood:

- **Wrappers** are looked through: ``env X=1 cmd``, ``timeout 600 cmd``,
  ``nice``/``ionice``/``nohup``/``setsid``/``stdbuf``/``time``/``exec``/``sudo``/
  ``flock FILE cmd``/``xargs cmd``/``find -exec cmd``, ``uv run cmd``,
  ``bunx cmd``, ``swarm build cmd``.
- **Shell scripts** (``sh -c '...'``, ``bash file.sh``, an executable with a
  ``#!`` shell line, ``source file``): split into simple commands on ``&&``,
  ``||``, ``;``, ``|``, ``&`` and newlines; the script is light only if every
  command in it is.
- **Python** (``python x.py``, ``python -c``, ``uv run x.py``, a ``#!`` python
  script): light unless the code starts processes (``subprocess``, ``os.system``,
  ``multiprocessing``...), runs tests, builds packages or loads an ML framework.
- **Package scripts** (``bun run build``): the script's own command line from
  the nearest ``package.json`` is classified.
- **Tools** by subcommand: ``cargo``, ``docker``, ``bun``/``npm``/``pnpm``/
  ``yarn``, ``uv``, ``go``, ``make``, ``git``, ``rustup``.

``[build].heavy`` / ``[build].light`` add patterns that win over every rule
here: a pattern is a command prefix whose words are globs, matched against each
simple command (``"cargo check"``, ``"scripts/*.sh"``, ``"bun run lint*"``).
Heavy patterns win over light ones.

Classification also collects **pre-flight requirements** -- things that must
exist for the command to have any chance (the executable, a ``cd`` target, a
``-f`` file, a ``Cargo.toml``, a bake file) -- which :func:`preflight` checks
before the command is queued, so a typo fails in a second instead of after the
queue. A requirement is only recorded while nothing earlier in the command
could have created it (a ``mkdir`` or a build step ends that stretch).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

HEAVY = "heavy"
LIGHT = "light"

_MAX_DEPTH = 4
_MAX_READ = 256 * 1024
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_VERSION_ARGS = {"--version", "-V", "--help", "-h"}

# Programs that never compile, test or bundle anything.
LIGHT_NAMES = frozenset("""
    : true false echo printf test [ [[ pwd cd pushd popd set shopt export unset local declare
    readonly trap wait shift exit return read type hash which whereis alias
    ls cat head tail grep egrep fgrep rg ag fd find wc sort uniq cut tr sed awk gawk jq yq diff
    cmp comm
    sleep date realpath readlink basename dirname tee xxd od hexdump file stat du df tree column
    paste join seq mkdir rmdir rm cp mv ln touch chmod chown install mktemp
    sha1sum sha256sum sha512sum md5sum b2sum cksum printenv id whoami hostname uname nproc free
    uptime ps pgrep pkill kill lsof ss ip
    git gh curl wget ssh scp rsync tar gzip gunzip zstd unzstd xz unzip zip base64
    rustfmt prettier shellcheck shfmt ruff black isort codespell yamllint taplo
""".split())

# Programs that always count as heavy (they compile, test, bundle or build images).
HEAVY_NAMES = frozenset("""
    rustc rustdoc cc c++ gcc g++ clang clang++ ld lld mold cmake ninja meson scons bazel
    gradle gradlew mvn ant javac dotnet zig tsc tsx vite vitest jest mocha playwright esbuild
    webpack rollup parcel turbo nx next astro svelte-kit node deno pytest tox nox ctest mypy pyright
    eval
""".split())

_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
# Once any other step has run, a later path may have been created by it.
_READONLY = frozenset({
    ":", "true", "false", "echo", "printf", "test", "[", "[[", "pwd", "cd", "pushd", "set",
    "shopt", "export", "unset", "local", "declare", "readonly", "trap", "type", "which",
    "hash", "ls", "cat", "head", "tail", "grep", "rg", "wc", "printenv", "sleep", "date",
    "env", "timeout", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "exec",
})
_KEYWORDS_LEAD = ("if", "then", "else", "elif", "do", "while", "until", "!", "{", "time")
_KEYWORDS_ALONE = ("fi", "done", "}", "esac")
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n", "|&", ";;", ";&", ";;&"})
_REDIRECTS = frozenset({">", ">>", "<", ">&", "<&", "&>", "&>>", ">|", "<>", "<<<"})

BAKE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml",
              "docker-bake.json", "docker-bake.hcl", "docker-bake.override.json",
              "docker-bake.override.hcl")
COMPOSE_FILES = BAKE_FILES[:4]
MAKE_FILES = ("GNUmakefile", "makefile", "Makefile")


_OPS = sorted(_SEPARATORS | _REDIRECTS | {"(", ")"}, key=len, reverse=True)


def _split_ops(tok: str) -> list[str]:
    """shlex glues adjacent punctuation (``)&&``): split it into real operators."""
    if not tok or tok in _OPS or any(c not in "();<>|&\n" for c in tok):
        return [tok]
    out: list[str] = []
    while tok:
        op = next((o for o in _OPS if tok.startswith(o)), tok[0])
        out.append(op)
        tok = tok[len(op):]
    return out


def _strip_comments(text: str) -> str:
    """Drop ``# ...`` comments the way the shell does: only where ``#`` starts a
    word outside quotes (``a#b`` and ``'#'`` are not comments)."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c == "\\" and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 2
            continue
        elif c in ("'", '"'):
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        out.append(c)
        i += 1
    return "".join(out)


@dataclass
class Req:
    """Something that must exist before the command is worth queueing."""

    # "exe" | "file" | "dir" | "any" (one of ``paths``) | "up"/"anyup" (here or above)
    # | "script" (a package script already known to be missing)
    kind: str
    paths: tuple[str, ...]
    base: str  # directory relative paths resolve against
    what: str  # plain words for the error ("cd target", "compose file"...)


@dataclass
class Step:
    """One simple command the shell would run, after looking through wrappers."""

    argv: list[str]
    cls: str
    why: str


@dataclass
class Verdict:
    cls: str
    why: str
    steps: list[Step] = field(default_factory=list)
    reqs: list[Req] = field(default_factory=list)

    @property
    def heavy_steps(self) -> list[Step]:
        return [s for s in self.steps if s.cls == HEAVY]


def _pattern_hit(patterns: list[str], toks: list[str]) -> bool:
    for pat in patterns:
        try:
            words = shlex.split(pat)
        except ValueError:
            continue
        if not words or len(words) > len(toks):
            continue
        first = fnmatch.fnmatchcase(toks[0], words[0]) or fnmatch.fnmatchcase(
            os.path.basename(toks[0]), words[0])
        if first and all(fnmatch.fnmatchcase(t, w) for t, w in zip(toks[1:], words[1:])):
            return True
    return False


def _only_version(args: list[str]) -> bool:
    return bool(args) and all(a in _VERSION_ARGS for a in args)


def _skip_opts(args: list[str], arity: dict[str, int], stop_at_first: bool = True) -> int:
    """Index of the first non-option word; ``arity`` names options taking values."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return i + 1
        if not a.startswith("-") or a == "-":
            return i
        name = a.split("=", 1)[0]
        i += 1 if "=" in a else 1 + arity.get(name, 0)
    return i


def _find_up(start: Path, names: tuple[str, ...]) -> Path | None:
    for d in (start, *start.parents):
        for n in names:
            if (d / n).exists():
                return d / n
    return None


def _read_text(path: Path) -> str | None:
    try:
        with path.open("rb") as fh:
            data = fh.read(_MAX_READ + 1)
    except OSError:
        return None
    if len(data) > _MAX_READ or b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


_PY_HEAVY = re.compile(
    r"\b(subprocess|os\.system|os\.exec\w*|os\.spawn\w*|os\.popen|os\.fork|Popen|pty\.spawn|"
    r"multiprocessing|ProcessPoolExecutor|pytest|unittest|setuptools|distutils|Cython|"
    r"torch|tensorflow|jax|onnxruntime)\b")
_PY_LIGHT_MODULES = frozenset({"json.tool", "py_compile", "tomllib", "this", "site", "platform"})


class _Walk:
    """One classification: walks the command, collecting steps and requirements."""

    def __init__(self, heavy: list[str], light: list[str], path: str | None):
        self.heavy_pats = heavy
        self.light_pats = light
        self.path = path if path is not None else os.environ.get("PATH", os.defpath)
        self.steps: list[Step] = []
        self.reqs: list[Req] = []
        self.trust = True  # no earlier step could have created a path we check
        self.path_ok = True  # PATH has not been changed by the command itself

    # -- requirements -----------------------------------------------------
    def need(self, kind: str, paths: tuple[str, ...], base: Path | None, what: str) -> None:
        if self.trust and base is not None:
            self.reqs.append(Req(kind, paths, str(base), what))

    def need_exe(self, name: str, base: Path | None, top: bool) -> None:
        if "/" in name:
            self.need("exe", (name,), base, "program")
        elif top and self.path_ok and name not in LIGHT_NAMES:
            self.need("exe", (name,), base, "program")

    # -- entry points -----------------------------------------------------
    def argv(self, toks: list[str], cwd: Path | None) -> tuple[str, str]:
        cls, why, _ = self.cmd(toks, cwd, 0, top=True)
        return cls, why

    def cmd(self, toks: list[str], cwd: Path | None, depth: int,
            top: bool = False) -> tuple[str, str, Path | None]:
        """Classify one simple command. Returns (cls, why, cwd after it)."""
        i = 0
        while i < len(toks) and _ASSIGN.match(toks[i]):
            if toks[i].startswith("PATH="):
                self.path_ok = False
            i += 1
        toks = toks[i:]
        if not toks:
            self.steps.append(Step(["export"], LIGHT, "variable assignment"))
            return LIGHT, "variable assignment", cwd
        if _pattern_hit(self.heavy_pats, toks):
            return self._leaf(toks, HEAVY, "matches [build].heavy", cwd)
        if _pattern_hit(self.light_pats, toks):
            return self._leaf(toks, LIGHT, "matches [build].light", cwd)
        name = os.path.basename(toks[0])
        args = toks[1:]
        if toks[0].startswith("$") or "$(" in toks[0] or "`" in toks[0]:
            return self._leaf(toks, HEAVY, "the program comes from a variable", cwd)
        self.need_exe(toks[0], cwd, top)
        if name in ("cd", "pushd"):
            return self._cd(toks, cwd)
        if name == "popd":
            return self._leaf(toks, LIGHT, "popd", None)
        wrapped = self._wrapper(name, args, cwd, depth, top)
        if wrapped is not None:
            return wrapped
        if name in _SHELLS:
            return self._shell(args, cwd, depth)
        if name in ("source", "."):
            self.path_ok = False
            return self._script_file(args[0] if args else "", cwd, depth, run_cwd=cwd)
        if _only_version(args):
            return self._leaf(toks, LIGHT, "version/help only", cwd)
        rule = _TOOLS.get(name)
        if rule is not None:
            cls, why = rule(self, args, cwd, depth)
            return self._leaf(toks, cls, why, cwd)
        if name in LIGHT_NAMES:
            return self._leaf(toks, LIGHT, f"{name} does not compile", cwd)
        if name in HEAVY_NAMES:
            return self._leaf(toks, HEAVY, f"{name} is build/test work", cwd)
        return self._program(toks, cwd, depth)

    def _leaf(self, toks: list[str], cls: str, why: str,
              cwd: Path | None) -> tuple[str, str, Path | None]:
        self.steps.append(Step(list(toks), cls, why))
        return cls, why, cwd

    def _cd(self, toks: list[str], cwd: Path | None) -> tuple[str, str, Path | None]:
        args = [a for a in toks[1:] if a not in ("-L", "-P", "-e", "--")]
        if not args:
            return self._leaf(toks, LIGHT, "cd", Path.home())
        target = args[0]
        if target == "-" or "$" in target or "`" in target or any(c in target for c in "*?["):
            return self._leaf(toks, LIGHT, "cd", None)
        target = os.path.expanduser(target)
        if target.startswith("/"):
            new: Path | None = Path(os.path.normpath(target))
            self.need("dir", (target,), Path("/"), "cd target")
        elif cwd is not None:
            new = Path(os.path.normpath(cwd / target))
            self.need("dir", (target,), cwd, "cd target")
        else:
            new = None
        return self._leaf(toks, LIGHT, "cd", new)

    # -- wrappers ---------------------------------------------------------
    def _wrapper(self, name: str, args: list[str], cwd: Path | None, depth: int,
                 top: bool) -> tuple[str, str, Path | None] | None:
        """Look through a program that just runs another one."""
        inner: list[str] | None
        chdir: str | None = None
        if name == "env":
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("-u", "--unset"):
                    i += 2
                elif a in ("-C", "--chdir"):
                    chdir = args[i + 1] if i + 1 < len(args) else None
                    i += 2
                elif a.startswith("--chdir="):
                    chdir = a.split("=", 1)[1]
                    i += 1
                elif a in ("-S", "--split-string") or a.startswith("--split-string="):
                    return self._leaf([name, *args], HEAVY, "env -S is not read", cwd)
                elif a == "--":
                    i += 1
                    break
                elif a.startswith("-") and a != "-":
                    i += 1
                elif _ASSIGN.match(a):
                    if a.startswith("PATH="):
                        self.path_ok = False
                    i += 1
                else:
                    break
            inner = args[i:]
        elif name == "timeout":
            i = _skip_opts(args, {"-s": 1, "--signal": 1, "-k": 1, "--kill-after": 1})
            inner = args[i + 1:]
        elif name == "nice":
            i = 0
            while i < len(args) and args[i].startswith("-"):
                i += 2 if args[i] in ("-n", "--adjustment") else 1
            inner = args[i:]
        elif name == "ionice":
            inner = args[_skip_opts(args, {"-c": 1, "-n": 1, "--class": 1, "--classdata": 1})
                         :]
        elif name in ("nohup", "builtin"):
            inner = args
        elif name == "setsid":
            inner = args[_skip_opts(args, {}):]
        elif name == "exec":
            inner = args[_skip_opts(args, {"-a": 1}):]
        elif name == "command":
            if any(a in ("-v", "-V") for a in args[:2]):
                return self._leaf([name, *args], LIGHT, "command -v", cwd)
            inner = args[_skip_opts(args, {}):]
        elif name == "stdbuf":
            inner = args[_skip_opts(args, {"-i": 1, "-o": 1, "-e": 1}):]
        elif name == "time":
            inner = args[_skip_opts(args, {"-f": 1, "--format": 1, "-o": 1, "--output": 1}):]
        elif name == "sudo":
            inner = args[_skip_opts(args, {"-u": 1, "-g": 1, "-p": 1, "-C": 1, "-D": 1,
                                           "-h": 1, "-U": 1, "-r": 1, "-t": 1}):]
        elif name == "taskset":
            i = _skip_opts(args, {})
            if any(a in ("-p", "--pid") for a in args[:i]):
                return self._leaf([name, *args], LIGHT, "taskset -p", cwd)
            inner = args[i + 1:]
        elif name == "flock":
            i = _skip_opts(args, {"-w": 1, "--wait": 1, "--timeout": 1, "-E": 1,
                                  "--conflict-exit-code": 1})
            rest = args[i + 1:]
            if rest and rest[0] in ("-c", "--command"):
                return self._script(rest[1] if len(rest) > 1 else "", cwd, depth + 1)
            inner = rest
        elif name == "xargs":
            inner = args[_skip_opts(args, {"-a": 1, "-d": 1, "-E": 1, "-I": 1, "-L": 1,
                                           "-n": 1, "-P": 1, "-s": 1, "--arg-file": 1,
                                           "--delimiter": 1, "--max-args": 1,
                                           "--max-procs": 1, "--max-lines": 1}):]
            if not inner:
                return self._leaf([name, *args], LIGHT, "xargs echo", cwd)
        elif name == "find":
            execs = [i for i, a in enumerate(args) if a in ("-exec", "-execdir", "-ok", "-okdir")]
            if not execs:
                return None
            worst = (LIGHT, "find -exec of light commands", cwd)
            for i in execs:
                end = next((j for j in range(i + 1, len(args)) if args[j] in (";", "+")),
                           len(args))
                sub = [a for a in args[i + 1:end] if a != "{}"]
                res = self.cmd(sub, cwd, depth + 1) if sub else (LIGHT, "", cwd)
                if res[0] == HEAVY:
                    worst = (HEAVY, f"find -exec: {res[1]}", cwd)
            return worst
        elif name == "swarm":
            if args[:1] == ["build"]:
                rest = args[1:]
                rest = rest[_skip_opts(rest, {"--timeout": 1, "--script": 1}):]
                return self.cmd(rest, cwd, depth + 1) if rest else (LIGHT, "swarm build", cwd)
            return self._leaf([name, *args], LIGHT, "swarm subcommand", cwd)
        elif name in ("bunx", "npx", "pnpx"):
            inner = args[_skip_opts(args, {"-p": 1, "--package": 1}):]
        else:
            return None
        if chdir is not None:
            self.need("dir", (chdir,), cwd, "env -C directory")
            cwd = (cwd / chdir) if cwd is not None else None
        if not inner:
            return self._leaf([name, *args], LIGHT, f"{name} with no command", cwd)
        # Only a wrapper that keeps PATH as it is lets the inner program be checked
        # on it: bunx fetches, sudo resets PATH.
        top = top and name not in ("bunx", "npx", "pnpx", "sudo")
        cls, why, _ = self.cmd(inner, cwd, depth + 1, top=top)
        return cls, why, cwd

    # -- shells -----------------------------------------------------------
    def _shell(self, args: list[str], cwd: Path | None,
               depth: int) -> tuple[str, str, Path | None]:
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-o", "+o", "-O", "+O"):
                i += 2
                continue
            if a == "--":
                i += 1
                break
            if a.startswith("--"):
                i += 1
                continue
            if a[:1] in ("-", "+") and len(a) > 1:
                if "c" in a[1:]:
                    script = args[i + 1] if i + 1 < len(args) else ""
                    return self._script(script, cwd, depth + 1)
                if "s" in a[1:] or "i" in a[1:]:
                    return self._leaf(["sh", *args], HEAVY, "shell reading stdin", cwd)
                i += 1
                continue
            break
        if i >= len(args):
            return self._leaf(["sh", *args], HEAVY, "shell reading stdin", cwd)
        return self._script_file(args[i], cwd, depth, run_cwd=cwd)

    def _script_file(self, path: str, cwd: Path | None, depth: int,
                     run_cwd: Path | None) -> tuple[str, str, Path | None]:
        if not path:
            return self._leaf([path], HEAVY, "no script given", cwd)
        self.need("file", (path,), cwd, "script")
        full = Path(os.path.expanduser(path))
        if not full.is_absolute():
            if cwd is None:
                return self._leaf([path], HEAVY, f"script {path} (cwd unknown)", cwd)
            full = cwd / full
        text = _read_text(full)
        if text is None:
            return self._leaf([path], HEAVY, f"cannot read script {path}", cwd)
        cls, why, _ = self._script(text, run_cwd, depth + 1)
        return cls, f"{path}: {why}", cwd

    def _script(self, text: str, cwd: Path | None,
                depth: int) -> tuple[str, str, Path | None]:
        if depth > _MAX_DEPTH:
            return self._leaf([text[:80]], HEAVY, "scripts nested too deep", cwd)
        if "$(" in text or "`" in text:
            return self._leaf([text[:80]], HEAVY, "command substitution is not read", cwd)
        if "<<" in text.replace("<<<", ""):
            return self._leaf([text[:80]], HEAVY, "heredoc is not read", cwd)
        lex = shlex.shlex(_strip_comments(text).replace("\\\n", " "), posix=True,
                          punctuation_chars="();<>|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
        try:
            toks = [p for t in lex for p in _split_ops(t)]
        except ValueError:
            return self._leaf([text[:80]], HEAVY, "unparseable shell", cwd)
        steps: list[list[str]] = [[]]
        pending_redirect = False
        for t in toks:
            if pending_redirect:
                pending_redirect = False
                continue
            if t in _REDIRECTS:
                if steps[-1] and steps[-1][-1].isdigit():
                    steps[-1].pop()
                pending_redirect = True
                continue
            if t in _SEPARATORS:
                steps.append([])
                continue
            if t == "(" and any(w != "\0(" for w in steps[-1]):
                # `name ( )` is a function definition, `x=(a b)` an array: not read
                return self._leaf([text[:80]], HEAVY, "shell functions/arrays are not read", cwd)
            steps[-1].append("\0" + t if t in ("(", ")") else t)
        worst: tuple[str, str] = (LIGHT, "every command in the script is light")
        stack: list[Path | None] = []
        for raw in steps:
            words = [w for w in raw if w not in ("\0(", "\0)")]
            stack.extend([cwd] * raw.count("\0("))  # a subshell's cd ends with it
            while words and words[0] in _KEYWORDS_LEAD:
                words.pop(0)
            if words and not (len(words) == 1 and words[0] in _KEYWORDS_ALONE) and \
                    words[0] not in ("for", "select"):
                if words[0] in ("case", "function", "coproc", "eval"):
                    return self._leaf(words, HEAVY, f"`{words[0]}` is not read", cwd)
                seen = len(self.steps)
                cls, why, cwd = self.cmd(words, cwd, depth)
                leaves = self.steps[seen:]
                if not leaves or any(os.path.basename(s.argv[0]) not in _READONLY
                                     for s in leaves):
                    self.trust = False  # it may have created what a later check looks for
                if cls == HEAVY and worst[0] == LIGHT:
                    worst = (HEAVY, why)
            for _ in range(raw.count("\0)")):
                cwd = stack.pop() if stack else cwd
        return worst[0], worst[1], cwd

    # -- unknown programs -------------------------------------------------
    def _program(self, toks: list[str], cwd: Path | None,
                 depth: int) -> tuple[str, str, Path | None]:
        """An unlisted program: light only if it is a script we can read."""
        name = toks[0]
        if "/" in name:
            full = Path(os.path.expanduser(name))
            if not full.is_absolute():
                full = cwd / full if cwd is not None else None
        else:
            found = shutil.which(name, path=self.path) if self.path_ok else None
            full = Path(found) if found else None
        text = _read_text(full) if full is not None and full.is_file() else None
        if text is None or not text.startswith("#!"):
            return self._leaf(toks, HEAVY, f"{os.path.basename(name)} is not a known light"
                              " command", cwd)
        shebang = text.splitlines()[0][2:].split()
        interp = [os.path.basename(w) for w in shebang if not w.startswith("-")]
        if interp and interp[0] == "env":
            interp = interp[1:]
        if interp and interp[0] in _SHELLS:
            cls, why, _ = self._script(text, cwd, depth + 1)
            return self._leaf(toks, cls, f"{os.path.basename(name)}: {why}", cwd)
        if interp and (interp[0].startswith("python") or interp[:2] == ["uv", "run"]):
            cls, why = _py_text(text)
            return self._leaf(toks, cls, f"{os.path.basename(name)}: {why}", cwd)
        return self._leaf(toks, HEAVY, f"{os.path.basename(name)} is not a known light"
                          " command", cwd)


def _py_text(text: str) -> tuple[str, str]:
    m = _PY_HEAVY.search(text)
    if m:
        return HEAVY, f"python code uses {m.group(1)}"
    return LIGHT, "python code that starts no processes"


# -- per-tool rules: (walk, args, cwd, depth) -> (cls, why) ---------------
def _python(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "-W", "--check-hash-based-pycs"):
            i += 2
        elif a == "-m":
            mod = args[i + 1] if i + 1 < len(args) else ""
            if mod in _PY_LIGHT_MODULES:
                return LIGHT, f"python -m {mod}"
            return HEAVY, f"python -m {mod or '?'} is not a known light module"
        elif a == "-c":
            return _py_text(args[i + 1] if i + 1 < len(args) else "")
        elif a.startswith("-") and a != "-":
            i += 1
        else:
            break
    if i >= len(args) or args[i] == "-":
        return HEAVY, "python reading stdin"
    w.need("file", (args[i],), cwd, "python script")
    path = Path(os.path.expanduser(args[i]))
    if not path.is_absolute():
        if cwd is None:
            return HEAVY, "python script (cwd unknown)"
        path = cwd / path
    text = _read_text(path)
    if text is None:
        return HEAVY, f"cannot read {args[i]}"
    return _py_text(text)


def _cargo(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    args = [a for a in args if not a.startswith("+")]
    i = 0
    chdir = None
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in ("--list", "--explain"):
            return LIGHT, f"cargo {a}"
        if a == "-C":
            chdir = args[i + 1] if i + 1 < len(args) else None
        i += 2 if a in ("--color", "--config", "-Z", "-C") else 1
    sub = args[i] if i < len(args) else ""
    rest = args[i + 1:]
    if chdir is not None:
        w.need("dir", (chdir,), cwd, "cargo -C directory")
        cwd = cwd / chdir if cwd is not None else None
    if sub in _CARGO_LIGHT:
        return LIGHT, f"cargo {sub} does not compile"
    if sub == "insta" and rest[:1] and rest[0] in ("accept", "reject", "review", "pending-list"):
        return LIGHT, f"cargo insta {rest[0]}"
    manifest = _opt_value(rest, "--manifest-path")
    if manifest is not None:
        w.need("file", (manifest,), cwd, "--manifest-path")
    elif sub != "install" and cwd is not None:
        w.need("up", ("Cargo.toml",), cwd, "Cargo.toml (in the directory or above)")
    return HEAVY, f"cargo {sub or '(no subcommand)'} compiles"


_CARGO_LIGHT = frozenset("""
    update metadata fmt tree version search locate-project pkgid generate-lockfile fetch vendor
    add remove rm owner login logout yank verify-project read-manifest help init new deny audit
    machete outdated sort set-version upgrade info report config
""".split())


def _opt_value(args: list[str], *names: str) -> str | None:
    for i, a in enumerate(args):
        for n in names:
            if a == n and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(n + "=") and n.startswith("--"):
                return a.split("=", 1)[1]
    return None


def _docker(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    i = _skip_opts(args, {"--context": 1, "-c": 1, "-H": 1, "--host": 1, "--config": 1,
                          "-l": 1, "--log-level": 1})
    sub, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
    if sub == "image" and rest[:1] == ["build"]:
        sub, rest = "build", rest[1:]
    if sub == "builder" and rest[:1] == ["build"]:
        sub, rest = "build", rest[1:]
    if sub == "buildx":
        j = _skip_opts(rest, {"--builder": 1})
        sub2, rest = (rest[j], rest[j + 1:]) if j < len(rest) else ("", [])
        if sub2 in ("build", "b", "bake"):
            if any(a == "--print" or a.startswith("--list") or a.startswith("--call")
                   or a == "--check" for a in rest):
                return LIGHT, f"docker buildx {sub2} --print/--list only prints"
            _docker_files(w, rest, cwd, bake=sub2 == "bake")
            return HEAVY, f"docker buildx {sub2} builds images"
        if sub2 in ("ls", "inspect", "du", "version", "imagetools", "history", "use", ""):
            return LIGHT, f"docker buildx {sub2 or ''}".strip()
        return HEAVY, f"docker buildx {sub2} is not a known light subcommand"
    if sub == "build":
        _docker_files(w, rest, cwd, bake=False)
        return HEAVY, "docker build builds an image"
    if sub == "compose":
        j = _skip_opts(rest, {"-f": 1, "--file": 1, "-p": 1, "--project-name": 1,
                              "--profile": 1, "--env-file": 1, "--project-directory": 1,
                              "--progress": 1, "--ansi": 1, "--parallel": 1})
        sub2 = rest[j] if j < len(rest) else ""
        if sub2 in _COMPOSE_LIGHT:
            return LIGHT, f"docker compose {sub2}"
        _docker_files(w, rest[:j], cwd, bake=False, compose=True)
        return HEAVY, f"docker compose {sub2} may build or run images"
    if sub in _DOCKER_LIGHT:
        return LIGHT, f"docker {sub}"
    return HEAVY, f"docker {sub} is not a known light subcommand"


_DOCKER_LIGHT = frozenset("""
    ps images inspect logs version info tag push pull login logout stop kill rm volume network
    context system-df manifest cp top stats events port diff search container image
""".split())
_COMPOSE_LIGHT = frozenset("""
    config ps ls logs images top version port events down stop kill rm pull push restart pause
    unpause
""".split())


def _docker_files(w: _Walk, args: list[str], cwd: Path | None, *, bake: bool,
                  compose: bool = False) -> None:
    files = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-f", "--file")]
    files += [a.split("=", 1)[1] for a in args if a.startswith("--file=")]
    for f in files:
        if "://" not in f and f != "-":
            w.need("file", (f,), cwd, "-f/--file")
    projdir = _opt_value(args, "--project-directory")
    if projdir is not None:
        w.need("dir", (projdir,), cwd, "--project-directory")
    if files or projdir is not None or os.environ.get("BUILDX_BAKE_FILE"):
        return
    if bake and not any("://" in a or a.endswith(".git") for a in args):
        w.need("any", BAKE_FILES, cwd, "bake file (none given with -f)")
    elif compose and not os.environ.get("COMPOSE_FILE"):
        w.need("anyup", COMPOSE_FILES, cwd, "compose file (none given with -f)")


def _make(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    if any(a in ("-n", "--dry-run", "--just-print", "--recon", "-q", "--question",
                 ) for a in args):
        return LIGHT, "make dry run"
    chdir = _opt_value(args, "-C", "--directory")
    if chdir is not None:
        w.need("dir", (chdir,), cwd, "make -C directory")
        cwd = cwd / chdir if cwd is not None else None
    mfile = _opt_value(args, "-f", "--file", "--makefile")
    if mfile is not None:
        w.need("file", (mfile,), cwd, "make -f file")
    else:
        w.need("any", MAKE_FILES, cwd, "Makefile")
    return HEAVY, "make builds"


def _pkg_script(w: _Walk, tool: str, script: str, cwd: Path | None,
                depth: int) -> tuple[str, str]:
    """``bun run <script>``: classify the script's own command line."""
    if cwd is None:
        return HEAVY, f"{tool} run {script} (cwd unknown)"
    pkg = _find_up(cwd, ("package.json",))
    scripts: dict = {}
    if pkg is not None:
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            scripts = {}
    body = scripts.get(script) if isinstance(scripts, dict) else None
    if isinstance(body, str):
        saved_trust = w.trust
        cls, why, _ = w._script(body, pkg.parent if pkg else cwd, depth + 1)
        w.trust = saved_trust
        return cls, f"{tool} run {script}: {why}"
    if pkg is not None and not (cwd / script).exists() and not _find_up(
            cwd, (f"node_modules/.bin/{script}",)) and not shutil.which(script, path=w.path):
        w.need("script", (script,), pkg.parent, f"script in {pkg}")
    return HEAVY, f"{tool} run {script} is not a readable package script"


def _bun(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--cwd" and i + 1 < len(args):
            w.need("dir", (args[i + 1],), cwd, "--cwd directory")
            cwd = cwd / args[i + 1] if cwd is not None else None
            i += 2
        else:
            i += 1
    sub, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
    if sub in ("x",):
        cls, why, _ = w.cmd(rest, cwd, depth + 1)
        return cls, why
    if sub == "run":
        j = _skip_opts(rest, {"--filter": 1, "--cwd": 1})
        if j >= len(rest):
            return LIGHT, "bun run (lists scripts)"
        return _pkg_script(w, "bun", rest[j], cwd, depth)
    if sub == "pm":
        return (LIGHT, f"bun pm {rest[0]}") if rest[:1] and rest[0] in (
            "ls", "bin", "hash", "hash-string", "cache", "whoami", "version") else (
            HEAVY, "bun pm subcommand may build")
    if sub in ("outdated", "why", "info", "audit", ""):
        return LIGHT, f"bun {sub}".strip()
    if sub in ("test", "build", "install", "i", "add", "a", "remove", "rm", "update",
               "link", "unlink", "create", "init", "upgrade", "publish", "patch"):
        return HEAVY, f"bun {sub}"
    if sub.endswith((".ts", ".js", ".tsx", ".jsx", ".mjs", ".cjs", ".mts")):
        return HEAVY, f"bun runs {sub}"
    return _pkg_script(w, "bun", sub, cwd, depth)


def _npm(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    sub = args[0] if args else ""
    if sub in ("run", "run-script"):
        j = _skip_opts(args[1:], {})
        return _pkg_script(w, "npm", args[1 + j], cwd, depth) if 1 + j < len(args) else (
            LIGHT, "npm run (lists scripts)")
    if sub in ("ls", "list", "view", "info", "outdated", "why", "explain", "config", "bin"):
        return LIGHT, f"npm {sub}"
    return HEAVY, f"npm {sub} may install or build"


def _pnpm_yarn(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    sub = args[0] if args else ""
    if sub in ("exec", "dlx"):
        cls, why, _ = w.cmd(args[1:], cwd, depth + 1)
        return cls, why
    if sub == "run":
        return _pkg_script(w, "run", args[1], cwd, depth) if len(args) > 1 else (
            LIGHT, "run (lists scripts)")
    if sub in ("ls", "list", "why", "outdated", "info", "config", "bin"):
        return LIGHT, sub
    if sub in ("install", "i", "add", "remove", "update", "up", "test", "t", "build", ""):
        return HEAVY, f"{sub or 'install'} may install or build"
    return _pkg_script(w, "run", sub, cwd, depth)


def _uv(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    if sub == "run":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            a = rest[i]
            if a in ("-m", "--module"):
                return _python(w, rest[i:], cwd, depth)
            if a in ("--directory", "--project") and i + 1 < len(rest):
                w.need("dir", (rest[i + 1],), cwd, f"uv {a}")
                if a == "--directory" and cwd is not None:
                    cwd = cwd / rest[i + 1]
            i += 2 if a in _UV_RUN_ARITY else 1
        inner = rest[i:]
        if not inner:
            return HEAVY, "uv run with no command"
        if inner[0].endswith(".py"):
            return _python(w, inner, cwd, depth)
        cls, why, _ = w.cmd(inner, cwd, depth + 1)  # the venv's bin is not on PATH yet
        return cls, why
    if sub == "tool" and rest[:1] == ["run"]:
        cls, why, _ = w.cmd(rest[1:], cwd, depth + 1)
        return cls, why
    if sub in ("lock", "tree", "version", "venv", "cache", "self"):
        return LIGHT, f"uv {sub}"
    if sub == "python" and rest[:1] and rest[0] in ("list", "find", "dir"):
        return LIGHT, f"uv python {rest[0]}"
    if sub == "pip" and rest[:1] and rest[0] in ("list", "show", "freeze", "check", "tree"):
        return LIGHT, f"uv pip {rest[0]}"
    return HEAVY, f"uv {sub} may install or build"


_UV_RUN_ARITY = frozenset({"--with", "--with-editable", "--with-requirements", "--python", "-p",
                           "--project", "--directory", "--package", "--extra", "--group",
                           "--only-group", "--env-file", "--index", "--index-url",
                           "--extra-index-url", "--find-links"})


def _uvx(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    i = _skip_opts(args, {"--from": 1, "--with": 1, "--python": 1, "-p": 1})
    cls, why, _ = w.cmd(args[i:], cwd, depth + 1)
    return cls, why


def _go(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    sub = args[0] if args else ""
    if sub in ("version", "env", "list", "fmt", "doc", "help", "mod", "work", "clean"):
        return LIGHT, f"go {sub}"
    return HEAVY, f"go {sub} compiles"


def _git(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    i = _skip_opts(args, {"-C": 1, "-c": 1, "--git-dir": 1, "--work-tree": 1})
    if args[i:i + 2] == ["bisect", "run"]:
        cls, why, _ = w.cmd(args[i + 2:], cwd, depth + 1)
        return cls, f"git bisect run: {why}"
    return LIGHT, "git does not compile"


def _rustup(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    sub = args[0] if args else ""
    if sub == "run" and len(args) > 2:
        cls, why, _ = w.cmd(args[2:], cwd, depth + 1)
        return cls, why
    if sub in ("show", "which", "help") or args[:2] in (["component", "list"],
                                                         ["toolchain", "list"],
                                                         ["target", "list"]):
        return LIGHT, f"rustup {sub}"
    return HEAVY, f"rustup {sub} changes a toolchain a build may be using"


def _biome(w: _Walk, args: list[str], cwd: Path | None, depth: int) -> tuple[str, str]:
    return LIGHT, "biome formats/lints without compiling"


_TOOLS = {
    "cargo": _cargo, "docker": _docker, "make": _make, "gmake": _make, "bun": _bun,
    "npm": _npm, "pnpm": _pnpm_yarn, "yarn": _pnpm_yarn, "uv": _uv, "uvx": _uvx, "go": _go,
    "git": _git, "rustup": _rustup, "biome": _biome,
    **{p: _python for p in ("python", "python3", "python3.10", "python3.11", "python3.12",
                            "python3.13", "python3.14")},
}


def classify(argv: list[str], cwd: str | Path | None, heavy: list[str] | None = None,
             light: list[str] | None = None, path: str | None = None) -> Verdict:
    """Classify ``argv`` (run in ``cwd``) as heavy or light, with its pre-flight
    requirements. ``heavy``/``light`` are the ``[build]`` patterns."""
    walk = _Walk(list(heavy or []), list(light or []), path)
    base = Path(cwd) if cwd is not None else None
    cls, why = walk.argv(list(argv), base)
    return Verdict(cls, why, walk.steps, walk.reqs)


def preflight(verdict: Verdict, path: str | None = None) -> str | None:
    """The first requirement that is not met, as a message; ``None`` if all are."""
    search = path if path is not None else os.environ.get("PATH", os.defpath)
    for req in verdict.reqs:
        base = Path(req.base)
        first = os.path.expanduser(req.paths[0])
        full = base / first
        if req.kind == "exe":
            if "/" in first:
                if not (os.path.isfile(full) and os.access(full, os.X_OK)):
                    return f"cannot run {first!r}: no such executable (from {base})"
            elif shutil.which(first, path=search) is None:
                return f"cannot run {first!r}: not found on PATH"
        elif req.kind == "dir" and not full.is_dir():
            return f"{req.what} {first!r} does not exist (from {base})"
        elif req.kind == "file" and not full.exists():
            return f"{req.what} {first!r} does not exist (from {base})"
        elif req.kind == "up" and _find_up(base, req.paths) is None:
            return f"no {req.what} in {base}"
        elif req.kind == "any" and not any((base / p).exists() for p in req.paths):
            return f"no {req.what} in {base} (looked for {', '.join(req.paths)})"
        elif req.kind == "anyup" and _find_up(base, req.paths) is None:
            return f"no {req.what} in {base} or above"
        elif req.kind == "script":
            return f"no script {first!r} in {req.what}, and no such file or program"
    return None
