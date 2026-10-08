#!/usr/bin/env python3
"""crew: hand bounded tasks to other vendors' coding-agent CLIs.

Backends: OpenAI Codex CLI (`codex`), Anthropic Claude Code (`claude`) and
Google Antigravity CLI (`agy`). The caller chooses each task's permission scope
(--access, --web, --network); crew maps it onto the backend's own controls,
runs the agent in its own process (optionally in a visible terminal tab) and
leaves a record of the run on disk.

Standard library only, Python 3.9+. Start with `crew.py doctor`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_DIR / "assets" / "config.default.json"
CREW_HOME = Path(os.environ.get("CREW_HOME") or (Path.home() / ".crew")).expanduser()
USER_CONFIG = Path(os.environ.get("CREW_CONFIG") or (CREW_HOME / "config.json")).expanduser()
RUNS_DIR = CREW_HOME / "runs"
LEDGER = CREW_HOME / "ledger.jsonl"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
BACKENDS = ("codex", "claude", "agy")
ACCESS_LEVELS = ("read", "verify", "write")
HEARTBEAT_SEC = 5
STALE_SEC = 30
STARTUP_GRACE_SEC = 90
TAB_CLAIM_SEC = 15
HASH_LIMIT = 64 * 1024 * 1024
AGY_INLINE_LIMIT = 8000
REVIEW_EMBED_LIMIT = 200_000
AGY_FETCH_RULE = "read_url(*)"
CLAUDE_ALIASES = ("fable", "opus", "sonnet", "haiku")

# Set in every delegated agent's environment; crew refuses to dispatch when it sees it.
NESTED_MARKER = "CREW_TASK_DIR"
# Environment markers that identify the calling agent. They are removed from the delegated
# agent's environment so it doesn't think it is nested inside its caller.
CALLER_MARKERS = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
    "codex": ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED"),
}


class CrewError(Exception):
    """A usage or configuration problem, reported without a traceback."""


# ----------------------------------------------------------------- utilities

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data) -> None:
    """Write atomically so a reader never sees a half-written file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the target open
            if attempt == 49:
                raise
            time.sleep(0.1)


def append_line(path, line: str) -> None:
    for _ in range(50):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return
        except PermissionError:  # parallel runners appending at once (Windows)
            time.sleep(0.1)


def short(text, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= n else text[: n - 3] + "..."


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def use_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def on_off(flag) -> str:
    return "on" if flag else "off"


# -------------------------------------------------------------------- config

def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_user_config() -> dict:
    if not USER_CONFIG.exists():
        return {}
    try:
        return read_json(USER_CONFIG)
    except ValueError as e:
        raise CrewError(f"{USER_CONFIG} is not valid JSON: {e}")


def load_config() -> dict:
    return deep_merge(read_json(DEFAULT_CONFIG), load_user_config())


def save_user_config(data: dict) -> None:
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    write_json(USER_CONFIG, data)


# ------------------------------------------------------------ agents / models

SPEC_RE = re.compile(r"^(codex|claude|agy):([^@\s]+?)(?:@([a-z]+))?$")


def parse_spec(text):
    m = SPEC_RE.match(str(text).strip())
    return {"backend": m[1], "model": m[2], "effort": m[3]} if m else None


def spec_str(spec: dict) -> str:
    return f"{spec['backend']}:{spec['model']}" + (f"@{spec['effort']}" if spec.get("effort") else "")


def expand_agent(cfg: dict, ref, _seen=()) -> list:
    """Agent reference -> candidate specs in fallback order.

    A reference is a spec ('codex:gpt-6.1-sol@high'), an alias from
    cfg['agents'], or a list of either."""
    if isinstance(ref, list):
        return [s for r in ref for s in expand_agent(cfg, r, _seen)]
    spec = parse_spec(ref)
    if spec:
        return [spec]
    agents = cfg.get("agents") or {}
    if ref in agents and ref not in _seen:
        return expand_agent(cfg, agents[ref], _seen + (ref,))
    raise CrewError(
        f"Unknown agent '{ref}'. Use backend:model[@effort] (backend: {', '.join(BACKENDS)}) "
        f"or an alias: {', '.join(agents) or '(none defined)'}")


def backend_cmd(cfg: dict, backend: str):
    """argv prefix that launches a backend, or None if it is not installed."""
    exe = ((cfg.get("backends") or {}).get(backend) or {}).get("exe")
    if isinstance(exe, list):
        return list(exe)
    name = os.path.expanduser(exe) if exe else backend
    found = name if Path(name).is_file() else shutil.which(name)
    return [found] if found else None


def detect_caller():
    """Which agent is running crew: CREW_CALLER, else environment markers, else None."""
    explicit = os.environ.get("CREW_CALLER", "").strip().lower()
    if explicit:
        return explicit if explicit in BACKENDS else None
    for backend, markers in CALLER_MARKERS.items():
        if any(os.environ.get(m) for m in markers):
            return backend
    if any(os.environ.get(m) for m in ("CODEX_MANAGED_BY_NPM", "CODEX_MANAGED_BY_BUN", "CODEX_MANAGED_BY_PNPM")):
        return "codex"
    return None


def caller_session():
    """The calling agent's own session id, so a run can be traced back to the conversation that
    dispatched it. Read here only; it is still removed from the delegated agent's environment."""
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        if os.environ.get(var):
            return os.environ[var]
    return None


def choose_agent(cfg: dict, ref, caller=None, explicit=False, balance=None, quota=None):
    """First eligible candidate. Returns (spec, argv prefix, note or None)."""
    return candidates_for(cfg, ref, caller, bool(balance), quota, explicit)[0]


def candidates_for(cfg: dict, ref, caller=None, balance=False, quota=None, explicit=False) -> list:
    """Eligible candidates for a role, best first, as (spec, argv prefix, note or None).

    Rules, in order: an explicitly named agent is honoured as is; candidates from the caller's
    model family are dropped (an agy Claude model is Claude, whatever the backend); candidates
    whose backend is near its usage limit are dropped; with `balance`, the rest are ordered by
    remaining headroom, keeping the configured order among similar ones. Each filter is skipped
    rather than leaving no candidate at all."""
    candidates = expand_agent(cfg, ref)
    installed = [(s, c) for s, c in ((s, backend_cmd(cfg, s["backend"])) for s in candidates) if c]
    if not installed:
        raise CrewError(
            f"No installed backend for agent '{ref}' (tried {', '.join(map(spec_str, candidates))}). "
            "Run `crew.py doctor`.")
    if explicit:
        return [(installed[0][0], installed[0][1], None)]
    notes = []
    family = CALLER_FAMILY.get(caller)
    if family:
        others = [(s, c) for s, c in installed if model_family(s) != family]
        if others:
            installed = others
        else:
            notes.append(f"{family} is also the caller's model family; no other installed candidate for this role")
    if quota is None:
        quota = read_quota()
    ready = [(s, c) for s, c in installed if not quota_exhausted(_quota_entry(quota, s["backend"]))]
    if ready:
        installed = ready
    else:
        notes.append("all candidates are near their usage limit")
    if balance:
        step = float(cfg.get("balance_step", 0.25))
        unknown = float(cfg.get("unknown_quota", 0.5))

        def bucket(item):
            score = quota_score(_quota_entry(quota, item[0]["backend"]))
            return (unknown if score is None else score) // step

        installed = sorted(installed, key=bucket)
    note = "; ".join(notes) or None
    return [(s, c, note) for s, c in installed]


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def codex_models():
    """Models listed in codex's local cache, or None if the cache is missing."""
    try:
        data = read_json(codex_home() / "models_cache.json")
    except (OSError, ValueError):
        return None
    return [m for m in data.get("models") or [] if m.get("visibility", "list") == "list"]


def agy_models(cfg: dict):
    cmd = backend_cmd(cfg, "agy")
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd + ["models"], capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and re.match(r"^[\w.\-]+$", parts[0].strip()):
            out.append({"slug": parts[0].strip(), "description": parts[1].strip()})
    return out


_help_cache = {}


def cli_help(cmd: list) -> str:
    key = tuple(cmd)
    if key not in _help_cache:
        try:
            r = subprocess.run(cmd + ["--help"], capture_output=True, timeout=60)
            _help_cache[key] = (r.stdout + r.stderr).decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            _help_cache[key] = ""
    return _help_cache[key]


# ---------------------------------------------------------------------- quota

# Model families. agy runs Claude, GPT and Gemini models, so its family comes from the model
# name; a Claude Code caller should not count an agy Claude model as a second vendor.
CALLER_FAMILY = {"codex": "openai", "claude": "anthropic", "agy": "google"}
QUOTA_FILE = CREW_HOME / "quota.json"
CODEX_DAY_LIMIT = 400  # newest session-log day folders searched for a task's session
CODEX_WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}
LIMIT_RE = re.compile(
    r"hit your (?:\w+ )?usage limit|usage limit (?:reached|exceeded)"
    r"|\brate[ _-]?limit(?:ed)?\b|rate_limit_error|too many requests|quota exceeded|resource[ _]exhausted"
    r"|\b(?:status|http|error|code)\W{0,3}429\b|\b429\b\s*(?:too many|rate|quota)",
    re.IGNORECASE)


def model_family(spec: dict) -> str:
    if spec["backend"] == "agy":
        model = spec["model"].lower()
        if model.startswith("claude"):
            return "anthropic"
        if model.startswith("gpt"):
            return "openai"
        return "google"
    return {"codex": "openai", "claude": "anthropic"}.get(spec["backend"], spec["backend"])


def read_quota() -> dict:
    try:
        data = read_json(QUOTA_FILE)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _quota_entry(quota: dict, backend: str) -> dict:
    entry = quota.get(backend)
    return entry if isinstance(entry, dict) else {}


def _save_quota(quota: dict) -> None:
    try:
        QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json(QUOTA_FILE, quota)
    except OSError:  # quota is advisory; a failed write must not fail a run
        pass


def record_quota(backend: str, windows: dict, source: str) -> None:
    """Replace a backend's usage windows, keeping any limit already recorded for it."""
    quota = read_quota()
    entry = _quota_entry(quota, backend)
    entry.setdefault("limited_until", None)
    entry.update({"windows": windows, "updated": now_iso(), "source": source})
    quota[backend] = entry
    _save_quota(quota)


def record_limit(backend: str, resets_at=None, cooldown_minutes: float = 60) -> None:
    """The backend hit its limit: blocked until the reset time if known, else for a cooldown."""
    now = time.time()
    until = resets_at if resets_at and resets_at > now else now + cooldown_minutes * 60
    quota = read_quota()
    entry = _quota_entry(quota, backend)
    entry.setdefault("windows", {})
    entry.setdefault("source", "limit")
    entry.update({"limited_until": until, "updated": now_iso()})
    quota[backend] = entry
    _save_quota(quota)


def quota_score(entry, now=None):
    """How full a backend is (0-1) from the windows still in effect; None if nothing is known."""
    if not isinstance(entry, dict):
        return None
    now = time.time() if now is None else now
    windows = entry.get("windows")
    windows = windows if isinstance(windows, dict) else {}
    limited = entry.get("limited_until")
    if not windows and limited is None:
        return None
    scores = [0.0]  # a window whose reset time has passed no longer counts
    for w in windows.values():
        if isinstance(w, dict) and (w.get("resets_at") is None or w["resets_at"] > now):
            scores.append(float(w.get("used") or 0))
    if limited is not None and limited > now:
        scores.append(1.0)
    return max(scores)


def quota_exhausted(entry, now=None, threshold: float = 0.95) -> bool:
    score = quota_score(entry, now)
    return score is not None and score >= threshold


def claude_quota_windows(event: dict):
    """Usage windows from a Claude Code stream-json rate_limit_event, or None."""
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    info = info if isinstance(info, dict) else {}
    unified = info.get("unifiedWindows")
    unified = unified if isinstance(unified, dict) else {}
    windows = {name: {"used": float(w.get("utilization") or 0), "resets_at": w.get("resetsAt")}
               for name, w in unified.items() if isinstance(w, dict)}
    if windows:
        return windows
    if info.get("status", "allowed") != "allowed":
        return {"limit": {"used": 1.0, "resets_at": info.get("resetsAt")}}
    return None


def _find_key(obj, key: str):
    """First non-empty dict stored under `key` anywhere inside a parsed JSON value."""
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, dict) and value:
            return value
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find_key(child, key)
        if found:
            return found
    return None


def _codex_session_log(base: Path, session_id: str):
    suffix = f"-{session_id}.jsonl"
    days = sorted((p for p in base.glob("*/*/*") if p.is_dir()), reverse=True)[:CODEX_DAY_LIMIT]
    for day in days:
        for p in day.iterdir():
            if p.name.startswith("rollout-") and p.name.endswith(suffix):
                return p
    return None


def codex_quota_windows(session_id: str, sessions_dir=None):
    """Latest rate limits Codex wrote into a task's session log, or None. Never raises."""
    if not session_id:
        return None
    try:
        base = Path(sessions_dir) if sessions_dir else codex_home() / "sessions"
        log = _codex_session_log(base, session_id)
        if log is None:
            return None
        found = None
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"rate_limits"' not in line:
                    continue
                try:
                    rl = _find_key(json.loads(line), "rate_limits")
                except ValueError:
                    continue
                windows = {}
                for w in (rl or {}).values():
                    if isinstance(w, dict) and w.get("used_percent") is not None:
                        name = CODEX_WINDOW_NAMES.get(w.get("window_minutes"), f"{w.get('window_minutes')}_min")
                        windows[name] = {"used": float(w["used_percent"]) / 100, "resets_at": w.get("resets_at")}
                if windows:
                    found = windows
        return found
    except (OSError, ValueError):
        return None


def looks_rate_limited(text: str) -> bool:
    return bool(LIMIT_RE.search(text or ""))


def limit_reset_from_text(text: str):
    """Epoch reset time from text such as 'usage limit reached|1791458400', else None."""
    m = re.search(r"\|\s*(\d{10})\b", text or "")
    return float(m[1]) if m else None


# --------------------------------------------------------------------- scope

def agy_settings_paths(cfg: dict) -> list:
    configured = ((cfg.get("backends") or {}).get("agy") or {}).get("settings")
    paths = [Path(os.path.expanduser(configured))] if configured else []
    paths.append(Path.home() / ".gemini" / "config" / "config.json")  # agy's shared config
    return paths


def agy_fetch_allowed(cfg: dict) -> bool:
    for p in agy_settings_paths(cfg):
        try:
            allow = (read_json(p).get("permissions") or {}).get("allow") or []
        except (OSError, ValueError, AttributeError):
            continue
        if AGY_FETCH_RULE in allow:
            return True
    return False


def claude_has_sandbox() -> bool:
    # Claude Code's shell sandbox exists on macOS, Linux and WSL2, not on native Windows.
    return not IS_WIN


def enforcement(backend: str, scope: dict, has_git: bool, agy_fetch: bool) -> dict:
    """How each scope dimension is enforced. Raises for scopes a backend cannot honour."""
    access, web, net = scope["access"], scope["web"], scope["network"]
    detect = "git before/after check" if has_git else "nothing (not a git repo, so changes cannot be detected)"
    if backend == "codex":
        files = {"read": "codex read-only sandbox",
                 "verify": f"codex workspace-write sandbox + instructions + {detect}",
                 "write": "codex workspace-write sandbox"}[access]
        network = ("n/a (read-only sandbox has no network)" if access == "read"
                   else f"codex sandbox network {on_off(net)}")
        return {"files": files, "web": f"codex web_search={'live' if web else 'disabled'}", "network": network}
    if backend == "claude":
        sandboxed = claude_has_sandbox()
        shell = ("shell in claude's sandbox" if sandboxed
                 else f"shell unsandboxed (no claude sandbox on Windows): instructions + {detect}")
        files = {"read": "claude tools limited to Read, Glob, Grep",
                 "verify": f"no Edit/Write tools; {shell}" + (", repository read-only, writes only to scratch" if sandboxed else ""),
                 "write": f"Edit/Write tools; {shell}"}[access]
        if access == "read":
            network = "n/a (no shell)"
        elif sandboxed:
            network = f"claude sandbox network {on_off(net)}"
        else:
            network = "not enforced (no claude sandbox on Windows): instructions only"
        return {"files": files, "web": f"claude WebSearch/WebFetch tools {'included' if web else 'removed'}",
                "network": network}
    if access == "verify":
        raise CrewError("agy cannot run commands in headless mode, so it cannot build or test. "
                        "Use a codex or claude agent for --access verify, or use --access read.")
    files = {"read": f"instructions + {detect} (agy has no read-only switch)",
             "write": "agy accept-edits mode (terminal commands stay denied)"}[access]
    if web:
        webe = "search allowed; page fetch " + ("allowed by agy settings" if agy_fetch
                                                 else f"NOT allowed (no {AGY_FETCH_RULE} rule; see `crew.py doctor`)")
    else:
        webe = "instructions only (agy cannot switch search off)"
    return {"files": files, "web": webe, "network": "n/a (agy runs no terminal commands)"}


def scope_preamble(backend: str, scope: dict, scratch: Path, top, agy_fetch: bool) -> str:
    access, web, net = scope["access"], scope["web"], scope["network"]
    lines = ["[crew task scope]"]
    if access == "read":
        lines.append("Read-only: do not create, modify or delete any file. Put everything in your answer.")
    elif access == "verify":
        repo = f" ({top})" if top else ""
        lines.append(
            f"You may build and run tests. Write scratch files only under {scratch} (TMP and TEMP point there). "
            f"Do not create, modify or delete files in the repository{repo} other than gitignored build output. "
            "Changes are detected and reported as violations.")
    else:
        lines.append("You may edit files in the working directory as the task requires. Keep changes within the task.")
        lines.append("Before reporting the work done, run the checks the task names (or the project's tests, build "
                     "or type-check) and report their results. If a check could not run, say which and why.")
    if access != "read":
        lines.append("Do not run git add, commit, push, checkout, reset, stash or clean; leave changes uncommitted.")
    if backend in ("codex", "claude") and access != "read":
        lines.append("Network access for commands: " + ("available." if net else "not available; do not try to install packages."))
    if backend == "agy":
        lines.append("Do not run terminal commands; they are denied in this headless mode.")
    if web:
        if backend == "agy" and not agy_fetch:
            lines.append("You may use web search, but page fetching is not permitted here: work from search results only.")
        else:
            lines.append("You may use web search and read web pages.")
        lines.append(f"Today's date is {dt.date.today().isoformat()}. Versions, prices and anything \"latest\" may "
                     "have changed since your training data, so search for them rather than answering from memory.")
        lines.append("Cite the URLs you rely on.")
    else:
        lines.append("Do not use web search or fetch URLs.")
    return "\n".join(lines)


# ---------------------------------------------------------------------- git

def git_top(cwd):
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return str(Path(out)) if r.returncode == 0 and out else None


def file_sig(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "gone"
    if not path.is_file():
        return "dir"
    if st.st_size > HASH_LIMIT:
        return f"size:{st.st_size}:mtime:{st.st_mtime_ns}"
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_state(top: str) -> dict:
    """Dirty and untracked files (paths relative to the repo root) with a content hash,
    so edits to an already-dirty file are still detected. Gitignored files are invisible."""
    r = subprocess.run(["git", "-C", top, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                       capture_output=True, timeout=600)
    if r.returncode != 0:
        return {}
    parts = r.stdout.split(b"\0")
    state, i = {}, 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        path = entry[3:].decode("utf-8", "surrogateescape")
        if entry[:1] in (b"R", b"C"):
            i += 1  # -z puts the rename/copy source in the next field
        state[path] = file_sig(Path(top) / path)
    return state


def changed_paths(before: dict, after: dict) -> list:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def git_text(top: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", top, *args], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise CrewError(f"git {' '.join(args)} failed: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode("utf-8", "replace")


# ------------------------------------------------------------------- review

def parse_target(target: str):
    if target == "uncommitted":
        return ("uncommitted", None)
    for prefix in ("commit:", "base:"):
        if target.startswith(prefix) and len(target) > len(prefix):
            return (prefix[:-1], target[len(prefix):])
    raise CrewError(f"Bad --target '{target}' (uncommitted | commit:<sha> | base:<branch>)")


def review_diff_text(top: str, target: str) -> str:
    kind, ref = parse_target(target)
    if kind == "commit":
        return git_text(top, "show", ref)
    if kind == "base":
        return git_text(top, "diff", f"{ref}...HEAD")
    text = "$ git status --short\n" + git_text(top, "status", "--short") + "\n$ git diff HEAD\n" + git_text(top, "diff", "HEAD")
    for rel in git_text(top, "ls-files", "--others", "--exclude-standard").splitlines():
        try:
            content = (Path(top) / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = "(unreadable or binary)"
        text += f"\n--- untracked file: {rel}\n{content}"
    return text


def review_task(extra: str, target: str, top: str, embed: bool, access: str) -> str:
    kind, ref = parse_target(target)
    if embed:
        diff = review_diff_text(top, target)
        if len(diff) > REVIEW_EMBED_LIMIT:
            raise CrewError(f"The diff is {len(diff)} characters, too large to embed. "
                            "Use an agent that can run git (codex, or claude with --access verify).")
        where = f"The change under review is below.\n<diff>\n{diff}\n</diff>"
    else:
        how = {"uncommitted": "git status --short, git diff HEAD, and read any untracked files",
               "commit": f"git show {ref}",
               "base": f"git diff {ref}...HEAD"}[kind]
        where = f"Inspect the change under review with: {how} (read surrounding code as needed)."
    act = ("Build and run tests where that confirms or refutes a finding." if access == "verify"
           else "Do not modify any files.")
    return (f"Review this code change as a strict senior reviewer. {act}\n{where}\n"
            "Follow the repository's own conventions (AGENTS.md, CLAUDE.md, CONTRIBUTING.md) where present.\n\n"
            + (f"{extra}\n\n" if extra else "")
            + "Report each problem with: severity (high/medium/low), file:line, what is wrong, a concrete way it "
              "fails, and a fix. Most severe first. If nothing needs fixing, say so plainly.")


# --------------------------------------------------------------- backend argv

def toml_str(value) -> str:
    return json.dumps(str(value))  # a JSON string is a valid TOML basic string


def codex_windows_sandbox():
    """[windows] sandbox from the user's codex config, restated when that config is skipped."""
    if not IS_WIN:
        return None
    try:
        text = (codex_home() / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'(?ms)^\[windows\][^\[]*?^\s*sandbox\s*=\s*"([^"]+)"', text)
    return m[1] if m else None


def codex_args(cfg, spec, effort, scope, task: Path, cwd: Path, mode: str, target=None, session=None) -> list:
    opts = ["--json", "--skip-git-repo-check", "-m", spec["model"], "-o", str(task / "final.md")]
    if ((cfg.get("backends") or {}).get("codex") or {}).get("ignore_user_config"):
        opts.append("--ignore-user-config")
        ws = codex_windows_sandbox()
        if ws:
            opts += ["-c", f"windows.sandbox={toml_str(ws)}"]
    if effort:
        opts += ["-c", f"model_reasoning_effort={toml_str(effort)}"]
    opts += ["-c", f"web_search={toml_str('live' if scope['web'] else 'disabled')}"]
    if mode == "resume":  # resume inherits the original session's sandbox and root
        return ["exec", "resume"] + opts + [session, "-"]
    sandbox = "read-only" if scope["access"] == "read" else "workspace-write"
    if sandbox == "workspace-write":
        opts += ["-c", f"sandbox_workspace_write.network_access={'true' if scope['network'] else 'false'}",
                 "-c", f"sandbox_workspace_write.writable_roots=[{toml_str(task / 'scratch')}]"]
    if mode == "builtin-review":  # `exec review` has no -s or -C: sandbox via config, cwd via the process
        kind, ref = parse_target(target)
        flags = {"uncommitted": ["--uncommitted"], "commit": ["--commit", ref or ""], "base": ["--base", ref or ""]}[kind]
        return ["exec", "review"] + opts + ["-c", f"sandbox_mode={toml_str(sandbox)}"] + flags
    return ["exec"] + opts + ["-s", sandbox, "-C", str(cwd), "-"]


def claude_args(cmd, spec, effort, scope, task: Path, top, cwd: Path, role_cfg: dict, session=None) -> list:
    """Scope -> an explicit tool list plus a per-task settings file (permissions and sandbox)."""
    access, web, net = scope["access"], scope["web"], scope["network"]
    scratch = task / "scratch"
    shell = ["Bash", "PowerShell"] if IS_WIN else ["Bash"]
    tools = ["Read", "Glob", "Grep"]
    if access != "read":
        tools += shell
    if access == "write":
        tools += ["Edit", "Write"]
    web_tools = ["WebSearch", "WebFetch"] if web else []
    tools += web_tools
    allow = list(web_tools)
    settings = {"permissions": {"allow": allow}}
    if access != "read":
        if claude_has_sandbox():
            fs = {"allowWrite": [str(scratch)]}
            if access == "verify":
                fs["denyWrite"] = [str(top or cwd)]
            domains = role_cfg.get("network_domains") or ["*"]
            settings["sandbox"] = {
                "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True, "filesystem": fs,
                "network": {"allowedDomains": domains if net else [], "strictAllowlist": True}}
        else:
            allow += shell  # no sandbox to auto-approve commands; the git check is the backstop
    settings_path = task / "claude-settings.json"
    write_json(settings_path, settings)
    help_text = cli_help(cmd)
    args = ["-p", "--output-format", "stream-json", "--verbose", "--model", spec["model"]]
    if effort:
        args += ["--effort", effort]
    if "--restricted" in help_text:
        args.append("--restricted")  # ignore the user's settings files: the scope below is the whole policy
    args += ["--tools", ",".join(tools),
             "--permission-mode", "acceptEdits" if access == "write" else "dontAsk"]
    if "--permission-prompts" in help_text:
        args += ["--permission-prompts", "none"]
    args += ["--strict-mcp-config", "--disable-slash-commands", "--settings", str(settings_path),
             "--add-dir", str(scratch)]
    if session:
        args += ["--resume", session]
    return args


def agy_args(spec, effort, scope, task: Path, cwd: Path, text: str) -> list:
    args = ["--model", spec["model"]]  # --model must come before -p
    if effort:
        args += ["--effort", effort]
    args += ["--add-dir", str(cwd)]
    if scope["access"] == "write":
        args += ["--mode", "accept-edits"]
    prompt = text
    if len(text) > AGY_INLINE_LIMIT:  # keep well under the Windows command-line limit
        args += ["--add-dir", str(task)]
        prompt = (f"Your full instructions are in the file {task / 'task.md'} . "
                  "Read that file completely and follow it exactly.")
    return args + ["-p", prompt]


# --------------------------------------------------------------- launching

def windows_service_session() -> bool:
    """True in Windows session 0 (services, some remote or SSH setups), where no window can
    reach the user's desktop: wt then reports success but nothing opens."""
    if not IS_WIN:
        return False
    try:
        import ctypes
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return sid.value == 0
    except Exception:
        pass
    return False


def launch_tab(argv: list, title: str, cwd: Path, cfg: dict):
    """Open the runner in a visible terminal. Returns a description, or None if unavailable."""
    custom = cfg.get("terminal")
    if custom:
        cmdline = subprocess.list2cmdline(argv) if IS_WIN else shlex.join(argv)
        expanded = []
        for part in custom:
            if part == "{argv}":
                expanded += argv
            else:
                expanded.append(part.replace("{title}", title).replace("{cmd}", cmdline))
        subprocess.Popen(expanded, cwd=str(cwd))
        return f"custom terminal '{title}'"
    if IS_WIN:
        wt = shutil.which("wt")
        # wt reads ';' as its own command separator, so any argument containing one would be shredded.
        if not wt or any(";" in a for a in argv) or windows_service_session():
            return None
        subprocess.Popen([wt, "-w", "crew", "new-tab", "--title", title, "--suppressApplicationTitle",
                          "-d", str(cwd)] + argv)
        return f"Windows Terminal tab '{title}' (window 'crew')"
    if IS_MAC and shutil.which("osascript"):
        script = shlex.join(argv).replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Terminal.app window"
    if os.environ.get("TMUX") and shutil.which("tmux"):
        subprocess.Popen(["tmux", "new-window", "-d", "-n", title, shlex.join(argv)])
        return f"tmux window '{title}'"
    return None


def launch_hidden(argv: list, cwd: Path) -> None:
    """Start the runner detached, so it survives the caller's shell being killed."""
    kw = dict(cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    if not IS_WIN:
        subprocess.Popen(argv, start_new_session=True, **kw)
        return
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:
        # The caller's job object forbids breakaway. Fall back to WMI, which creates the
        # process outside the caller's process tree.
        if not launch_wmi(argv, cwd):
            subprocess.Popen(argv, creationflags=flags, **kw)


def launch_wmi(argv: list, cwd: Path) -> bool:
    """A WMI-created process does not inherit this process's environment, so it is passed
    explicitly (on stdin, not the command line, so secrets never appear in process listings)."""
    ps = shutil.which("pwsh") or shutil.which("powershell")
    if not ps:
        return False
    cmdline = subprocess.list2cmdline(argv).replace("'", "''")
    script = ("$vars = [string[]]([Console]::In.ReadToEnd() | ConvertFrom-Json); "
              "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly "
              "-Property @{ShowWindow=[uint16]0; EnvironmentVariables=$vars}; "
              f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{cmdline}'; "
              f"CurrentDirectory='{str(cwd).replace(chr(39), chr(39) * 2)}'; ProcessStartupInformation=$si}}; exit $r.ReturnValue")
    env_list = json.dumps([f"{k}={v}" for k, v in os.environ.items()])
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                           input=env_list.encode("utf-8"), capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# ----------------------------------------------------------- liveness / wait

def claim_task(task: Path) -> bool:
    """Exactly one runner may run a task: the first to create 'claimed' wins."""
    try:
        fd = os.open(str(task / "claimed"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def wait_for_claim(task: Path, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if (task / "claimed").exists():
            return True
        time.sleep(0.25)
    return (task / "claimed").exists()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def runner_state(task: Path) -> str:
    """'alive', 'dead' (runner gone without a result) or 'unknown' (not started yet)."""
    try:
        info = read_json(task / "run.json")
    except (OSError, ValueError):
        return "unknown"
    if time.time() - float(info.get("beat", 0)) < STALE_SEC:
        return "alive"
    # A stale heartbeat can also mean the machine slept; trust the process table then.
    return "alive" if pid_alive(int(info.get("pid", 0))) else "dead"


def write_harness_result(task: Path, note: str, cmd=None, seconds=None) -> None:
    try:
        append_line(task / "raw.log", f"crew harness error: {note}")
    except OSError:
        pass
    if cmd is None:
        try:
            cmd = read_json(task / "cmd.json")
        except (OSError, ValueError):
            cmd = {}
    result = {"status": "harness-error", "note": note, "exit": None, "role": cmd.get("role"),
              "agent": cmd.get("agent"), "backend": cmd.get("backend"), "model": cmd.get("model"),
              "scope": cmd.get("scope"), "seconds": seconds, "tokens": None, "cost_usd": None,
              "session_id": None, "touched_files": [], "violations": [], "denied_tools": [],
              "final": str(task / "final.md"), "finished": now_iso()}
    write_json(task / "result.json", result)


def wait_for(task: Path, timeout: float) -> int:
    result = task / "result.json"
    begin = time.time()
    try:
        created = (task / "cmd.json").stat().st_mtime
    except OSError:
        created = begin
    while not result.exists():
        if time.time() - begin > timeout:
            print(f"crew: TIMEOUT after {int(timeout)}s waiting; the task may still be running.")
            print(f"crew: collect it later with: crew.py collect \"{task}\"")
            return 3
        state = runner_state(task)
        if state == "dead":
            time.sleep(2)
            if not result.exists():
                write_harness_result(task, "runner exited without writing result.json (window closed or process killed)")
            break
        if state == "unknown" and time.time() - created > STARTUP_GRACE_SEC:
            write_harness_result(task, f"runner never started within {STARTUP_GRACE_SEC}s (terminal or background launch failed)")
            break
        time.sleep(2)
    try:
        retried = read_json(result).get("retried_as")
    except (OSError, ValueError):
        retried = None
    if retried and Path(retried).is_dir():
        print(f"crew: {read_json(task / 'cmd.json').get('agent')} hit its usage limit; "
              f"the task was sent on to another agent (run {Path(retried).name})")
        return wait_for(Path(retried), max(timeout - (time.time() - begin), 60))
    return print_result(task)


def wait_for_panel(panel: Path, timeout: float) -> int:
    info = read_json(panel / "panel.json")
    begin, codes = time.time(), []
    for i, member in enumerate(info["members"], 1):
        print(f"\n===== panel member {i} of {len(info['members'])}: {info['agents'][i - 1]} =====")
        codes.append(wait_for(Path(member), max(timeout - (time.time() - begin), 60)))
    print(f"\n===== panel {panel.name}: {sum(c == 0 for c in codes)} of {len(codes)} ok =====")
    print("crew: compare the answers: where they disagree, check the claim yourself before using it.")
    return 0 if all(c == 0 for c in codes) else (3 if 3 in codes else 2)


def print_result(task: Path) -> int:
    time.sleep(0.2)
    r = read_json(task / "result.json")
    scope = r.get("scope") or {}
    secs = r.get("seconds")
    line = f"crew: {r.get('status')} | role={r.get('role')} agent={r.get('agent')}"
    resolved = r.get("resolved_model")
    if resolved and resolved != r.get("model"):
        line += f" (ran {resolved})"
    line += f" | {'?' if secs is None else secs}s"
    if r.get("tokens"):
        line += f" | tokens={r['tokens']}"
    if r.get("cost_usd"):
        line += f" | cost=${r['cost_usd']:.2f}" + ("?" if r.get("cost_note") else "")
    print(line)
    if r.get("cost_note"):
        print(f"crew: {r['cost_note']}")
    if scope:
        print(f"crew: scope access={scope.get('access')} web={on_off(scope.get('web'))} network={on_off(scope.get('network'))}")
    print(f"crew: dir={task}")
    if r.get("note"):
        print(f"crew: note={r['note']}")
    if r.get("violations"):
        print("crew: SCOPE VIOLATION: files changed that this task's access level does not allow:")
        for p in r["violations"]:
            print(f"  {p}")
    if r.get("touched_files"):
        print("crew: touched files (relative to the repository root):")
        for p in r["touched_files"]:
            print(f"  {p}")
    if r.get("checks"):
        print("crew: fact check (crew fetched the cited URLs and package registries; details in checks.json):")
        for line in summarize_checks(r["checks"]):
            print(f"  {line}")
    print("----- final answer -----")
    final = task / "final.md"
    if final.exists() and final.read_text(encoding="utf-8", errors="replace").strip():
        print(final.read_text(encoding="utf-8", errors="replace").rstrip())
    else:
        print("(no final answer; read raw.log in the task dir)")
    return 0 if r.get("status") == "ok" else 2


# --------------------------------------------------------------- commands

def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "task"


def new_task_dir(role: str, slug: str) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = RUNS_DIR / f"{stamp}-{slugify(role)}-{slugify(slug)}"
    task, n = base, 1
    while True:
        try:
            task.mkdir()
            return task
        except FileExistsError:
            n += 1
            task = base.with_name(f"{base.name}-{n}")


def read_task_text(args) -> str:
    text = ""
    if args.task_file:
        text = Path(args.task_file).read_text(encoding="utf-8-sig")
    if args.task:
        text = "\n\n".join(t for t in (text, args.task) if t)
    return text.strip()


def resolve_caller(arg):
    if arg == "none":
        return None
    return arg or detect_caller()


REQUEST_KEYS = ("role", "agent", "model", "effort", "cd", "access", "web", "network", "target", "slug",
                "max_minutes", "caller", "view", "hold", "timeout")


def cmd_run(args) -> int:
    if os.environ.get(NESTED_MARKER):
        raise CrewError("crew is running inside a delegated task; delegated agents cannot delegate further.")
    cfg = load_config()
    if args.resume:
        return run_resume(cfg, args)
    excluded = []
    retry_of = None
    if args.retry_of:
        # Re-send a task whose agent hit its usage limit, to the next eligible agent.
        retry_of = Path(args.retry_of).expanduser().resolve()
        try:
            prev = read_json(retry_of / "cmd.json")
            req = prev["request"]
            text = (retry_of / "request.md").read_text(encoding="utf-8")
        except (OSError, ValueError, KeyError):
            raise CrewError(f"{retry_of} has no stored request to retry")
        for key in REQUEST_KEYS:
            setattr(args, key, req.get(key))
        args.caller = req.get("caller") or "none"
        excluded = list(req.get("excluded") or []) + [prev["backend"]]
    else:
        text = read_task_text(args)
    caller = resolve_caller(args.caller)
    if not args.role:
        raise CrewError("--role is required (or --resume <taskdir>)")
    roles = cfg.get("roles") or {}
    role_cfg = roles.get(args.role)
    if role_cfg is None:
        raise CrewError(f"Unknown role '{args.role}'. Known: {', '.join(roles)}. Add one with `crew.py set-role`.")
    cwd = Path(args.cd).expanduser().resolve()
    if not cwd.is_dir():
        raise CrewError(f"--cd {cwd} is not a directory")
    access = args.access or role_cfg.get("access", "read")
    if access not in ACCESS_LEVELS:
        raise CrewError(f"Bad access '{access}' ({' | '.join(ACCESS_LEVELS)})")
    scope = {"access": access,
             "web": role_cfg.get("web", False) if args.web is None else args.web,
             "network": role_cfg.get("network", False) if args.network is None else args.network}
    request = {key: getattr(args, key, None) for key in REQUEST_KEYS}
    request.update(cd=str(cwd), caller=caller, excluded=excluded)

    if args.agent:  # an explicit choice is always honoured, so it is never retried elsewhere
        options = [choose_agent(cfg, args.agent, caller, explicit=True)]
    else:
        options = [o for o in candidates_for(cfg, role_cfg.get("agent"), caller, balance=bool(role_cfg.get("balance")))
                   if o[0]["backend"] not in excluded]
        if not options:
            raise CrewError(f"no eligible agent for role '{args.role}' outside {', '.join(excluded)}")

    if args.panel:
        return run_panel(cfg, args, role_cfg, options, cwd, scope, text, caller, request)
    spec, exe, note = options[0]
    request["retry"] = not args.agent and len(options) > 1
    task = launch_task(cfg, args, args.role, role_cfg, spec, exe, cwd, scope, text, caller, note,
                       extra={"request": request, "retry_of": str(retry_of) if retry_of else None})
    if args.wait:
        return wait_for(task, args.timeout)
    return 0


def run_resume(cfg, args) -> int:
    text = read_task_text(args)
    prev = Path(args.resume).expanduser().resolve()
    try:
        pc, pr = read_json(prev / "cmd.json"), read_json(prev / "result.json")
    except (OSError, ValueError):
        raise CrewError(f"{prev} is not a finished crew task directory")
    if pc.get("backend") not in ("codex", "claude"):
        raise CrewError("--resume works for codex and claude tasks only")
    session = pr.get("session_id")
    if not session:
        raise CrewError(f"No session id recorded in {prev / 'result.json'}")
    if not text:
        raise CrewError("--resume needs the follow-up instructions (--task or --task-file)")
    spec = parse_spec(pc["agent"])
    exe = backend_cmd(cfg, spec["backend"])
    if not exe:
        raise CrewError(f"{spec['backend']} is not installed (or backends.{spec['backend']}.exe is wrong)")
    role = pc["role"]
    role_cfg = (cfg.get("roles") or {}).get(role) or {}
    # Results written before session_cost_usd existed stored the session total in cost_usd.
    prior_cost = pr.get("session_cost_usd", pr.get("cost_usd"))
    task = launch_task(cfg, args, role, role_cfg, spec, exe, Path(pc["cwd"]), pc["scope"], text,
                       resolve_caller(args.caller), None, session=session,
                       extra={"resumed_from": str(prev), "prior_session_cost_usd": prior_cost})
    if args.wait:
        return wait_for(task, args.timeout)
    return 0


def run_panel(cfg, args, role_cfg, options, cwd, scope, text, caller, request) -> int:
    """The same task to several agents from different model families at once, for comparison."""
    members, families = [], set()
    for spec, exe, note in options:
        family = model_family(spec)
        if family not in families:
            families.add(family)
            members.append((spec, exe, note))
        if len(members) == args.panel:
            break
    if len(members) < 2:
        raise CrewError(f"a panel needs agents from at least two model families; role '{args.role}' has "
                        f"{len(members)} eligible (see `crew.py roles --caller {caller or 'none'}`)")
    if len(members) < args.panel:
        print(f"crew: only {len(members)} model families are available for this panel")
    panel = new_task_dir("panel", args.slug or cwd.name)
    request = dict(request, retry=False)  # a retry could land on a family already in the panel
    tasks = [launch_task(cfg, args, args.role, role_cfg, spec, exe, cwd, scope, text, caller, note,
                         extra={"request": request, "panel": str(panel)})
             for spec, exe, note in members]
    write_json(panel / "panel.json", {"role": args.role, "members": [str(t) for t in tasks],
                                      "agents": [spec_str(s) for s, _, _ in members], "created": now_iso()})
    (panel / "request.md").write_text(text, encoding="utf-8")
    print(f"crew: panel={panel}")
    if args.wait:
        return wait_for_panel(panel, args.timeout)
    return 0


def launch_task(cfg, args, role, role_cfg, spec, exe, cwd, scope, text, caller, choice_note,
                session=None, extra=None) -> Path:
    """Write the task folder and start its runner. `text` is the caller's own request; the scope
    note, role text and (for reviews) the diff are added here because they depend on the backend."""
    spec = dict(spec)
    if args.model and not session:
        spec["model"] = args.model
    request_text = text
    backend = spec["backend"]
    effort = args.effort or spec.get("effort") or role_cfg.get("effort")
    top = git_top(cwd)
    agy_fetch = backend == "agy" and scope["web"] and agy_fetch_allowed(cfg)
    enf = enforcement(backend, scope, bool(top), agy_fetch)

    is_review = role_cfg.get("mode") == "review" and not session
    builtin_review = is_review and backend == "codex" and not text
    if is_review:
        if not top:
            raise CrewError("A review needs a git repository (--cd inside one)")
        if not builtin_review:
            text = review_task(text, args.target, top, embed=(backend != "codex"), access=scope["access"])
    if not text and not builtin_review:
        raise CrewError("Nothing to do: pass --task or --task-file")

    task = new_task_dir(role, args.slug or cwd.name)
    scratch = task / "scratch"
    scratch.mkdir()
    if builtin_review:
        sent = f"(built-in codex review of target: {args.target}; no prompt is sent)"
    elif session:
        sent = text  # the session already holds the original scope and instructions
    else:
        parts = [scope_preamble(backend, scope, scratch, top, agy_fetch), role_cfg.get("preamble") or "", text]
        sent = "\n\n".join(p for p in parts if p)
    (task / "task.md").write_text(sent, encoding="utf-8")
    (task / "request.md").write_text(request_text, encoding="utf-8")

    if backend == "codex":
        mode = "resume" if session else ("builtin-review" if builtin_review else "exec")
        argv = exe + codex_args(cfg, spec, effort, scope, task, cwd, mode, args.target, session)
        use_stdin = not builtin_review
    elif backend == "claude":
        argv = exe + claude_args(exe, spec, effort, scope, task, top, cwd, role_cfg, session)
        use_stdin = True
    else:
        argv = exe + agy_args(spec, effort, scope, task, cwd, sent)
        use_stdin = False

    env = {"CREW_SCRATCH": str(scratch), NESTED_MARKER: str(task)}
    if scope["access"] != "read":
        env.update({"TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch)})
    if scope["access"] == "verify":
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # keeps __pycache__ from tripping the violation check
    for key, value in (role_cfg.get("env") or {}).items():
        env[key] = str(value).replace("{cwd}", str(cwd)).replace("{scratch}", str(scratch)).replace("{task}", str(task))

    allowed = []
    if top:
        for p in role_cfg.get("scratch_paths") or []:
            rel = Path(os.path.relpath(cwd / p, top)).as_posix()
            allowed.append(rel.rstrip("/") + "/" if p.endswith("/") else rel)
    max_minutes = args.max_minutes or role_cfg.get("max_minutes") or cfg.get("max_minutes") or 60
    agent_label = spec_str(dict(spec, effort=effort))
    view = args.view or cfg.get("view", "auto")
    hold = cfg.get("hold", True) if args.hold is None else args.hold

    cmd = {"role": role, "agent": agent_label, "backend": backend, "model": spec["model"], "effort": effort,
           "family": model_family(spec), "caller": caller, "caller_session": caller_session(), "scope": scope,
           "enforcement": enf, "cwd": str(cwd), "git_top": top, "argv": argv, "stdin": use_stdin, "env": env,
           "allowed_paths": allowed, "max_minutes": max_minutes, "resumed_from": None,
           "prior_session_cost_usd": None, "fact_check": bool(scope["web"] and cfg.get("fact_check", True)),
           "view": view, "hold": hold, "created": now_iso()}
    cmd.update(extra or {})
    write_json(task / "cmd.json", cmd)

    runner = [sys.executable, "-u", str(Path(__file__).resolve()), "_run", "--dir", str(task)]
    how = None
    if view in ("auto", "tab"):
        how = launch_tab(runner + (["--hold"] if hold else []), f"crew {role} {slugify(args.slug or cwd.name)}", task, cfg)
        if how and not wait_for_claim(task, TAB_CLAIM_SEC):
            # Some launchers report success without opening anything. The runner that claims
            # the task first wins, so a late tab exits instead of running it twice.
            print(f"crew: the {how} did not start within {TAB_CLAIM_SEC}s; running hidden instead")
            how = None
        elif not how and view == "tab":
            print("crew: no usable terminal launcher; running hidden instead (see README: Visible terminals)")
    if not how:
        launch_hidden(runner, cwd)
        how = "hidden background process"
    print(f"crew: launched {role} -> {agent_label} in {how}")
    if caller:
        print(f"crew: caller={caller}" + (f" ({choice_note})" if choice_note else ""))
    print(f"crew: scope access={scope['access']} web={on_off(scope['web'])} network={on_off(scope['network'])}")
    for dim, how_enforced in enf.items():
        print(f"crew:   {dim}: {how_enforced}")
    print(f"crew: dir={task}")
    return task


def cmd_collect(args) -> int:
    task = Path(args.dir).expanduser().resolve()
    if (task / "panel.json").exists():
        return wait_for_panel(task, args.timeout)
    if not (task / "cmd.json").exists():
        raise CrewError(f"{task} is not a crew task directory")
    return wait_for(task, args.timeout)


def cmd_check(args) -> int:
    task = Path(args.dir).expanduser().resolve()
    if not (task / "final.md").exists():
        raise CrewError(f"{task} has no final.md to check")
    summary = fact_check(task)
    try:
        r = read_json(task / "result.json")
        r["checks"] = summary
        write_json(task / "result.json", r)
    except (OSError, ValueError):
        pass
    for line in summarize_checks(summary):
        print(line)
    print(f"crew: details in {task / 'checks.json'}")
    return 0 if not summary.get("urls_failed") and not summary.get("packages") and not summary.get("error") else 2


def cmd_quota(args) -> int:
    state = read_quota()
    if not state:
        print("crew: no usage-limit data yet (claude and codex runs report it; agy does not)")
        return 0
    now = time.time()
    for backend, entry in sorted(state.items()):
        score = quota_score(entry, now)
        line = f"{backend:<8} {'?' if score is None else f'{score:.0%} used'}"
        if (entry.get("limited_until") or 0) > now:
            line += f"  LIMITED until {dt.datetime.fromtimestamp(entry['limited_until']):%H:%M %d %b}"
        for name, w in (entry.get("windows") or {}).items():
            live = not w.get("resets_at") or w["resets_at"] > now
            when = f", resets {dt.datetime.fromtimestamp(w['resets_at']):%H:%M %d %b}" if w.get("resets_at") else ""
            line += f"  | {name} {w.get('used', 0):.0%}{when}" if live else f"  | {name} reset"
        print(line + f"  (seen {entry.get('updated')})")
    return 0


def cmd_status(args) -> int:
    if not RUNS_DIR.exists():
        print("crew: no tasks yet")
        return 0
    dirs = sorted((d for d in RUNS_DIR.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)[: args.last]
    for d in dirs:
        rp = d / "result.json"
        if rp.exists():
            r = read_json(rp)
            print(f"{d.name:<48} {str(r.get('status')):<16} {str(r.get('agent')):<32} {r.get('seconds')}s")
        else:
            state = {"alive": "running", "dead": "dead (runner gone, no result)"}.get(runner_state(d), "starting?")
            print(f"{d.name:<48} {state}")
    return 0


def cmd_stats(args) -> int:
    if not LEDGER.exists():
        print("crew: no tasks yet")
        return 0
    rows = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    # Rows written before resumed_from was logged recorded a resumed claude run's whole-session
    # cost. Subtract what the run it continued already recorded, using the run folder if it is kept.
    logged = {r.get("dir"): r.get("cost_usd") for r in rows}
    for r in rows:
        if "resumed_from" in r or not isinstance(r.get("cost_usd"), (int, float)):
            continue
        try:
            prev = read_json(RUNS_DIR / str(r.get("dir")) / "cmd.json").get("resumed_from")
        except (OSError, ValueError):
            continue
        before = logged.get(Path(prev).name) if prev else None
        if isinstance(before, (int, float)):
            r["cost_usd"] = max(r["cost_usd"] - before, 0.0)
    groups = {}
    for row in rows:
        name = f"{row.get('backend')}:{row.get('resolved_model') or row.get('model')}"
        g = groups.setdefault(name, {"tasks": 0, "ok": 0, "sec": 0.0, "tokens": 0, "cost": 0.0, "guess": False})
        g["tasks"] += 1
        g["ok"] += row.get("status") == "ok"
        g["sec"] += float(row.get("seconds") or 0)
        g["tokens"] += int(row.get("tokens") or 0)
        g["cost"] += float(row.get("cost_usd") or 0)
        g["guess"] |= row.get("cost_reliable") is False
    print(f"{'backend:model':<40} {'tasks':>5} {'ok':>4} {'minutes':>8} {'tokens':>12} {'cost $':>9}")
    for name, g in sorted(groups.items()):
        cost = f"{g['cost']:.2f}{'?' if g['guess'] else ''}" if g["cost"] else "-"
        print(f"{name:<40} {g['tasks']:>5} {g['ok']:>4} {g['sec'] / 60:>8.1f} {g['tokens']:>12} {cost:>9}")
    print("\ncost is the API list price the CLI reports (claude only; codex and agy report none). "
          "On a subscription it measures usage, not money. '?' = the CLI had no price for that model.")
    return 0


def describe_agent(cfg, ref, caller=None, balance=False) -> str:
    try:
        spec, _, note = choose_agent(cfg, ref, caller, balance=balance)
    except CrewError as e:
        msg = str(e)
        return ("NOT INSTALLED" if msg.startswith("No installed") else "INVALID") + f" ({msg})"
    return spec_str(spec) + (f"  [{note}]" if note else "")


def cmd_roles(args) -> int:
    cfg = load_config()
    caller = resolve_caller(args.caller)
    print(f"config: {DEFAULT_CONFIG} + {USER_CONFIG}{'' if USER_CONFIG.exists() else ' (not created yet)'}")
    print(f"caller: {caller or 'unknown (no agents skipped)'}\n")
    print("Aliases:")
    for name, ref in (cfg.get("agents") or {}).items():
        print(f"  {name:<18} {json.dumps(ref)}  ->  {describe_agent(cfg, name)}")
    print("\nRoles:")
    for name, r in (cfg.get("roles") or {}).items():
        balance = bool(r.get("balance"))
        print(f"  {name:<18} agent={json.dumps(r.get('agent'))} -> {describe_agent(cfg, r.get('agent'), caller, balance)}")
        print(f"  {'':<18} access={r.get('access', 'read')} web={on_off(r.get('web'))} "
              f"network={on_off(r.get('network'))}{' mode=review' if r.get('mode') == 'review' else ''}"
              f"{' balance=on' if balance else ''}")
        if r.get("about"):
            print(f"  {'':<18} {r['about']}")
    return 0


def cmd_models(args) -> int:
    cfg = load_config()
    cm = codex_models()
    print("codex (from the local model cache; run codex once to refresh it):")
    if cm is None:
        print("  (no cache found at " + str(codex_home() / "models_cache.json") + ")")
    for m in cm or []:
        efforts = ",".join(e.get("effort", "") for e in m.get("supported_reasoning_levels") or [])
        print(f"  codex:{m.get('slug'):<22} {m.get('description', '')}  [effort: {efforts}]")
    print("\nclaude (short aliases, or full model ids such as claude:claude-haiku-5-5):")
    if backend_cmd(cfg, "claude"):
        for alias in CLAUDE_ALIASES:
            print(f"  claude:{alias}")
        print("  [effort: low,medium,high,xhigh,max]")
        print("  Aliases are resolved by your claude CLI and can lag a release (claude 2.1.286 still maps\n"
              "  haiku to Haiku 4.5). Each run records the model that actually ran as resolved_model.")
    else:
        print("  (claude not installed)")
    print("\nagy (from `agy models`):")
    am = agy_models(cfg)
    if am is None:
        print("  (agy not installed or not responding)")
    for m in am or []:
        print(f"  agy:{m['slug']:<26} {m['description']}")
    return 0


def cmd_set_role(args) -> int:
    cfg = load_config()
    user = load_user_config()
    roles = user.setdefault("roles", {})
    if args.reset:
        roles.pop(args.role, None)
        save_user_config(user)
        builtin = args.role in (read_json(DEFAULT_CONFIG).get("roles") or {})
        print(f"crew: role '{args.role}' " + ("reset to the built-in default" if builtin else "removed"))
        return 0
    if args.role not in (cfg.get("roles") or {}) and not args.agent:
        raise CrewError(f"New role '{args.role}' needs --agent")
    entry = roles.setdefault(args.role, {})
    if args.agent:
        expand_agent(cfg, args.agent)  # validate
        entry["agent"] = args.agent if len(args.agent) > 1 else args.agent[0]
    for key in ("access", "effort", "about"):
        if getattr(args, key):
            entry[key] = getattr(args, key)
    for key in ("web", "network"):
        if getattr(args, key) is not None:
            entry[key] = getattr(args, key)
    save_user_config(user)
    merged_cfg = load_config()
    merged = merged_cfg["roles"][args.role]
    print(f"crew: role '{args.role}' -> {describe_agent(merged_cfg, merged.get('agent'))} "
          f"access={merged.get('access', 'read')} web={on_off(merged.get('web'))} "
          f"network={on_off(merged.get('network'))}  (saved to {USER_CONFIG})")
    return 0


def cmd_set_agent(args) -> int:
    user = load_user_config()
    agents = user.setdefault("agents", {})
    if args.remove:
        agents.pop(args.name, None)
        save_user_config(user)
        print(f"crew: alias '{args.name}' removed from {USER_CONFIG}")
        return 0
    if not args.refs:
        raise CrewError("Give one or more agent specs, e.g. codex:gpt-6.1-sol@high")
    value = args.refs[0] if len(args.refs) == 1 else list(args.refs)
    expand_agent(deep_merge(load_config(), {"agents": {args.name: value}}), args.name)  # validate
    agents[args.name] = value
    save_user_config(user)
    print(f"crew: alias '{args.name}' = {json.dumps(value)} -> {describe_agent(load_config(), args.name)}")
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config()
    problems = 0

    def report(ok, label, detail=""):
        nonlocal problems
        tag = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        problems += ok is False
        print(f"[{tag}] {label}" + (f": {detail}" if detail else ""))

    def first_line(r):
        lines = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()
        return lines[0] if lines else ""

    report(sys.version_info >= (3, 9), "python", sys.version.split()[0])
    report(bool(shutil.which("git")), "git", shutil.which("git") or "not found (change detection and reviews need it)")
    try:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        report(True, "runs dir", str(RUNS_DIR))
    except OSError as e:
        report(False, "runs dir", str(e))
    caller = detect_caller()
    report(True, "caller", caller or "not detected (fine when you run crew yourself)")
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"):
        report(None, "sandbox", "running inside codex's sandbox: crew needs to run outside it "
               "(write ~/.crew, start agents that use the network). Approve running it unsandboxed.")
    for backend in BACKENDS:
        cmd = backend_cmd(cfg, backend)
        if not cmd:
            report(None, backend, "not installed (roles that use it will fall back or fail)")
            continue
        try:
            version = first_line(subprocess.run(cmd + ["--version"], capture_output=True, timeout=60))
        except (OSError, subprocess.TimeoutExpired):
            version = "installed, but --version failed"
        report(True, backend, f"{version} ({' '.join(cmd)})")
        if backend == "codex":
            try:
                s = subprocess.run(cmd + ["login", "status"], capture_output=True, timeout=60)
                report(s.returncode == 0, "codex login", first_line(s))
            except (OSError, subprocess.TimeoutExpired):
                report(None, "codex login", "could not check")
            if IS_WIN and not codex_windows_sandbox():
                report(None, "codex windows sandbox", "no [windows] sandbox in config.toml; see README (Codex setup)")
            if IS_WIN and windows_service_session() and codex_windows_sandbox() == "elevated":
                report(None, "codex commands", "this process runs in Windows session 0 (service/remote), where codex's "
                       "elevated sandbox cannot start commands (\"timed out connecting runner pipe\"). "
                       "Codex can still answer, but not run commands, until you run from a desktop session")
            known = {m.get("slug") for m in codex_models() or []}
            for name, r in (cfg.get("roles") or {}).items():
                for spec in expand_agent(cfg, r.get("agent")):
                    if spec["backend"] == "codex" and known and spec["model"] not in known:
                        report(None, f"role {name}", f"model {spec['model']} is not in codex's model list (`crew.py models`)")
        if backend == "claude":
            try:
                s = subprocess.run(cmd + ["auth", "status"], capture_output=True, timeout=60)
                out = (s.stdout + s.stderr).decode("utf-8", "replace")
                logged_in = s.returncode == 0 and '"loggedIn": false' not in out
                report(logged_in, "claude login", "signed in" if logged_in else "not signed in: run `claude auth login`")
            except (OSError, subprocess.TimeoutExpired):
                report(None, "claude login", "could not check")
            help_text = cli_help(cmd)
            report(True if "--restricted" in help_text else None, "claude --restricted",
                   "supported: delegated runs ignore your own claude settings files" if "--restricted" in help_text
                   else "not in this claude version: your own claude settings also apply to delegated runs (update claude)")
            if IS_WIN:
                report(None, "claude sandbox", "not available on native Windows: shell commands in verify/write "
                       "tasks run unsandboxed (instructions + git check). Use WSL2 for a hard boundary.")
            for name, r in (cfg.get("roles") or {}).items():
                for spec in expand_agent(cfg, r.get("agent")):
                    if spec["backend"] != "claude":
                        continue
                    if spec["model"] in CLAUDE_ALIASES:
                        report(None, f"role {name}", f"claude:{spec['model']} is an alias your claude CLI resolves, and "
                               "aliases can lag a release; a full id (e.g. claude-opus-5-5) is exact")
                    elif "-" not in spec["model"]:
                        report(None, f"role {name}", f"claude model '{spec['model']}' is neither an alias nor a full name")
        if backend == "agy":
            fetch = agy_fetch_allowed(cfg)
            report(fetch or None, "agy page fetching", "allowed" if fetch else
                   f"no {AGY_FETCH_RULE} allow rule, so research can search but not read pages. "
                   "Fix: `crew.py setup-agy-web` (edits agy settings, with a backup)")
            known = {m["slug"] for m in agy_models(cfg) or []}
            for name, r in (cfg.get("roles") or {}).items():
                for spec in expand_agent(cfg, r.get("agent")):
                    if spec["backend"] == "agy" and known and spec["model"] not in known:
                        report(None, f"role {name}", f"model {spec['model']} is not in `agy models`")
    for name, r in (cfg.get("roles") or {}).items():
        d = describe_agent(cfg, r.get("agent"), caller)
        report(False if d.startswith(("INVALID", "NOT")) else True, f"role {name}", d)
    view = cfg.get("view", "auto")
    if view != "hidden":
        if cfg.get("terminal"):
            launcher = "custom terminal command"
        elif IS_WIN and windows_service_session():
            launcher = None
        elif IS_WIN:
            launcher = "Windows Terminal (wt)" if shutil.which("wt") else None
        elif IS_MAC:
            launcher = "Terminal.app via osascript" if shutil.which("osascript") else None
        else:
            launcher = "tmux" if os.environ.get("TMUX") and shutil.which("tmux") else None
        report(True if launcher else None, "visible terminal", launcher or
               ("this process runs in Windows session 0 (service/remote), so no window can open; tasks run hidden"
                if IS_WIN and windows_service_session() else
                "none found; tasks run hidden (set \"terminal\" in config, see README)"))
    print(f"\nconfig: {USER_CONFIG}{'' if USER_CONFIG.exists() else ' (not created; built-in defaults in use)'}")
    return 1 if problems else 0


def cmd_setup_agy_web(args) -> int:
    cfg = load_config()
    path = agy_settings_paths(cfg)[0]
    if agy_fetch_allowed(cfg):
        print(f"crew: {AGY_FETCH_RULE} is already allowed; nothing to do")
        return 0
    data = {}
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        print(f"crew: backed up {path} -> {backup}")
        data = read_json(path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    allow.append(AGY_FETCH_RULE)
    write_json(path, data)
    print(f"crew: added {AGY_FETCH_RULE} to permissions.allow in {path}")
    print("crew: agy may now read any web page without asking. Remove the rule to undo.")
    return 0


# ------------------------------------------------------------------- runner

COLOURS = {"dim": "90", "red": "31", "green": "32", "yellow": "33", "cyan": "36", "magenta": "35", "bold": "1"}


def enable_vt() -> None:
    if not IS_WIN:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 4)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass


class View:
    """Shows agent output live and keeps a compact copy in raw.log. Plain-text agents (agy)."""

    def __init__(self, task: Path):
        self.raw = open(task / "raw.log", "a", encoding="utf-8")
        self.colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.text = []
        self.session = None
        self.tokens = None
        self.cost = None
        self.error = None
        self.denied = []
        self.resolved_model = None  # the model the backend reports it ran, when it says
        self.model_usage = None  # per-model tokens and cost, including hidden helper models
        self.cost_note = None  # set when the reported cost can't be trusted
        self.quota = None  # usage-limit windows the backend reported, if it reports them

    def show(self, text: str, style=None) -> None:
        out = f"\x1b[{COLOURS[style]}m{text}\x1b[0m" if style and self.colour else text
        print(out, flush=True)
        self.raw.write(text + "\n")
        self.raw.flush()

    def feed(self, line: str) -> None:
        line = line.rstrip("\r\n")
        self.text.append(line)
        self.show(line)

    def final_text(self) -> str:
        return strip_ansi("\n".join(self.text)).strip()

    def all_text(self) -> str:
        return strip_ansi("\n".join(self.text))

    def close(self) -> None:
        self.raw.close()


class JsonView(View):
    """Base for agents that stream JSON lines: the full stream goes to events.jsonl."""

    def __init__(self, task: Path):
        super().__init__(task)
        self.events = open(task / "events.jsonl", "a", encoding="utf-8")
        self.last_message = None

    def feed(self, line: str) -> None:
        self.events.write(line if line.endswith("\n") else line + "\n")
        self.events.flush()
        s = line.strip()
        if not s:
            return
        self.text.append(s)
        try:
            e = json.loads(s)
        except ValueError:
            self.show(short(s, 200), "dim")
            return
        if isinstance(e, dict):
            self.event(e)

    def event(self, e: dict) -> None:
        raise NotImplementedError

    def final_text(self) -> str:
        return (self.last_message or "").strip()

    def close(self) -> None:
        super().close()
        self.events.close()


class CodexView(JsonView):
    """Renders `codex exec --json` compactly: one line per command, agent messages in full."""

    SHELL_WRAPPER = re.compile(r'^"?[^"]*?[\\/]?(pwsh|powershell|cmd|bash|zsh|sh)(\.exe)?"?\s+(-Command|/c|-lc|-c)\s+')

    def event(self, e: dict) -> None:
        kind = e.get("type", "")
        if kind == "thread.started":
            self.session = e.get("thread_id")
            self.show(f"session {self.session}", "dim")
        elif kind == "turn.completed":
            u = e.get("usage") or {}
            inp, cached = int(u.get("input_tokens") or 0), int(u.get("cached_input_tokens") or 0)
            out = int(u.get("output_tokens") or 0) + int(u.get("reasoning_output_tokens") or 0)
            self.tokens = inp - cached + out  # roughly what a subscription meters
            self.show(f"tokens: in {inp} (cached {cached}), out {out}", "dim")
        elif kind == "turn.failed":
            self.error = (e.get("error") or {}).get("message") or self.error
            self.show(f"TURN FAILED: {self.error}", "red")
        elif kind == "error":
            self.error = e.get("message") or self.error
            self.show(f"ERROR: {e.get('message')}", "red")
        elif kind.startswith("item."):
            self.item(kind, e.get("item") or {})

    def item(self, kind: str, it: dict) -> None:
        t = it.get("type")
        done = kind == "item.completed"
        if t == "agent_message" and done:
            self.last_message = it.get("text") or ""
            self.show("")
            self.show(self.last_message)
            self.show("")
        elif t == "reasoning" and done and it.get("text"):
            self.show("~ " + short(it["text"], 200), "dim")
        elif t == "command_execution":
            c = self.SHELL_WRAPPER.sub("", str(it.get("command") or ""))
            if len(c) >= 2 and c[0] in "'\"" and c[-1] == c[0]:
                c = c[1:-1]
            if kind == "item.started":
                self.show("$ " + short(c, 180), "cyan")
            elif done:
                out = str(it.get("aggregated_output") or "").rstrip()
                n = len(out.split("\n")) if out else 0
                if it.get("exit_code") == 0:
                    self.show(f"  -> ok, {n} lines", "dim")
                else:
                    self.show(f"  -> exit {it.get('exit_code')}, {n} lines", "yellow")
                    for tail in out.split("\n")[-3:] if out else []:
                        self.show("     " + short(tail, 160), "yellow")
        elif t == "web_search" and done:
            q = it.get("query") or (it.get("action") or {}).get("query") or ""
            if q:
                self.show("web: " + short(q, 160), "magenta")
        elif t == "file_change" and done:
            for ch in it.get("changes") or []:
                self.show(f"edit: {ch.get('kind')} {ch.get('path')}", "yellow")
        elif t == "mcp_tool_call" and done:
            self.show(f"mcp: {it.get('server')}/{it.get('tool')} {it.get('status')}", "cyan")
        elif t == "todo_list" and not done:
            plan = " | ".join(("[x] " if x.get("completed") else "[ ] ") + str(x.get("text")) for x in it.get("items") or [])
            self.show("plan: " + short(plan, 300), "green")
        elif t == "error":
            self.show(f"error: {it.get('message')}", "red")


class ClaudeView(JsonView):
    """Renders `claude -p --output-format stream-json`: one line per tool call, text in full."""

    TOOL_ARG = ("command", "file_path", "pattern", "query", "url", "path")

    def event(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "rate_limit_event":
            self.quota = claude_quota_windows(e) or self.quota
        elif kind == "system" and e.get("subtype") == "init":
            self.session = e.get("session_id") or self.session
            self.resolved_model = e.get("model") or self.resolved_model
            self.show(f"session {self.session}  model {e.get('model')}  tools {','.join(e.get('tools') or [])}", "dim")
        elif kind == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.last_message = block["text"]
                    self.show("")
                    self.show(block["text"])
                elif block.get("type") == "tool_use":
                    args = block.get("input") or {}
                    key = next((k for k in self.TOOL_ARG if k in args), None)
                    detail = args[key] if key else json.dumps(args, ensure_ascii=False)
                    self.show(f"{block.get('name')}: {short(detail, 170)}", "cyan")
        elif kind == "user":
            for block in (e.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    if isinstance(content, list):
                        content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                    self.show("  -> error: " + short(content, 160), "yellow")
        elif kind == "result":
            self.session = e.get("session_id") or self.session
            if isinstance(e.get("result"), str) and e["result"].strip():
                self.last_message = e["result"]
            u = e.get("usage") or {}
            self.tokens = (int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0)
                           + int(u.get("cache_creation_input_tokens") or 0)) or None
            # total_cost_usd covers the whole session, earlier --resume runs included; the runner
            # subtracts what earlier runs already recorded.
            self.cost = e.get("total_cost_usd")
            mu = e.get("modelUsage")
            if isinstance(mu, dict) and mu:
                self.model_usage = {
                    name: {"input": int(m.get("inputTokens") or 0), "output": int(m.get("outputTokens") or 0),
                           "cache_read": int(m.get("cacheReadInputTokens") or 0),
                           "cache_write": int(m.get("cacheCreationInputTokens") or 0),
                           "web_searches": int(m.get("webSearchRequests") or 0),
                           "cost_usd": m.get("costUSD"), "cost_basis": m.get("costBasis")}
                    for name, m in mu.items() if isinstance(m, dict)}
                # Tools such as WebSearch run on a helper model; its tokens belong in the count too.
                self.tokens = sum(m["input"] + m["output"] + m["cache_write"]
                                  for m in self.model_usage.values()) or self.tokens
                unpriced = sorted(n for n, m in self.model_usage.items()
                                  if m["cost_basis"] not in (None, "list"))
                if unpriced:
                    self.cost_note = (f"claude has no price list for {', '.join(unpriced)}, so its cost "
                                      "figure is a guess; update the claude CLI")
                helpers = sorted(n for n in self.model_usage if n != self.resolved_model)
                if helpers and self.resolved_model:
                    self.show(f"helper models: {', '.join(helpers)}", "dim")
            self.denied = sorted({str(d.get("tool_name")) for d in e.get("permission_denials") or [] if isinstance(d, dict)})
            if e.get("is_error"):
                self.error = short(e.get("result") or e.get("subtype") or "claude reported an error", 400)
            cost = f"${self.cost:.4f}" if isinstance(self.cost, (int, float)) else "n/a"
            self.show(f"tokens: {self.tokens}  cost: {cost}" +(f"  denied: {', '.join(self.denied)}" if self.denied else ""), "dim")


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        return
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(5)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def heartbeat(task: Path, stop: threading.Event) -> None:
    info = {"pid": os.getpid(), "started": now_iso()}
    while True:
        info["beat"] = time.time()
        try:
            write_json(task / "run.json", info)
        except OSError:
            pass
        if stop.wait(HEARTBEAT_SEC):
            return


def print_header(cmd: dict, text: str, task: Path, view: View) -> None:
    argv = list(cmd["argv"])
    if cmd["backend"] == "agy" and argv:
        argv[-1] = short(argv[-1], 60)  # the prompt; shown in full below
    scope = cmd["scope"]
    view.show("=" * 72, "dim")
    view.show(f"crew  role={cmd['role']}  agent={cmd['agent']}" + (f"  caller={cmd['caller']}" if cmd.get("caller") else ""), "cyan")
    view.show(f"scope access={scope['access']}  web={on_off(scope['web'])}  network={on_off(scope['network'])}", "cyan")
    for dim, how in (cmd.get("enforcement") or {}).items():
        view.show(f"      {dim}: {how}", "dim")
    view.show(f"cwd   {cmd['cwd']}", "dim")
    view.show(f"exec  {subprocess.list2cmdline(argv) if IS_WIN else shlex.join(argv)}", "dim")
    view.show(f"dir   {task}", "dim")
    view.show("-" * 30 + " TASK (input) " + "-" * 28, "yellow")
    view.show(text)
    view.show("-" * 30 + " OUTPUT " + "-" * 34, "green")
    if cmd["backend"] == "agy":
        view.show("(agy prints nothing until it has finished; long research can take 15+ minutes)", "dim")


def cmd_runner(args) -> int:
    use_utf8_stdio()
    enable_vt()
    task = Path(args.dir)
    if not claim_task(task):
        print(f"crew: {task.name} is already being run by another runner; nothing to do here.")
        return 0
    started = time.time()
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(task, stop), daemon=True).start()
    cmd = None
    try:
        cmd = read_json(task / "cmd.json")
        run_agent(task, cmd, started)
    except Exception as e:  # any harness failure must still leave a result.json
        write_harness_result(task, f"{type(e).__name__}: {e}", cmd, round(time.time() - started, 1))
        print(f"crew harness error: {e}", flush=True)
    finally:
        stop.set()
    if args.hold:
        try:
            input("\nPress Enter to close this tab")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


# Per-session variables the calling agent sets for its own commands (session ids, messaging
# sockets and tokens, effort). Passed on, they would make a delegated agent think it belongs to
# the caller's session, and would hand the caller's tokens to another vendor's process.
# Configuration variables (CLAUDE_CODE_USE_BEDROCK, CODEX_HOME, ...) are kept.
SESSION_VAR_RE = re.compile(
    r"^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|CLAUDE_CODE_ENTRYPOINT"
    r"|CLAUDE_CODE_\w*(SESSION|MESSAGING|BRIDGE|CHILD|EXECPATH)\w*"
    r"|CODEX_(SESSION_ID|THREAD_ID|CI|SANDBOX\w*|VERSION))$")


def retry_elsewhere(task: Path):
    """Send a task whose agent hit its usage limit to the next eligible agent, as a new run.
    Returns (new run folder or None, note)."""
    try:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "run", "--retry-of", str(task)],
                           capture_output=True, timeout=120, env=agent_env({}))
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"could not retry elsewhere: {e}"
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    found = re.search(r"^crew: dir=(.+)$", out, re.M)
    if r.returncode != 0 or not found:
        return None, "not retried: " + short(out.replace("crew: error: ", ""), 200)
    agent = re.search(r"^crew: launched \S+ -> (\S+)", out, re.M)
    return found[1].strip(), f"retried on {agent[1] if agent else 'another agent'} (run {Path(found[1].strip()).name})"


def agent_env(cmd: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not SESSION_VAR_RE.match(k.upper())}
    env.update(cmd.get("env") or {})
    return env


def run_agent(task: Path, cmd: dict, started: float) -> None:
    text = (task / "task.md").read_text(encoding="utf-8")
    view = {"codex": CodexView, "claude": ClaudeView}.get(cmd["backend"], View)(task)
    print_header(cmd, text, task, view)
    top = cmd.get("git_top")
    before = git_state(top) if top else None
    popen_kw = {} if IS_WIN else {"start_new_session": True}
    proc = subprocess.Popen(cmd["argv"], cwd=cmd["cwd"], env=agent_env(cmd),
                            stdin=subprocess.PIPE if cmd["stdin"] else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kw)
    if cmd["stdin"]:
        def feed():
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=feed, daemon=True).start()
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        kill_tree(proc)
    timer = threading.Timer(float(cmd.get("max_minutes") or 60) * 60, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        for raw in proc.stdout:
            view.feed(raw.decode("utf-8", "replace"))
        rc = proc.wait()
    finally:
        timer.cancel()
        view.close()

    final = task / "final.md"
    if not final.exists() or final.stat().st_size == 0:
        answer = view.final_text()
        if answer:
            final.write_text(answer + "\n", encoding="utf-8")
    has_final = final.exists() and final.read_text(encoding="utf-8", errors="replace").strip() != ""

    touched = changed_paths(before, git_state(top)) if top else []
    violations = []
    if cmd["scope"]["access"] != "write":
        allowed = cmd.get("allowed_paths") or []
        violations = [p for p in touched if not any(p.startswith(a) for a in allowed)]

    note = None
    if timed_out.is_set():
        status, note = "timeout", f"killed after max_minutes={cmd.get('max_minutes')}"
    elif rc != 0 or view.error:
        status = "agent-error"
        note = short(view.error or (view.text[-1] if view.text else ""), 400) or None
    elif not has_final:
        status = "empty"
    else:
        status = "ok"
    # agy print mode cannot prompt, so an unapproved tool is auto-denied and the
    # "answer" is only the denial notice (exit code 0).
    if cmd["backend"] == "agy" and re.search(r"auto-denied|headless mode cannot prompt", view.all_text()):
        status = "permission-denied"
        note = "agy denied a tool it had no allow rule for; see final.md and README (agy permissions)"
    if view.denied:
        denied = f"denied tool calls outside the scope: {', '.join(view.denied)}"
        if status in ("empty", "agent-error"):
            status = "permission-denied"
        note = f"{note}; {denied}" if note else denied
    if violations:
        status = "scope-violation"
        note = (f"{len(violations)} file(s) changed outside what access={cmd['scope']['access']} allows. "
                "If you edited the repo yourself during the run, those edits are included.")

    backend = cmd["backend"]
    quota = view.quota
    if backend == "codex" and view.session:
        quota = codex_quota_windows(view.session)
    if quota:
        record_quota(backend, quota, f"{backend} run {task.name}")
    retried_as = None
    tail = "\n".join([view.error or ""] + view.text[-30:])
    hit_limit = any((w or {}).get("used", 0) >= 1 for w in (quota or {}).values())
    if status in ("agent-error", "empty") and (looks_rate_limited(tail) or hit_limit):
        status = "limited"
        resets = limit_reset_from_text(tail) or max(
            [w["resets_at"] for w in (quota or {}).values() if (w or {}).get("used", 0) >= 1 and w.get("resets_at")],
            default=None)
        record_limit(backend, resets, load_config().get("limit_cooldown_minutes", 60))
        note = f"{backend} hit its usage limit" + (f" (resets {dt.datetime.fromtimestamp(resets):%H:%M %d %b})"
                                                   if resets else "") + (f": {note}" if note else "")
        if (cmd.get("request") or {}).get("retry"):
            retried_as, retry_note = retry_elsewhere(task)
            note += f"; {retry_note}"
            view.show(f"crew: {retry_note}", "yellow")

    checks = None
    if status == "ok" and cmd.get("fact_check"):
        view.show("crew: fact-checking the cited URLs and package versions...", "dim")
        checks = fact_check(task)
        for line in summarize_checks(checks):
            view.show(f"  {line}", "yellow" if "fail" in line or "not" in line or "latest is" in line else "dim")
    seconds = round(time.time() - started, 1)
    session_cost = view.cost
    cost = session_cost
    if isinstance(session_cost, (int, float)) and isinstance(cmd.get("prior_session_cost_usd"), (int, float)):
        cost = max(session_cost - cmd["prior_session_cost_usd"], 0.0)
    resolved = view.resolved_model or cmd["model"]
    result = {"status": status, "note": note, "exit": rc, "role": cmd["role"], "agent": cmd["agent"],
              "backend": cmd["backend"], "model": cmd["model"], "resolved_model": resolved,
              "scope": cmd["scope"], "seconds": seconds, "tokens": view.tokens, "cost_usd": cost,
              "session_cost_usd": session_cost, "cost_note": view.cost_note, "model_usage": view.model_usage,
              "session_id": view.session, "touched_files": touched, "violations": violations,
              "denied_tools": view.denied, "quota": quota, "checks": checks, "retried_as": retried_as,
              "final": str(final), "finished": now_iso()}
    write_json(task / "result.json", result)
    append_line(LEDGER, json.dumps({
        "ts": now_iso(), "role": cmd["role"], "backend": cmd["backend"], "model": cmd["model"],
        "resolved_model": resolved, "caller": cmd.get("caller"), "caller_session": cmd.get("caller_session"),
        "status": status, "seconds": seconds, "tokens": view.tokens, "cost_usd": cost,
        "cost_reliable": view.cost_note is None, "resumed_from": cmd.get("resumed_from"),
        "retry_of": cmd.get("retry_of"), "panel": cmd.get("panel"), "dir": task.name}))
    colour, reset = ("\x1b[32m" if status == "ok" else "\x1b[31m", "\x1b[0m") if view.colour else ("", "")
    print("-" * 72)
    print(f"{colour}crew  {status}  exit={rc}  {seconds}s"
          + (f"  tokens={view.tokens}" if view.tokens else "")
          + (f"  touched={len(touched)} file(s)" if touched else "") + reset, flush=True)


# --------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crew.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    callers = BACKENDS + ("none",)

    r = sub.add_parser("run", help="dispatch a task")
    r.add_argument("--role", help="role from the config (review, check, implement, ...)")
    r.add_argument("--task", help="what the agent should do, inline")
    r.add_argument("--task-file", help="what the agent should do, from a file")
    r.add_argument("--cd", default=os.getcwd(), help="working directory for the agent (default: here)")
    r.add_argument("--caller", choices=callers,
                   help="the agent running crew; its own vendor is skipped in fallback lists (default: detected)")
    r.add_argument("--agent", help="use this agent: an alias or backend:model[@effort]")
    r.add_argument("--model", help="override just the model name")
    r.add_argument("--effort", help="override reasoning effort (low | medium | high | ...)")
    r.add_argument("--access", choices=ACCESS_LEVELS, help="files: read | verify (build/test, scratch writes) | write")
    r.add_argument("--web", action=argparse.BooleanOptionalAction, default=None, help="allow web search / fetch")
    r.add_argument("--network", action=argparse.BooleanOptionalAction, default=None,
                   help="allow network for commands the agent runs (codex/claude, verify/write only)")
    r.add_argument("--target", default="uncommitted", help="review target: uncommitted | commit:<sha> | base:<branch>")
    r.add_argument("--resume", help="follow up on a finished codex or claude task in the same session")
    r.add_argument("--slug", help="short name for the task directory")
    r.add_argument("--view", choices=("auto", "tab", "hidden"), help="visible terminal or hidden (default from config)")
    r.add_argument("--hold", action=argparse.BooleanOptionalAction, default=None,
                   help="keep the terminal tab open after the task ends")
    r.add_argument("--max-minutes", type=float, help="kill the agent after this long")
    r.add_argument("--wait", action="store_true", help="block until done, then print the final answer")
    r.add_argument("--timeout", type=float, default=3600, help="seconds --wait waits (default 3600)")
    r.add_argument("--panel", type=int, nargs="?", const=2, default=None, metavar="N",
                   help="send the task to N agents (default 2) from different model families at once")
    r.add_argument("--retry-of", help=argparse.SUPPRESS)  # internal: the runner's retry after a usage limit
    r.set_defaults(func=cmd_run)

    ck = sub.add_parser("check", help="fact-check a finished run: cited URLs and package versions")
    ck.add_argument("dir")
    ck.set_defaults(func=cmd_check)
    sub.add_parser("quota", help="usage-limit state crew has seen for each backend").set_defaults(func=cmd_quota)

    c = sub.add_parser("collect", help="wait for / reprint a task")
    c.add_argument("dir")
    c.add_argument("--timeout", type=float, default=3600)
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("status", help="recent tasks and their state")
    s.add_argument("--last", type=int, default=10)
    s.set_defaults(func=cmd_status)

    sub.add_parser("stats", help="usage per backend and model").set_defaults(func=cmd_stats)
    ro = sub.add_parser("roles", help="show roles, aliases and which agent each resolves to")
    ro.add_argument("--caller", choices=callers)
    ro.set_defaults(func=cmd_roles)
    sub.add_parser("models", help="list the models each installed backend offers").set_defaults(func=cmd_models)

    sr = sub.add_parser("set-role", help="choose the agent and default scope for a role (saved in your config)")
    sr.add_argument("role")
    sr.add_argument("--agent", nargs="+", help="alias or backend:model[@effort]; several = fallback list")
    sr.add_argument("--access", choices=ACCESS_LEVELS)
    sr.add_argument("--web", action=argparse.BooleanOptionalAction, default=None)
    sr.add_argument("--network", action=argparse.BooleanOptionalAction, default=None)
    sr.add_argument("--effort")
    sr.add_argument("--about", help="one-line description")
    sr.add_argument("--reset", action="store_true", help="drop your override for this role")
    sr.set_defaults(func=cmd_set_role)

    sa = sub.add_parser("set-agent", help="define an alias: one spec, or several as a fallback list")
    sa.add_argument("name")
    sa.add_argument("refs", nargs="*")
    sa.add_argument("--remove", action="store_true")
    sa.set_defaults(func=cmd_set_agent)

    sub.add_parser("doctor", help="check installation, logins, models and permissions").set_defaults(func=cmd_doctor)
    sub.add_parser("setup-agy-web", help=f"add {AGY_FETCH_RULE} to agy's settings (backs the file up first)"
                   ).set_defaults(func=cmd_setup_agy_web)

    rr = sub.add_parser("_run")  # internal: the per-task runner
    rr.add_argument("--dir", required=True)
    rr.add_argument("--hold", action="store_true")
    rr.set_defaults(func=cmd_runner)
    return p


def main(argv=None) -> int:
    use_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CrewError as e:
        print(f"crew: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
