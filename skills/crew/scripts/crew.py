#!/usr/bin/env python3
"""crew: hand bounded tasks to other vendors' coding-agent CLIs.

Backends: OpenAI Codex CLI (`codex`), Anthropic Claude Code (`claude`), Google
Antigravity CLI (`agy`), OpenCode (`opencode`, any provider it supports), direct
OpenAI-compatible APIs (`api`: OpenRouter, DeepSeek, Groq, Ollama, ...) and any
command-line agent defined in the config. Claude Code and Codex can also be routed
to other providers (`claude+openrouter:...`). The caller chooses each task's permission scope
(--access, --web, --network); crew maps it onto the backend's own controls,
runs the agent in its own process (optionally in a visible terminal tab) and
leaves a record of the run on disk.

Standard library only, Python 3.9+. Start with `crew.py doctor`.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_DIR / "assets" / "config.default.json"
CREW_HOME = Path(os.environ.get("CREW_HOME") or (Path.home() / ".crew")).expanduser()
USER_CONFIG = Path(os.environ.get("CREW_CONFIG") or (CREW_HOME / "config.json")).expanduser()
RUNS_DIR = CREW_HOME / "runs"
LEDGER = CREW_HOME / "ledger.jsonl"

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
BACKENDS = ("codex", "claude", "agy", "opencode", "api")  # built in; the config can define more
ACCESS_LEVELS = ("read", "verify", "write")
HEARTBEAT_SEC = 5
STALE_SEC = 30
STARTUP_GRACE_SEC = 90
TAB_CLAIM_SEC = 15
HASH_LIMIT = 64 * 1024 * 1024
AGY_INLINE_LIMIT = 8000
INLINE_PROMPT_LIMIT = AGY_INLINE_LIMIT  # longer prompts go by file, under the Windows command-line limit
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
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
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

# backend[+provider]:model[@effort]. The model may itself hold ':', '/' and '@' (ollama tags, opencode's
# provider/model ids, Vertex dates), so only the first ':' ends the backend and only a final
# all-letter '@word' is an effort.
SPEC_RE = re.compile(r"^([a-z][a-z0-9_-]*)(?:\+([a-z0-9][a-z0-9_.-]*))?:(\S+?)(?:@([a-z]+))?$")


def parse_spec(text):
    m = SPEC_RE.match(str(text).strip())
    if not m:
        return None
    spec = {"backend": m[1], "model": m[3], "effort": m[4]}
    if m[2]:
        spec["provider"] = m[2]
    return spec


def spec_str(spec: dict) -> str:
    return (f"{spec['backend']}" + (f"+{spec['provider']}" if spec.get("provider") else "")
            + f":{spec['model']}" + (f"@{spec['effort']}" if spec.get("effort") else ""))


def known_backends(cfg: dict) -> tuple:
    """Built-in backends plus command-line agents the config defines (backends.<name>.command)."""
    extra = [name for name, b in (cfg.get("backends") or {}).items()
             if name not in BACKENDS and isinstance(b, dict) and b.get("command")]
    return BACKENDS + tuple(extra)


def check_spec(cfg: dict, spec: dict, text) -> dict:
    """Raise for a spec whose backend or provider crew doesn't know."""
    backend, provider = spec["backend"], spec.get("provider")
    if backend not in known_backends(cfg):
        raise CrewError(f"Unknown backend '{backend}' in '{text}' (known: {', '.join(known_backends(cfg))}; "
                        "define others under backends.<name>.command)")
    if provider:
        if backend not in ("claude", "codex"):
            raise CrewError(f"'{text}': only claude and codex take +provider; "
                            f"{backend} names the provider in the model, e.g. {backend}:openrouter/<model>")
        if provider not in providers(cfg):
            raise CrewError(f"Unknown provider '{provider}' in '{text}' (known: {', '.join(providers(cfg))})")
    if backend == "api" and "/" not in spec["model"]:
        raise CrewError(f"'{text}': api agents are written api:<provider>/<model>, e.g. api:openrouter/<model>")
    return spec


def expand_agent(cfg: dict, ref, _seen=()) -> list:
    """Agent reference -> candidate specs in fallback order.

    A reference is a spec ('codex:gpt-6.1-sol@high'), an alias from
    cfg['agents'], or a list of either."""
    if isinstance(ref, list):
        return [s for r in ref for s in expand_agent(cfg, r, _seen)]
    spec = parse_spec(ref)
    if spec:
        return [check_spec(cfg, spec, ref)]
    agents = cfg.get("agents") or {}
    if ref in agents and ref not in _seen:
        return expand_agent(cfg, agents[ref], _seen + (ref,))
    raise CrewError(
        f"Unknown agent '{ref}'. Use backend[+provider]:model[@effort] (backend: {', '.join(known_backends(cfg))}) "
        f"or an alias: {', '.join(agents) or '(none defined)'}")


def backend_cmd(cfg: dict, backend: str):
    """argv prefix that launches a backend, or None if it is not installed."""
    if backend == "api":  # crew itself makes the request
        return [sys.executable, "-u", str(Path(__file__).resolve()), "_api"]
    bcfg = (cfg.get("backends") or {}).get(backend) or {}
    exe = bcfg.get("exe")
    if not exe and isinstance(bcfg.get("command"), list) and bcfg["command"]:
        exe = bcfg["command"][0]
    if isinstance(exe, list):
        return list(exe)
    name = os.path.expanduser(exe) if exe else backend
    found = name if Path(name).is_file() else shutil.which(name)
    if found and IS_WIN and found.lower().endswith((".cmd", ".bat")):
        return windows_shim_target(found) or [found]
    return [found] if found else None


def windows_shim_target(shim: str):
    """What an npm/pnpm .cmd shim runs, as an argv prefix, or None if it isn't recognised. Running
    the target directly keeps cmd.exe, which reinterprets &, |, % and newlines, out of the way.
    The shim's last line that passes %* on names the target as a quoted "%dp0%" path."""
    try:
        content = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = [line for line in content.splitlines() if "%*" in line]
    if not lines:
        return None
    paths = [m for m in re.findall(r'"(%~?dp0%?[^"]*)"', lines[-1], re.I) if not m.lower().endswith("node.exe")]
    if not paths:
        return None
    rel = re.sub(r"^%~?dp0%?\\?", "", paths[-1], flags=re.I).replace("\\", os.sep)
    target = os.path.normpath(os.path.join(os.path.dirname(shim), rel))
    if not Path(target).is_file():
        return None
    if target.lower().endswith((".exe", ".com")):
        return [target]
    if not re.search(r"node(\.exe)?\b", lines[-1], re.I) and "%_prog%" not in lines[-1].lower():
        return None  # run by something other than node
    local = Path(shim).with_name("node.exe")
    node = str(local) if local.is_file() else shutil.which("node")
    return [node, target] if node else None


# ------------------------------------------------------------------ providers

# Providers are model APIs. `api` agents call them directly (api:<provider>/<model>); claude and
# codex agents can be pointed at them (claude+<provider>:<model>) when the provider offers an
# Anthropic-compatible (anthropic_url) or Responses-compatible (responses) endpoint. opencode
# manages its own providers; its model ids start with the provider (opencode:openrouter/...).
KEYS_FILE = CREW_HOME / "keys.json"


def providers(cfg: dict) -> dict:
    return {k: v for k, v in (cfg.get("providers") or {}).items() if isinstance(v, dict) and not k.startswith("_")}


def spec_provider(spec: dict):
    """The provider a spec runs on: +provider for claude/codex, the model's first segment for
    opencode and api, else None (the backend's own service)."""
    if spec.get("provider"):
        return spec["provider"]
    if spec["backend"] in ("opencode", "api") and "/" in spec["model"]:
        return spec["model"].split("/", 1)[0]
    return None


def api_model(spec: dict) -> str:
    """The model id sent to an api agent's provider (the part after 'provider/')."""
    return spec["model"].split("/", 1)[1] if "/" in spec["model"] else spec["model"]


def quota_key(spec: dict) -> str:
    """Usage limits belong to whoever bills the request: the provider when there is one."""
    return spec_provider(spec) or spec["backend"]


def read_keys() -> dict:
    try:
        data = read_json(KEYS_FILE)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_secret(var: str):
    """An API key: from the environment, else from ~/.crew/keys.json (`crew.py set-key`). The file
    matters for visible tabs, which may not inherit the caller's environment."""
    if not var:
        return None
    return os.environ.get(var) or read_keys().get(var) or None


def provider_unavailable(cfg: dict, spec: dict):
    """Why this spec's provider can't be used now, or None when it can."""
    name = spec_provider(spec)
    if not name or spec["backend"] == "opencode":  # opencode keeps its own provider logins
        return None
    pcfg = providers(cfg).get(name)
    if pcfg is None:
        return f"unknown provider '{name}'"
    allowed = pcfg.get("allowed_models")
    model = api_model(spec) if spec["backend"] == "api" else spec["model"]
    if allowed and model not in allowed:
        return f"model {model} is not in providers.{name}.allowed_models"
    if spec["backend"] == "api" and not pcfg.get("base_url"):
        return f"provider {name} has no OpenAI-compatible endpoint (base_url) for api agents"
    if spec["backend"] == "claude" and not pcfg.get("anthropic_url"):
        return f"provider {name} has no Anthropic-compatible endpoint (anthropic_url) for claude"
    if spec["backend"] == "codex" and not pcfg.get("responses"):
        return f"provider {name} has no Responses API for codex (set providers.{name}.responses if it does)"
    if pcfg.get("key_env") and not read_secret(pcfg["key_env"]):
        return f"no API key: set {pcfg['key_env']} or run `crew.py set-key {name}`"
    return None


def detect_caller():
    """Which agent is running crew: CREW_CALLER, else environment markers, else None."""
    explicit = os.environ.get("CREW_CALLER", "").strip()
    if explicit:
        return explicit if valid_caller(explicit) else None
    for backend, markers in CALLER_MARKERS.items():
        if any(os.environ.get(m) for m in markers):
            return backend
    if any(os.environ.get(m) for m in ("CODEX_MANAGED_BY_NPM", "CODEX_MANAGED_BY_BUN", "CODEX_MANAGED_BY_PNPM")):
        return "codex"
    return None


def valid_caller(caller: str) -> bool:
    """A caller is a backend name, or backend:model when the backend runs many families (opencode)."""
    return caller in CALLER_FAMILY or caller in BACKENDS or bool(parse_spec(caller))


def caller_family(caller):
    """The caller's model family, or None when it can't be known (e.g. opencode without a model)."""
    if not caller:
        return None
    spec = parse_spec(caller)
    if spec:
        return model_family(spec)
    return CALLER_FAMILY.get(caller)


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
    installed, missing = [], []
    for s in candidates:
        c = backend_cmd(cfg, s["backend"])
        why = None if c else f"{s['backend']} not installed"
        why = why or provider_unavailable(cfg, s)
        if why:
            missing.append(f"{spec_str(s)}: {why}")
        else:
            installed.append((s, c))
    if not installed:
        raise CrewError(
            f"No installed backend for agent '{ref}' ({'; '.join(missing)}). Run `crew.py doctor`.")
    if explicit:
        return [(installed[0][0], installed[0][1], None)]
    notes = []
    family = caller_family(caller)
    if family:
        others = [(s, c) for s, c in installed if model_family(s, cfg) != family]
        if others:
            installed = others
        else:
            notes.append(f"{family} is also the caller's model family; no other installed candidate for this role")
    if quota is None:
        quota = read_quota()
    ready = [(s, c) for s, c in installed if not quota_exhausted(_quota_entry(quota, quota_key(s)))]
    if ready:
        installed = ready
    else:
        notes.append("all candidates are near their usage limit")
    if balance:
        step = float(cfg.get("balance_step", 0.25))
        unknown = float(cfg.get("unknown_quota", 0.5))

        def bucket(item):
            score = quota_score(_quota_entry(quota, quota_key(item[0])))
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

# Model families. agy, opencode and api agents run many vendors' models, so their family comes
# from the model name; a Claude Code caller should not count an agy Claude model as a second vendor.
# opencode can run any family, so as a caller it passes its model: --caller opencode:<provider/model>.
CALLER_FAMILY = {"codex": "openai", "claude": "anthropic", "agy": "google"}
QUOTA_FILE = CREW_HOME / "quota.json"
CODEX_DAY_LIMIT = 400  # newest session-log day folders searched for a task's session
CODEX_WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}
LIMIT_RE = re.compile(
    r"hit your (?:\w+ )?usage limit|usage limit (?:reached|exceeded)"
    r"|\brate[ _-]?limit(?:ed)?\b|rate_limit_error|too many requests|quota exceeded|resource[ _]exhausted"
    r"|\b(?:status|http|error|code)\W{0,3}429\b|\b429\b\s*(?:too many|rate|quota)"
    r"|insufficient (?:credits|balance|quota)|\b402\b\s*payment required",
    re.IGNORECASE)


# Model-name prefixes and organisation names (as in OpenRouter or Hugging Face ids) -> family.
# A second opinion should come from a different family, whichever CLI or provider serves it.
MODEL_PREFIX_FAMILY = (
    ("claude", "anthropic"), ("gpt", "openai"), ("chatgpt", "openai"), ("codex", "openai"), ("o1", "openai"),
    ("o3", "openai"), ("o4", "openai"), ("gemini", "google"), ("gemma", "google"), ("deepseek", "deepseek"),
    ("qwen", "alibaba"), ("qwq", "alibaba"), ("kimi", "moonshot"), ("moonshot", "moonshot"), ("glm", "zhipu"),
    ("grok", "xai"), ("mistral", "mistral"), ("mixtral", "mistral"), ("codestral", "mistral"),
    ("devstral", "mistral"), ("magistral", "mistral"), ("ministral", "mistral"), ("pixtral", "mistral"),
    ("llama", "meta"), ("minimax", "minimax"), ("command", "cohere"), ("nemotron", "nvidia"),
    ("phi", "microsoft"), ("ernie", "baidu"), ("hunyuan", "tencent"), ("doubao", "bytedance"),
    ("seed", "bytedance"), ("mimo", "xiaomi"), ("step", "stepfun"), ("nova", "amazon"), ("granite", "ibm"),
    ("olmo", "allenai"), ("jamba", "ai21"), ("sonar", "perplexity"))
ORG_FAMILY = {
    "anthropic": "anthropic", "openai": "openai", "google": "google", "deepseek": "deepseek",
    "deepseek-ai": "deepseek", "qwen": "alibaba", "alibaba": "alibaba", "moonshotai": "moonshot",
    "moonshot": "moonshot", "z-ai": "zhipu", "zai": "zhipu", "zai-org": "zhipu", "thudm": "zhipu",
    "x-ai": "xai", "xai": "xai", "mistralai": "mistral", "mistral": "mistral", "meta-llama": "meta",
    "meta": "meta", "minimax": "minimax", "minimaxai": "minimax", "cohere": "cohere", "nvidia": "nvidia",
    "microsoft": "microsoft", "amazon": "amazon", "ibm-granite": "ibm", "perplexity": "perplexity",
    "baidu": "baidu", "tencent": "tencent", "bytedance": "bytedance", "bytedance-seed": "bytedance",
    "xiaomi": "xiaomi", "stepfun-ai": "stepfun", "allenai": "allenai", "ai21": "ai21"}


def family_from_name(model: str):
    """Family from a model id such as 'openrouter/anthropic/claude-x:free', or None if unknown."""
    segments = [seg.lstrip("~") for seg in model.lower().split("/") if seg]
    if not segments:
        return None
    name = re.split(r"[:#]", segments[-1])[0]  # drop ollama tags, OpenRouter variants, opencode efforts
    for prefix, family in MODEL_PREFIX_FAMILY:
        if re.match(re.escape(prefix) + r"(?![a-z])", name):
            return family
    for seg in reversed(segments[:-1]):
        if seg in ORG_FAMILY:
            return ORG_FAMILY[seg]
    return None


def model_family(spec: dict, cfg=None) -> str:
    """The model's maker. codex and claude on their own service are openai and anthropic; anything
    else (agy, opencode, api, a +provider route, config-defined CLIs) goes by the model name, then
    by the provider's or backend's configured family."""
    backend = spec["backend"]
    if backend in ("codex", "claude") and not spec.get("provider"):
        return {"codex": "openai", "claude": "anthropic"}[backend]
    found = family_from_name(spec["model"])
    if found:
        return found
    if backend == "agy":
        return "google"
    cfg = cfg if cfg is not None else load_config()
    provider = spec_provider(spec)
    pcfg = providers(cfg).get(provider) if provider else None
    bcfg = (cfg.get("backends") or {}).get(backend) or {}
    return (pcfg or {}).get("family") or bcfg.get("family") or provider or backend


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


def _num(value):
    """A float from a JSON value, or None for anything that isn't a number."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_quota(backend: str, windows: dict, source: str) -> None:
    """Replace a backend's usage windows, keeping any limit already recorded for it."""
    # A window without a reset time would otherwise count forever, and a backend that is never
    # chosen is never refreshed; give it an hour.
    later = time.time() + 3600
    windows = {str(name): {"used": _num(w.get("used")) or 0.0, "resets_at": _num(w.get("resets_at")) or later}
               for name, w in (windows or {}).items() if isinstance(w, dict)}
    quota = read_quota()
    entry = _quota_entry(quota, backend)
    entry.setdefault("limited_until", None)
    entry.update({"windows": windows, "updated": now_iso(), "source": source})
    quota[backend] = entry
    _save_quota(quota)


def record_limit(backend: str, resets_at=None, cooldown_minutes: float = 60) -> None:
    """The backend hit its limit: blocked until the reset time if known, else for a cooldown."""
    now = time.time()
    resets_at = _num(resets_at)
    until = resets_at if resets_at and now < resets_at < now + 31 * 86400 else now + cooldown_minutes * 60
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
    limited = _num(entry.get("limited_until"))
    if not windows and limited is None:
        return None
    scores = [0.0]  # a window whose reset time has passed no longer counts
    for w in windows.values():
        if not isinstance(w, dict):
            continue
        resets = _num(w.get("resets_at"))
        if resets is None or resets > now:
            scores.append(_num(w.get("used")) or 0.0)
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
    windows = {name: {"used": _num(w.get("utilization")) or 0.0, "resets_at": _num(w.get("resetsAt"))}
               for name, w in unified.items() if isinstance(w, dict) and _num(w.get("utilization")) is not None}
    if windows:
        return windows
    if info.get("status", "allowed") != "allowed":
        return {"limit": {"used": 1.0, "resets_at": _num(info.get("resetsAt"))}}
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
                    used = _num(w.get("used_percent")) if isinstance(w, dict) else None
                    if used is not None:
                        name = CODEX_WINDOW_NAMES.get(w.get("window_minutes"), f"{w.get('window_minutes')}_min")
                        windows[name] = {"used": used / 100, "resets_at": _num(w.get("resets_at"))}
                if windows:
                    found = windows
        return found
    except Exception:  # an unexpected log format must never fail the run it describes
        return None


def looks_rate_limited(text: str) -> bool:
    return bool(LIMIT_RE.search(text or ""))


def limit_reset_from_text(text: str):
    """Epoch reset time from text such as 'usage limit reached|1791458400', else None."""
    m = re.search(r"limit[^|\n]{0,80}\|\s*(\d{10})\b", text or "", re.I)
    if not m:
        return None
    when = float(m[1])
    return when if time.time() < when < time.time() + 31 * 86400 else None


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


def enforcement(backend: str, scope: dict, has_git: bool, agy_fetch: bool, bcfg=None) -> dict:
    """How each scope dimension is enforced. Raises for scopes a backend cannot honour.
    `bcfg` is the backend's config entry (needed for config-defined command backends)."""
    access, web, net = scope["access"], scope["web"], scope["network"]
    detect = "git before/after check" if has_git else "nothing (not a git repo, so changes cannot be detected)"
    if backend == "opencode":
        files = {"read": "opencode permissions: read/search tools only; edit and bash denied",
                 "verify": f"opencode permissions: bash allowed, edit denied; bash has no OS sandbox, so "
                           f"repository writes are limited by instructions + {detect}",
                 "write": "opencode permissions: edit and bash allowed"}[access]
        network = ("n/a (bash denied)" if access == "read"
                   else "not enforced (opencode has no sandbox): instructions only")
        return {"files": files, "web": f"opencode webfetch/websearch permissions {'allow' if web else 'deny'}",
                "network": network}
    if backend == "api":
        if access == "write":
            raise CrewError("api agents have no tools, so they cannot edit files. Use an opencode, codex or "
                            "claude agent for --access write.")
        if web:
            raise CrewError("api agents have no tools, so they cannot search the web. Use --no-web, or an "
                            "opencode, codex or claude agent.")
        files = "none needed: the model sees only the task text (no tools)" + (
            "; verify runs read-only, since it cannot build or test" if access == "verify" else "")
        return {"files": files, "web": "n/a (no tools)", "network": "n/a (no tools)"}
    if backend not in BACKENDS:  # a command-line agent defined in the config
        bcfg = bcfg or {}
        supported = bcfg.get("access") or ["read"]
        if access not in supported:
            raise CrewError(f"backend {backend} is configured for --access {' | '.join(supported)} only "
                            f"(backends.{backend}.access)")
        how = (bcfg.get("enforcement") or {}).get(access)
        files = how or f"instructions + {detect}"
        return {"files": files,
                "web": (bcfg.get("enforcement") or {}).get("web") or "instructions only",
                "network": "n/a (no shell)" if access == "read" else "not enforced: instructions only"}
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
    if backend == "api":
        lines.append("You have no tools: you cannot read files, run commands or browse. Work only from the "
                     "text of this task. Where a claim can't be settled from that text, say what would need "
                     "checking and how.")
        return "\n".join(lines)
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
    if backend not in ("agy", "api") and access != "read":
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


SAFE_MCP_NAME = re.compile(r"^[^\s.=\"'\x00-\x1f](?:[^.=\"'\x00-\x1f]*[^\s.=\"'\x00-\x1f])?$")


def codex_isolation(cfg: dict, cmd: list):
    """(-c options, enforcement note) that keep the user's MCP servers and plugins out of a codex
    run. Codex runs MCP tools outside its shell sandbox, so a read-only task could still write
    through one. Plugins bring their own servers; each server still enabled is disabled by name."""
    opts = ["-c", "features.plugins=false"]
    if ((cfg.get("backends") or {}).get("codex") or {}).get("ignore_user_config"):
        return opts, "none: your codex config (and its MCP servers) is skipped"
    try:
        r = subprocess.run(cmd + ["mcp", "list", "--json"] + opts, capture_output=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        listed = json.loads(r.stdout.decode("utf-8", "replace")) if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        listed = None
    if not isinstance(listed, list):
        return opts, ("could not list your codex MCP servers (`codex mcp list --json`), so any you have stay "
                      "enabled and run outside codex's sandbox; set backends.codex.ignore_user_config to skip them")
    names = []
    for entry in listed:
        if not isinstance(entry, dict) or entry.get("enabled") is not True or not isinstance(entry.get("name"), str):
            continue
        if not SAFE_MCP_NAME.match(entry["name"]):
            raise CrewError(f"codex MCP server {entry['name'][:80]!r} can't be disabled safely by name; "
                            "rename or disable it, or set backends.codex.ignore_user_config")
        names.append(entry["name"])
    for name in names:
        opts += ["-c", f"mcp_servers.{name}.enabled=false"]
    return opts, (f"your {len(names)} codex MCP server(s) disabled for this run" if names
                  else "none enabled in your codex config") + "; plugins off"


def codex_args(cfg, spec, effort, scope, task: Path, cwd: Path, mode: str, target=None, session=None,
               extra_write=(), isolation=()) -> list:
    opts = ["--json", "--skip-git-repo-check", "-m", spec["model"], "-o", str(task / "final.md")]
    opts += list(isolation)
    if ((cfg.get("backends") or {}).get("codex") or {}).get("ignore_user_config"):
        opts.append("--ignore-user-config")
        ws = codex_windows_sandbox()
        if ws:
            opts += ["-c", f"windows.sandbox={toml_str(ws)}"]
    if effort:
        opts += ["-c", f"model_reasoning_effort={toml_str(effort)}"]
    provider = spec.get("provider")
    if provider:  # codex on another provider: a model_providers entry built from crew's providers table
        pcfg, pid = providers(cfg)[provider], f"crew-{provider}"
        opts += ["-c", f"model_provider={toml_str(pid)}",
                 "-c", f"model_providers.{pid}.name={toml_str(provider)}",
                 "-c", f"model_providers.{pid}.base_url={toml_str(pcfg['base_url'])}",
                 "-c", f"model_providers.{pid}.wire_api={toml_str('responses')}"]
        if pcfg.get("key_env"):
            opts += ["-c", f"model_providers.{pid}.env_key={toml_str(pcfg['key_env'])}"]
    opts += ["-c", f"web_search={toml_str('live' if scope['web'] else 'disabled')}"]
    if mode == "resume":  # resume inherits the original session's sandbox and root
        return ["exec", "resume"] + opts + [session, "-"]
    sandbox = "read-only" if scope["access"] == "read" else "workspace-write"
    if sandbox == "workspace-write":
        opts += ["-c", f"sandbox_workspace_write.network_access={'true' if scope['network'] else 'false'}",
                 "-c", "sandbox_workspace_write.writable_roots=["
                       + ", ".join(toml_str(r) for r in [task / "scratch", *extra_write]) + "]"]
    if mode == "builtin-review":  # `exec review` has no -s or -C: sandbox via config, cwd via the process
        kind, ref = parse_target(target)
        flags = {"uncommitted": ["--uncommitted"], "commit": ["--commit", ref or ""], "base": ["--base", ref or ""]}[kind]
        return ["exec", "review"] + opts + ["-c", f"sandbox_mode={toml_str(sandbox)}"] + flags
    return ["exec"] + opts + ["-s", sandbox, "-C", str(cwd), "-"]


def claude_args(cmd, spec, effort, scope, task: Path, top, cwd: Path, role_cfg: dict, session=None,
                extra_write=()) -> list:
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
            fs = {"allowWrite": [str(scratch)] + [str(p) for p in extra_write]}
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


def claude_provider_env(cfg: dict, spec: dict):
    """(plain env, secret env) that point claude at a provider's Anthropic-compatible endpoint.
    Secret env maps a variable to the key's source variable; it is resolved when the agent starts,
    so the key is never written into cmd.json."""
    pcfg = providers(cfg)[spec["provider"]]
    model = spec["model"]
    env = {"ANTHROPIC_BASE_URL": pcfg["anthropic_url"], "ANTHROPIC_API_KEY": "",
           # Claude Code's helper and subagent models would otherwise ask the provider for Anthropic ids.
           "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
           "ANTHROPIC_DEFAULT_SONNET_MODEL": model, "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
           "CLAUDE_CODE_SUBAGENT_MODEL": model}
    # Most Anthropic-compatible endpoints take the key as a bearer token (ANTHROPIC_AUTH_TOKEN); some
    # want it as ANTHROPIC_API_KEY instead (providers.<name>.auth_env).
    auth = pcfg.get("auth_env") or "ANTHROPIC_AUTH_TOKEN"
    if auth != "ANTHROPIC_AUTH_TOKEN":
        env.pop("ANTHROPIC_API_KEY", None)
        env["ANTHROPIC_AUTH_TOKEN"] = None  # None removes it from the agent's environment
    secret = {}
    if pcfg.get("key_env"):
        secret[auth] = pcfg["key_env"]
    else:
        env[auth] = spec["provider"]  # local servers want some token and ignore it
    return env, secret


def opencode_config(scope: dict) -> dict:
    """A per-task opencode config: the permission rules are the scope. '*' denies every tool not
    named here (MCP tools and subagents included), so nothing is left to an 'ask' prompt that a
    headless run cannot answer."""
    access, web = scope["access"], scope["web"]
    yes = lambda on: "allow" if on else "deny"  # noqa: E731
    perm = {"*": "deny", "read": "allow", "glob": "allow", "grep": "allow", "list": "allow", "lsp": "allow",
            "todoread": "allow", "todowrite": "allow",
            "edit": yes(access == "write"), "bash": yes(access != "read"),
            "external_directory": yes(access != "read"),
            "webfetch": yes(web), "websearch": yes(web), "codesearch": yes(web)}
    return {"$schema": "https://opencode.ai/config.json", "permission": perm, "share": "disabled",
            "autoupdate": False}


_version_cache = {}


def cli_version(cmd: list):
    """(major, minor) from `<cmd> --version`, or None if it can't be read."""
    key = tuple(cmd)
    if key not in _version_cache:
        try:
            r = subprocess.run(cmd + ["--version"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            m = re.search(r"(\d+)\.(\d+)", (r.stdout + r.stderr).decode("utf-8", "replace"))
            _version_cache[key] = (int(m[1]), int(m[2])) if m else None
        except (OSError, subprocess.TimeoutExpired):
            _version_cache[key] = None
    return _version_cache[key]


def opencode_args(cmd: list, spec, effort, session=None):
    """argv tail for `opencode run`, plus a note when the effort can't be passed. The prompt goes
    on stdin, which avoids quoting problems and Windows' cmd.exe.

    OpenCode 2 runs sessions through a shared background server that owns configuration and
    permissions, so a run's own environment (crew's permission config) is ignored there unless
    the run gets a private server: --standalone. It also takes effort as a model variant
    (provider/model#effort) instead of 1.x's --variant flag."""
    version = cli_version(cmd)
    v2 = bool(version and version[0] >= 2)
    model, note = spec["model"], None
    args = ["run"]
    if v2:
        args.append("--standalone")
        if effort and "#" not in model:
            model = f"{model}#{effort}"
    args += ["--format", "json", "-m", model]
    if effort and not v2:
        if "--variant" in cli_help(cmd + ["run"]):
            args += ["--variant", effort]
        else:
            note = f"this opencode has no --variant flag, so effort={effort} was not passed"
    if session:
        args += ["-s", session]
    return args, note


def command_args(bcfg: dict, spec, effort, scope, task: Path, cwd: Path, text: str):
    """argv tail and stdin use for a command-line agent defined in the config.

    backends.<name>.command is an argv template; {model}, {prompt}, {task_file}, {cwd} and {scratch}
    are filled in. Without {prompt} or {task_file} the prompt goes to stdin. effort_args, access_args
    (per access level) and web_args are added when they apply."""
    prompt = text
    if len(text) > INLINE_PROMPT_LIMIT:
        prompt = (f"Your full instructions are in the file {task / 'task.md'} . "
                  "Read that file completely and follow it exactly.")
    values = {"model": spec["model"], "effort": effort or "", "prompt": prompt,
              "task_file": str(task / "task.md"), "cwd": str(cwd), "scratch": str(task / "scratch")}

    def fill(items):
        out = []
        for item in items or []:
            item = str(item)
            for key, value in values.items():
                item = item.replace("{" + key + "}", value)
            out.append(item)
        return out

    template = list(bcfg["command"][1:])
    if effort:
        template += list(bcfg.get("effort_args") or [])
    template += list((bcfg.get("access_args") or {}).get(scope["access"]) or [])
    if scope["web"]:
        template += list(bcfg.get("web_args") or [])
    use_stdin = not any("{prompt}" in str(t) or "{task_file}" in str(t) for t in template)
    return fill(template), use_stdin


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
    wt = r.get("worktree")
    if wt and wt.get("kept"):
        print(f"crew: worktree {wt['path']} (branch {wt['branch']}); merge or cherry-pick it, then "
              f"`git worktree remove {wt['path']}`")
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


def caller_arg(text: str) -> str:
    if text == "none" or valid_caller(text):
        return text
    raise argparse.ArgumentTypeError(f"'{text}' is not a caller: use codex, claude, agy, opencode:<provider/model> "
                                     "(any backend:model), or none")


def resolve_caller(arg):
    if arg == "none":
        return None
    return arg or detect_caller()


REQUEST_KEYS = ("role", "agent", "model", "effort", "cd", "access", "web", "network", "target", "slug",
                "max_minutes", "caller", "view", "hold", "timeout", "worktree", "max_cost", "stall_minutes")


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
        excluded = list(req.get("excluded") or []) + [prev.get("quota_key") or prev["backend"]]
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
    if args.panel and args.model:
        raise CrewError("--model names one backend's model, so it can't apply to a panel; use set-role or --agent")

    if args.agent:  # an explicit choice is always honoured, so it is never retried elsewhere
        options = [choose_agent(cfg, args.agent, caller, explicit=True)]
    else:
        options = [o for o in candidates_for(cfg, role_cfg.get("agent"), caller, balance=bool(role_cfg.get("balance")))
                   if quota_key(o[0]) not in excluded]
        if not options:
            raise CrewError(f"no eligible agent for role '{args.role}' outside {', '.join(excluded)}")

    if args.panel:
        return run_panel(cfg, args, role_cfg, options, cwd, scope, text, caller, request)
    spec, exe, note = options[0]
    if retry_of and args.model and spec["backend"] != req.get("model_backend"):
        args.model = None  # that model name belongs to the backend that hit its limit
    request.update(model=args.model, model_backend=spec["backend"] if args.model else None)
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
    if pc.get("backend") not in ("codex", "claude", "opencode", "api"):
        raise CrewError("--resume works for codex, claude, opencode and api tasks only")
    session = pr.get("session_id")
    if not session:
        raise CrewError(f"No session id recorded in {prev / 'result.json'}")
    if not text:
        raise CrewError("--resume needs the follow-up instructions (--task or --task-file)")
    spec = parse_spec(pc["agent"])
    exe = backend_cmd(cfg, spec["backend"])
    if not exe:
        raise CrewError(f"{spec['backend']} is not installed (or backends.{spec['backend']}.exe is wrong)")
    if not Path(pc["cwd"]).is_dir():
        raise CrewError(f"{pc['cwd']} no longer exists (a worktree is removed when its run changed nothing)")
    role = pc["role"]
    role_cfg = (cfg.get("roles") or {}).get(role) or {}
    # Results written before session_cost_usd existed stored the session total in cost_usd.
    # Only claude reports a whole-session cost; opencode and api report each run's own.
    prior_cost = pr.get("session_cost_usd", pr.get("cost_usd")) if pc.get("backend") == "claude" else None
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
        family = model_family(spec, cfg)
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
        check_spec(cfg, spec, spec_str(spec))
        why = provider_unavailable(cfg, spec)
        if why:
            raise CrewError(f"{spec_str(spec)}: {why}")
    request_text = text
    backend = spec["backend"]
    effort = args.effort or spec.get("effort") or role_cfg.get("effort")
    top = git_top(cwd)
    agy_fetch = backend == "agy" and scope["web"] and agy_fetch_allowed(cfg)
    bcfg = (cfg.get("backends") or {}).get(backend) or {}
    enf = enforcement(backend, scope, bool(top), agy_fetch, bcfg)

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
    worktree = None
    if getattr(args, "worktree", False) and not session:
        if not top:
            shutil.rmtree(task, ignore_errors=True)
            raise CrewError("--worktree needs a git repository (--cd inside one)")
        if is_review:
            shutil.rmtree(task, ignore_errors=True)
            raise CrewError("--worktree doesn't apply to reviews: a new worktree holds only committed files")
        worktree = make_worktree(top, task, cwd)
        cwd, top = Path(worktree["cwd"]), worktree["path"]
    if builtin_review:
        sent = f"(built-in codex review of target: {args.target}; no prompt is sent)"
    elif session:
        sent = text  # the session already holds the original scope and instructions
    else:
        parts = [scope_preamble(backend, scope, scratch, top, agy_fetch), role_cfg.get("preamble") or "", text]
        sent = "\n\n".join(p for p in parts if p)
    (task / "task.md").write_text(sent, encoding="utf-8")
    (task / "request.md").write_text(request_text, encoding="utf-8")

    role_env = role_cfg.get("env") or {}
    cache = shared_cache(top, cwd) if any("{cache}" in str(v) for v in role_env.values()) else None
    extra_write = [cache] if cache and scope["access"] != "read" else []
    for folder in extra_write:
        folder.mkdir(parents=True, exist_ok=True)
    provider = spec_provider(spec)
    pcfg = providers(cfg).get(provider) or {} if provider else {}
    backend_env, secret_env, effort_note, api_cfg = {}, {}, None, None
    if backend == "codex":
        mode = "resume" if session else ("builtin-review" if builtin_review else "exec")
        isolation, enf["mcp"] = codex_isolation(cfg, exe)
        argv = exe + codex_args(cfg, spec, effort, scope, task, cwd, mode, args.target, session, extra_write,
                                isolation)
        use_stdin = not builtin_review
        if provider and pcfg.get("key_env"):
            secret_env[pcfg["key_env"]] = pcfg["key_env"]  # from ~/.crew/keys.json when not in the environment
    elif backend == "claude":
        argv = exe + claude_args(exe, spec, effort, scope, task, top, cwd, role_cfg, session, extra_write)
        use_stdin = True
        enf["mcp"] = "none (--strict-mcp-config with no servers)"
        if provider:
            backend_env, secret_env = claude_provider_env(cfg, spec)
    elif backend == "agy":
        argv = exe + agy_args(spec, effort, scope, task, cwd, sent)
        use_stdin = False
    elif backend == "opencode":
        tail, effort_note = opencode_args(exe, spec, effort, session)
        argv, use_stdin = exe + tail, True
        oc_cfg = opencode_config(scope)
        write_json(task / "opencode-config.json", oc_cfg)
        enf["mcp"] = "denied: crew's permission rules start from '*': deny"
        if (cli_version(exe) or (0,))[0] >= 2:
            enf["files"] += " (on a private --standalone server, so these rules apply)"
        # Inline config outranks the user's global and project opencode configs. 1.x also reads
        # OPENCODE_PERMISSION; 2.x reads only the inline config (and only on a private server).
        backend_env = {"OPENCODE_CONFIG_CONTENT": json.dumps(oc_cfg),
                       "OPENCODE_PERMISSION": json.dumps(oc_cfg["permission"])}
        if bcfg.get("private_db"):  # keep crew's sessions out of the user's own opencode history
            prior = None
            if session and getattr(args, "resume", None):
                try:
                    prior = read_json(Path(args.resume).expanduser() / "cmd.json").get("opencode_db")
                except (OSError, ValueError):
                    prior = None
            opencode_db = prior or str(task / "opencode.db")
            backend_env["OPENCODE_DB"] = opencode_db
    elif backend == "api":
        argv, use_stdin = exe + ["--dir", str(task)], False
        api_cfg = {"provider": provider, "base_url": pcfg["base_url"], "key_env": pcfg.get("key_env"),
                   "model": api_model(spec), "effort": effort, "effort_style": pcfg.get("effort_style"),
                   "body": pcfg.get("body") or {}, "headers": pcfg.get("headers") or {},
                   "quota": pcfg.get("quota"), "timeout": pcfg.get("timeout", 600),
                   "history": str(Path(session) / "conversation.json") if session else None}
    else:
        tail, use_stdin = command_args(bcfg, spec, effort, scope, task, cwd, sent)
        argv = exe + tail

    env = {"CREW_SCRATCH": str(scratch), NESTED_MARKER: str(task)}
    env.update(backend_env)
    if scope["access"] != "read":
        env.update({"TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch)})
    if scope["access"] == "verify":
        env["PYTHONDONTWRITEBYTECODE"] = "1"  # keeps __pycache__ from tripping the violation check
    for key, value in role_env.items():
        env[key] = (str(value).replace("{cwd}", str(cwd)).replace("{scratch}", str(scratch))
                    .replace("{task}", str(task)).replace("{cache}", str(cache)))

    allowed = []
    if top:
        for p in role_cfg.get("scratch_paths") or []:
            rel = Path(os.path.relpath(cwd / p, top)).as_posix()
            allowed.append(rel.rstrip("/") + "/" if p.endswith("/") else rel)
    max_minutes = args.max_minutes or role_cfg.get("max_minutes") or cfg.get("max_minutes") or 60
    stall_minutes = getattr(args, "stall_minutes", None) or role_cfg.get("stall_minutes") or cfg.get("stall_minutes")
    if stall_minutes and backend == "agy":
        stall_minutes = None  # agy prints nothing until it has finished, so silence isn't a stall
    max_cost = getattr(args, "max_cost", None) or role_cfg.get("max_cost_usd") or cfg.get("max_cost_usd")
    if max_cost:
        if backend == "claude" and "--max-budget-usd" in cli_help(exe):
            argv += ["--max-budget-usd", str(max_cost)]
            enf["cost"] = f"claude --max-budget-usd {max_cost} (checked between turns, so one turn can overshoot)"
        else:
            enf["cost"] = f"not enforced by {backend}: crew reports a run that cost more than ${max_cost} afterwards"
    agent_label = spec_str(dict(spec, effort=effort))
    view = args.view or cfg.get("view", "auto")
    hold = cfg.get("hold", True) if args.hold is None else args.hold

    cmd = {"role": role, "agent": agent_label, "backend": backend, "model": spec["model"], "effort": effort,
           "family": model_family(spec, cfg), "caller": caller, "caller_session": caller_session(), "scope": scope,
           "enforcement": enf, "cwd": str(cwd), "git_top": top, "argv": argv, "stdin": use_stdin, "env": env,
           "allowed_paths": allowed, "max_minutes": max_minutes, "resumed_from": None,
           "prior_session_cost_usd": None, "fact_check": bool(scope["web"] and cfg.get("fact_check", True)),
           "prune_scratch": bool(cfg.get("prune_scratch", True)), "cache": str(cache) if cache else None,
           "view": view, "hold": hold, "created": now_iso(), "provider": provider, "quota_key": quota_key(spec),
           "secret_env": secret_env, "api": api_cfg, "effort_note": effort_note,
           "opencode_db": backend_env.get("OPENCODE_DB"), "worktree": worktree,
           "stall_minutes": stall_minutes, "max_cost_usd": max_cost}
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
        launch_hidden(runner, task)
        how = "hidden background process"
    print(f"crew: launched {role} -> {agent_label} in {how}")
    if caller:
        print(f"crew: caller={caller}" + (f" ({choice_note})" if choice_note else ""))
    if effort_note:
        print(f"crew: note: {effort_note}")
    print(f"crew: scope access={scope['access']} web={on_off(scope['web'])} network={on_off(scope['network'])}")
    for dim, how_enforced in enf.items():
        print(f"crew:   {dim}: {how_enforced}")
    if worktree:
        print(f"crew: worktree {worktree['path']} on branch {worktree['branch']} (from HEAD: uncommitted "
              "changes in your checkout are not in it)")
    print(f"crew: dir={task}")
    return task


def make_worktree(top: str, task: Path, cwd: Path) -> dict:
    """A git worktree for one run, at <run folder>/worktree on branch crew/<run name>. It starts
    from HEAD, so parallel write tasks can't trip over each other or over your own edits."""
    path, branch = task / "worktree", f"crew/{task.name}"
    r = subprocess.run(["git", "-C", top, "worktree", "add", "-q", "-b", branch, str(path), "HEAD"],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        shutil.rmtree(task, ignore_errors=True)
        raise CrewError("could not create a worktree: " + r.stderr.decode("utf-8", "replace").strip())
    rel = os.path.relpath(str(cwd), top)
    return {"path": str(path), "branch": branch, "repo": top,
            "cwd": str(path if rel == "." else path / rel)}


def finish_worktree(wt: dict, touched: list) -> dict:
    """Keep a run's worktree when the agent changed something; otherwise remove it and its branch."""
    if touched:
        return dict(wt, kept=True)
    for args in (["worktree", "remove", "--force", wt["path"]], ["branch", "-D", wt["branch"]]):
        subprocess.run(["git", "-C", wt["repo"], *args], capture_output=True, timeout=120)
    return dict(wt, kept=False)


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


def cmd_prune(args) -> int:
    """Remove build output from finished runs' scratch folders; the run records are kept."""
    if args.cache:
        cache = CREW_HOME / "cache"
        if args.dry_run:
            print(f"crew: would remove the shared build caches in {cache}")
            return 0
        freed, errors = _remove(str(cache)) if cache.exists() else (0, 0)
        print(f"crew: removed {human_bytes(freed)} of shared build caches" + (f", {errors} error(s)" if errors else ""))
        return 0 if not errors else 2
    dirs = [Path(d).expanduser().resolve() for d in args.dirs] if args.dirs else \
        sorted(d for d in RUNS_DIR.iterdir() if d.is_dir()) if RUNS_DIR.exists() else []
    total = total_errors = 0
    for task in dirs:
        if not (task / "scratch").is_dir():
            continue
        if not (task / "result.json").exists():  # a finished runner may still hold its tab open; that's fine
            print(f"  {task.name}: still running, skipped")
            continue
        freed, errors = prune_scratch(task / "scratch", dry_run=args.dry_run)
        if args.dry_run:
            if freed:
                print(f"  {task.name}: about {human_bytes(freed)} of build output")
            total += freed
            continue
        if freed or errors:
            print(f"  {task.name}: removed {human_bytes(freed)}" + (f", {errors} error(s)" if errors else ""))
        total, total_errors = total + freed, total_errors + errors
    verb = "would remove about" if args.dry_run else "removed"
    print(f"crew: {verb} {human_bytes(total)} of build output; prompts, answers and logs are kept"
          + (f" ({total_errors} item(s) could not be removed)" if total_errors else ""))
    return 0 if not total_errors else 2


def cmd_quota(args) -> int:
    state = read_quota()
    if not state:
        print("crew: no usage-limit data yet (claude and codex runs report it, and OpenRouter keys with a "
              "credit limit; others only when they hit a limit)")
        return 0
    now = time.time()
    for backend, entry in sorted(state.items()):
        score = quota_score(entry, now)
        line = f"{backend:<11} {'?' if score is None else f'{score:.0%} used'}"
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
        if (d / "panel.json").exists():
            try:
                print(f"{d.name:<48} {'panel':<16} {len(read_json(d / 'panel.json').get('members') or [])} runs")
            except (OSError, ValueError):
                print(f"{d.name:<48} panel")
            continue
        if rp.exists():
            r = read_json(rp)
            print(f"{d.name:<48} {str(r.get('status')):<16} {str(r.get('agent')):<32} {r.get('seconds')}s")
        else:
            state = {"alive": "running", "dead": "dead (runner gone, no result)"}.get(runner_state(d), "starting?")
            if state == "running":
                try:
                    quiet = time.time() - float(read_json(d / "run.json").get("last_output"))
                    if quiet >= 60:
                        state += f", no output for {quiet / 60:.0f} min"
                except (OSError, ValueError, TypeError):
                    pass
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
    print("\ncost is the price the agent or provider reports (claude, opencode, and api providers that "
          "report it, e.g. OpenRouter; codex and agy report none). On a subscription it measures usage, not money. "
          "'?' = the CLI had no price for that model.")
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
    if getattr(args, "changes", False):
        return model_changes(cfg)
    if args.provider:
        for model in provider_models(cfg, args.provider):
            print(f"api:{args.provider}/{model}")
        return 0
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
    print("\nopencode (from `opencode models`; connect providers with `opencode auth login`):")
    om = opencode_models(cfg)
    if om is None:
        print("  (opencode not installed or not responding)")
    elif not om:
        print("  (no models: connect a provider first)")
    for m in om or []:
        print(f"  opencode:{m}")
    print("\nproviders (api:<provider>/<model>, claude+<provider>:<model>, codex+<provider>:<model>;\n"
          "  `crew.py models --provider <name>` lists a provider's models):")
    for name, pcfg in providers(cfg).items():
        print(f"  {name:<12} {provider_status(cfg, name, pcfg)}")
    for name in known_backends(cfg)[len(BACKENDS):]:
        print(f"\n{name} (command-line agent defined in your config): {'installed' if backend_cmd(cfg, name) else 'NOT INSTALLED'}")
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
    for backend in known_backends(cfg):
        if backend == "api":
            continue
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
        if backend == "opencode":
            run_help = cli_help(cmd + ["run"])
            report(True if "--format" in run_help else None, "opencode run --format json",
                   "supported" if "--format" in run_help else "not found in `opencode run --help`; update opencode")
            known = set(opencode_models(cfg) or [])
            report(True if known else None, "opencode models",
                   f"{len(known)} available" if known else "none listed: connect a provider with `opencode auth login`")
            for name, r in (cfg.get("roles") or {}).items():
                for spec in expand_agent(cfg, r.get("agent")):
                    if spec["backend"] == "opencode" and known and spec["model"] not in known:
                        report(None, f"role {name}", f"model {spec['model']} is not in `opencode models`")
    refs = list((cfg.get("agents") or {}).values()) + [r.get("agent") for r in (cfg.get("roles") or {}).values()]
    used = {spec_provider(spec) for ref in refs for spec in _safe_expand(cfg, ref) if spec["backend"] != "opencode"}
    unset = []
    for name, pcfg in providers(cfg).items():
        status = provider_status(cfg, name, pcfg)
        if status.startswith("no key (") and name not in used:
            unset.append(name)
            continue
        report(None if status.startswith("no key (") else True, f"provider {name}", status)
    if unset:
        report(True, "providers without a key", f"{', '.join(unset)} (add one with `crew.py set-key <provider>`)")
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
    if getattr(args, "ask", False):
        problems += ask_agents(cfg)
    print(f"\nconfig: {USER_CONFIG}{'' if USER_CONFIG.exists() else ' (not created; built-in defaults in use)'}")
    return 1 if problems else 0


def _safe_expand(cfg: dict, ref) -> list:
    try:
        return expand_agent(cfg, ref)
    except CrewError:
        return []


def ask_agents(cfg: dict) -> int:
    """Send every distinct, usable agent in the roles and aliases a one-word question, in parallel,
    through crew's normal run path. Returns the number that failed."""
    refs = [r.get("agent") for r in (cfg.get("roles") or {}).values()] + list((cfg.get("agents") or {}).values())
    specs = {}
    for ref in refs:
        for spec in _safe_expand(cfg, ref):
            if backend_cmd(cfg, spec["backend"]) and not provider_unavailable(cfg, spec):
                specs.setdefault(spec_str(spec), spec)
    roles = cfg.get("roles") or {}
    role = "light" if "light" in roles else next(iter(roles), None)
    if not specs or not role:
        print("crew: no usable agents to ask")
        return 0
    probe_dir = CREW_HOME / "doctor"
    probe_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nAsking {len(specs)} agent(s) a one-word question (this uses a little of each one's quota)...")

    def ask(label):
        argv = [sys.executable, str(Path(__file__).resolve()), "run", "--role", role, "--agent", label,
                "--caller", "none", "--access", "read", "--no-web", "--no-network", "--view", "hidden",
                "--no-hold", "--max-minutes", "5", "--timeout", "420", "--slug", "doctor-ask",
                "--cd", str(probe_dir), "--wait", "--task", "Reply with the single word OK and nothing else."]
        try:
            r = subprocess.run(argv, capture_output=True, timeout=480)
        except (OSError, subprocess.TimeoutExpired) as e:
            return label, False, str(e)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        status = re.search(r"^crew: (\S+) \|", out, re.M)
        answer = out.split("----- final answer -----", 1)[-1].strip() if "----- final answer -----" in out else ""
        if r.returncode == 0:
            return label, True, f"answered {short(answer, 40)!r}"
        why = status[1] if status else short(out.strip().splitlines()[-1] if out.strip() else "no output", 160)
        return label, False, why

    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for label, ok, detail in pool.map(ask, sorted(specs)):
            failed += not ok
            print(f"[{'ok  ' if ok else 'FAIL'}] ask {label}: {detail}")
    return failed


MODELS_SEEN = CREW_HOME / "models-seen.json"


def current_model_lists(cfg: dict) -> dict:
    """Model ids per source, from local catalogues and the providers you have keys for."""
    lists = {}
    cm = codex_models()
    if cm is not None:
        lists["codex"] = sorted(str(m.get("slug")) for m in cm if m.get("slug"))
    am = agy_models(cfg)
    if am is not None:
        lists["agy"] = sorted(m["slug"] for m in am)
    om = opencode_models(cfg)
    if om:
        lists["opencode"] = sorted(om)
    for name, pcfg in providers(cfg).items():
        if not pcfg.get("base_url") or (pcfg.get("key_env") and not read_secret(pcfg["key_env"])):
            continue
        try:
            lists[f"provider:{name}"] = provider_models(cfg, name)
        except CrewError:
            continue  # a local server that isn't running, or a provider without a model list
    return lists


def model_changes(cfg: dict) -> int:
    try:
        seen = read_json(MODELS_SEEN)
    except (OSError, ValueError):
        seen = {}
    lists = current_model_lists(cfg)
    if not seen:
        print(f"crew: recorded {sum(map(len, lists.values()))} models from {', '.join(lists) or 'no sources'}; "
              "later runs of `crew.py models --changes` report what was added or removed")
    changed = False
    for source, models in lists.items():
        if source not in seen:
            if seen:
                changed = True
                print(f"{source}: new source, {len(models)} models")
            continue
        for sign, diff in (("+", set(models) - set(seen[source])), ("-", set(seen[source]) - set(models))):
            for m in sorted(diff):
                changed = True
                print(f"{source}: {sign} {m}")
    if seen and not changed:
        print("crew: no model changes since the last check")
    write_json(MODELS_SEEN, dict(seen, **lists))
    return 0


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


def cmd_set_key(args) -> int:
    """Store or remove an API key in ~/.crew/keys.json (owner-only permissions). Keys in the
    environment take precedence; the file is for tabs that don't inherit your shell's variables."""
    cfg = load_config()
    pcfg = providers(cfg).get(args.name)
    var = (pcfg or {}).get("key_env") if pcfg else args.name
    if pcfg and not var:
        raise CrewError(f"provider {args.name} needs no key")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", var or ""):
        raise CrewError(f"'{args.name}' is neither a provider nor a variable name")
    keys = read_keys()
    if args.remove:
        keys.pop(var, None)
    else:
        if sys.stdin.isatty():
            import getpass
            value = getpass.getpass(f"{var}: ").strip()
        else:
            value = sys.stdin.readline().strip()
        if not value:
            raise CrewError("no key given")
        keys[var] = value
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_json(KEYS_FILE, keys)
    try:
        os.chmod(KEYS_FILE, 0o600)
    except OSError:
        pass
    print(f"crew: {var} {'removed from' if args.remove else 'saved in'} {KEYS_FILE}")
    return 0


def opencode_models(cfg: dict):
    """`provider/model` ids from `opencode models`, or None if opencode isn't installed or fails."""
    cmd = backend_cmd(cfg, "opencode")
    if not cmd:
        return None
    try:
        r = subprocess.run(cmd + ["models"], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = strip_ansi(r.stdout.decode("utf-8", "replace"))
    return [line.strip() for line in out.splitlines() if re.match(r"^[\w.@~-]+/\S+$", line.strip())]


def provider_models(cfg: dict, name: str) -> list:
    """Model ids from a provider's GET /models."""
    pcfg = providers(cfg).get(name)
    if pcfg is None:
        raise CrewError(f"Unknown provider '{name}' (known: {', '.join(providers(cfg))})")
    if not pcfg.get("base_url"):
        raise CrewError(f"provider {name} has no base_url to list models from")
    headers = {"User-Agent": "crew"}
    headers.update(pcfg.get("headers") or {})
    if pcfg.get("key_env"):
        key = read_secret(pcfg["key_env"])
        if not key:
            raise CrewError(f"no API key for {name}: set {pcfg['key_env']} or run `crew.py set-key {name}`")
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(pcfg["base_url"].rstrip("/") + "/models", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise CrewError(f"could not list {name} models: {e}")
    items = data.get("data") if isinstance(data, dict) else data
    return sorted(str(m.get("id")) for m in items or [] if isinstance(m, dict) and m.get("id"))


def provider_status(cfg: dict, name: str, pcfg: dict) -> str:
    uses = (["api"] if pcfg.get("base_url") else []) + (["claude+"] if pcfg.get("anthropic_url") else []) + (["codex+"] if pcfg.get("responses") else [])
    if pcfg.get("key_env"):
        key = "key set" if read_secret(pcfg["key_env"]) else f"no key ({pcfg['key_env']})"
    else:
        key = "no key needed"
    return f"{key}; usable by {', '.join(uses)}"


# ---------------------------------------------------------------- fact check

CHECK_UA = "crew-factcheck/1.1"
CHECK_GET_LIMIT = 64 * 1024
REGISTRY_READ_LIMIT = 16 * 1024 * 1024
REGISTRIES = {
    "crates": "https://crates.io/api/v1/crates/{name}",
    "pypi": "https://pypi.org/pypi/{name}/json",
    "npm": "https://registry.npmjs.org/{name}",
}
REGISTRY_ENV = {"crates": "CREW_REGISTRY_CRATES", "pypi": "CREW_REGISTRY_PYPI", "npm": "CREW_REGISTRY_NPM"}
URL_RE = re.compile(r"https?://[^\s<>\[\]\"'`]+", re.IGNORECASE)
URL_TRAIL = ".,;:'\">*"
PKG_NAME = r"[A-Za-z][A-Za-z0-9_.-]{1,60}"
PKG_VERSION = r"\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?(?:\+[0-9A-Za-z.]+)?"
# Stops "1.2.3.4" being read as "1.2.3" and "2.0-beta" being cut short.
VERSION_END = r"(?![\w.+-]*\w)"
INLINE_CLAIM_RES = [
    re.compile(rf"(?<![\w./@-])({PKG_NAME})@v?({PKG_VERSION}){VERSION_END}"),  # name@1.2.3
    re.compile(rf"`({PKG_NAME})`\s+v?({PKG_VERSION}){VERSION_END}"),  # `name` 1.2.3
    re.compile(rf"(?<![\w./@-])({PKG_NAME})\s+v({PKG_VERSION}){VERSION_END}"),  # name v1.2.3
    re.compile(rf"(?<![\w./@-])({PKG_NAME})\s+({PKG_VERSION}){VERSION_END}"),  # name 1.2.3
]
# Common English words and table headers that look like "word 1.2" but are not packages.
SKIP_NAMES = {
    "the", "and", "of", "is", "was", "in", "to", "a", "an", "for", "on", "or", "by", "as", "it",
    "version", "versions", "release", "releases", "latest", "python", "rust", "node", "step", "phase",
    "section", "table", "figure", "fig", "page", "chapter", "edition", "crate", "crates", "package",
    "packages", "name", "library", "libraries", "dependency", "dependencies", "date", "updated",
    "published",
}


def _clean_url(url: str) -> str:
    url = url.rstrip(URL_TRAIL)
    # Drop a closing bracket only when it is unbalanced: "[x](url)" ends with one, Wikipedia URLs don't.
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1].rstrip(URL_TRAIL)
    return url


def extract_urls(text: str) -> list:
    """http(s) URLs from Markdown links and bare text, first-seen order, no duplicates."""
    seen = {}
    for match in URL_RE.finditer(text):
        url = _clean_url(match.group(0))
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and parts.hostname:
            seen[url] = None
    return list(seen)


def _public_ip(text: str) -> bool:
    addr = ipaddress.ip_address(text)
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return not (addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved
                or addr.is_unspecified or addr.is_multicast)


def url_allowed(url: str) -> bool:
    """False for local and private addresses, including names that resolve to them. The answer
    being checked is model output, so a cited URL must not reach the user's own network."""
    if os.environ.get("CREW_CHECK_ALLOW_LOCAL") == "1":
        return True
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host or host == "localhost" \
            or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        return _public_ip(host)
    except ValueError:
        pass
    if re.fullmatch(r"[0-9a-fx.]+", host) and not re.search(r"[g-wyz]", host):
        return False  # 2130706433, 0x7f000001, 127.1: other spellings of an IP address
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return True  # unresolvable: the fetch fails and is reported as a dead link
    return all(_public_ip(info[4][0].split("%")[0]) for info in infos)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl):
            fp.close()
            raise urllib.error.URLError(f"redirect to a private or local address refused: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_CHECK_OPENER = urllib.request.build_opener(_CheckedRedirects)


def _probe(url: str, method: str, timeout: float):
    """(status, final_url). HTTP error statuses are returned, not raised."""
    # Some sites (crates.io among them) answer 404 unless the client accepts HTML.
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": CHECK_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        with _CHECK_OPENER.open(req, timeout=timeout) as resp:
            if method == "GET":
                resp.read(CHECK_GET_LIMIT)
            return resp.status, resp.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.geturl()


def check_url(url: str, timeout: float = 10.0) -> dict:
    """HEAD first, GET if HEAD is refused or fails. Never raises."""
    result = {"url": url, "status": None, "final_url": None, "ok": False, "error": None}
    try:
        try:
            status, final = _probe(url, "HEAD", timeout)
        except (OSError, ValueError):
            status = None
        if status is None or status in (403, 405, 501):
            status, final = _probe(url, "GET", timeout)
        result.update(status=status, final_url=final, ok=200 <= status < 400)
        if status in (401, 403, 429):  # the site refuses automated requests: unknown, not dead
            result.update(ok=None, error=f"blocked by the site (HTTP {status})")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def _skipped(url: str, reason: str) -> dict:
    return {"url": url, "status": None, "final_url": None, "ok": None, "error": reason}


def check_urls(urls: list, limit: int = 30, workers: int = 8) -> list:
    """Check the first `limit` allowed URLs concurrently. Results keep input order."""
    urls = list(urls)
    allowed = [u for u in urls if url_allowed(u)][:limit]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        checked = dict(zip(allowed, pool.map(check_url, allowed)))
    out = []
    for url in urls:
        if url in checked:
            out.append(checked[url])
        elif not url_allowed(url):
            out.append(_skipped(url, "skipped: private or local address"))
        else:
            out.append(_skipped(url, f"skipped: over limit of {limit}"))
    return out


def detect_ecosystems(text: str) -> list:
    low = text.lower()
    found = []
    if re.search(r"crates\.io|\bcrates?\b|\bcargo\b|cargo\.toml", low):
        found.append("crates")
    if re.search(r"pypi|pip3? install", low):
        found.append("pypi")
    if re.search(r"\bnpm|package\.json", low):
        found.append("npm")
    return found


def _plain(cell: str) -> str:
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell)  # keep link text, drop the URL
    return cell.replace("`", "").replace("*", "").strip()


def _table_claim(line: str):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) < 2 or all(re.fullmatch(r":?-+:?", c) for c in cells):
        return None
    name = _plain(cells[0]).rstrip(".-")
    if not re.fullmatch(PKG_NAME, name):
        return None
    for cell in cells[1:]:
        version = _plain(cell)
        if re.fullmatch(PKG_VERSION, version):
            return name, version
    return None


def _inline_claims(line: str) -> list:
    hits = []
    for pattern in INLINE_CLAIM_RES:
        for m in pattern.finditer(line):
            hits.append((m.start(), m.group(1).rstrip(".-"), m.group(2)))
    return [(name, version) for _, name, version in sorted(hits)]


def _looks_like_date(version: str) -> bool:
    return re.fullmatch(r"(19|20)\d\d\.\d{1,2}(\.\d{1,2})?", version) is not None


def extract_version_claims(text: str) -> list:
    """(name, version) pairs from table rows and inline mentions, first-seen order."""
    claims = {}
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            hit = _table_claim(line)
            pairs = [hit] if hit else []
        else:
            pairs = _inline_claims(line)
        for name, version in pairs:
            if name.lower() in SKIP_NAMES or len(name) < 2 or _looks_like_date(version):
                continue
            claims[(name, version)] = None
    return list(claims)


def registry_url(ecosystem: str, name: str) -> str:
    template = os.environ.get(REGISTRY_ENV[ecosystem]) or REGISTRIES[ecosystem]
    return template.replace("{name}", urllib.parse.quote(name, safe=""))


def _registry_facts(ecosystem: str, data: dict):
    """(set of published non-yanked versions, latest stable version or None)."""
    if ecosystem == "crates":
        crate = data.get("crate") or {}
        published = {v["num"] for v in data.get("versions", []) if not v.get("yanked")}
        return published, crate.get("max_stable_version") or crate.get("max_version")
    if ecosystem == "pypi":
        releases = data.get("releases") or {}
        published = {v for v, files in releases.items() if files and not all(f.get("yanked") for f in files)}
        return published, (data.get("info") or {}).get("version")
    published = set(data.get("versions") or {})
    return published, (data.get("dist-tags") or {}).get("latest")


def version_key(version: str):
    """Comparable form of a version: 2.0 and 2.0.0 are the same; build metadata is ignored."""
    main, _, pre = str(version).strip().lstrip("vV").split("+")[0].partition("-")
    nums = (main.split(".") + ["0", "0", "0"])[:max(3, len(main.split(".")))]
    return tuple(int(n) if n.isdigit() else n for n in nums), pre


def check_package(ecosystem: str, name: str, claimed: str, timeout: float = 8.0) -> dict:
    """Look a package version up in its registry. Never raises."""
    result = {"ecosystem": ecosystem, "name": name, "claimed": claimed,
              "found": None, "exists": None, "latest": None, "error": None}
    # npm's abbreviated document is a fraction of the full one, which can run to megabytes.
    accept = "application/vnd.npm.install-v1+json" if ecosystem == "npm" else "application/json"
    try:
        req = urllib.request.Request(registry_url(ecosystem, name), headers={"User-Agent": CHECK_UA, "Accept": accept})
        with _CHECK_OPENER.open(req, timeout=timeout) as resp:
            body = resp.read(REGISTRY_READ_LIMIT + 1)
        if len(body) > REGISTRY_READ_LIMIT:
            raise ValueError(f"registry answer larger than {REGISTRY_READ_LIMIT} bytes")
        published, latest = _registry_facts(ecosystem, json.loads(body.decode("utf-8")))
        result.update(found=True, exists=version_key(claimed) in {version_key(v) for v in published},
                      latest=latest)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            result["found"] = False
        else:
            result["error"] = f"HTTP {e.code}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def check_packages(text: str, task_text: str = "", limit: int = 25) -> list:
    """Check version claims against the ecosystems the text is about. Keeps only found packages."""
    ecosystems = detect_ecosystems(task_text + "\n" + text)
    claims = extract_version_claims(text)[:limit]
    if not ecosystems or not claims:
        return []

    def first_found(claim):
        for ecosystem in ecosystems:
            result = check_package(ecosystem, *claim)
            if result["found"] is True:
                return result
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        return [r for r in pool.map(first_found, claims) if r]


def _read_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def fact_check(task: Path) -> dict:
    """Check a finished task's URLs and package versions. Writes task/checks.json; never raises."""
    try:
        task = Path(task)
        final = _read_optional(task / "final.md")
        task_text = _read_optional(task / "task.md")
        cmd = read_json(task / "cmd.json") if (task / "cmd.json").is_file() else {}
        urls = check_urls(extract_urls(final))
        packages = check_packages(final, task_text)
        full = {"checked_at": now_iso(), "role": cmd.get("role"), "scope": cmd.get("scope"),
                "urls": urls, "packages": packages}
        write_json(task / "checks.json", full)
        checked = [u for u in urls if u["ok"] is not None]
        failed = [{"url": u["url"], "status": u["status"], "error": u["error"]} for u in checked if not u["ok"]]
        blocked = sum(1 for u in urls if (u["error"] or "").startswith("blocked"))
        # "latest" questions matter even when the claimed version exists, so keep those too, unless
        # the answer also gives the latest version (then the older one is mentioned in passing).
        current = {p["name"] for p in packages if p["latest"] and version_key(p["latest"]) == version_key(p["claimed"])}
        flagged = [p for p in packages if p["exists"] is False
                   or (p["latest"] and p["name"] not in current
                       and version_key(p["latest"]) != version_key(p["claimed"]))]
        return {"urls_checked": len(checked), "urls_failed": failed, "urls_blocked": blocked,
                "packages": flagged, "checked_at": full["checked_at"]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


SEARCH_REDIRECT = "vertexaisearch.cloud.google.com/grounding-api-redirect/"


def summarize_checks(summary: dict) -> list:
    """Short terminal lines for a fact_check summary."""
    if summary.get("error"):
        return [f"fact check failed: {summary['error']}"]
    lines = []
    failed = summary.get("urls_failed", [])
    # Gemini cites its search tool's short-lived redirect links rather than the pages themselves.
    redirects = [b for b in failed if SEARCH_REDIRECT in b["url"]]
    if redirects:
        lines.append(f"{len(redirects)} cited link(s) are expired Google search redirects, not real sources: "
                     "ask for the page URLs if the claims matter")
    for bad in failed:
        if bad in redirects:
            continue
        why = str(bad["status"]) if bad.get("status") else short(bad.get("error") or "no response", 60)
        lines.append(f"URL failed ({why}): {bad['url']}")
    for pkg in summary.get("packages", []):
        if pkg.get("exists") is False:
            tail = f" (latest {pkg['latest']})" if pkg.get("latest") else ""
            lines.append(f"{pkg['name']}: claimed {pkg['claimed']} is not a published version{tail}")
        elif pkg.get("latest"):
            lines.append(f"{pkg['name']}: claimed {pkg['claimed']} exists, but latest is {pkg['latest']}")
    if not lines:
        ok = summary.get("urls_checked", 0) - len(summary.get("urls_failed", []))
        lines.append(f"fact check: {ok} URLs ok, no version problems")
    if summary.get("urls_blocked"):
        lines.append(f"{summary['urls_blocked']} URL(s) could not be checked: the site refuses automated requests")
    return lines


# ------------------------------------------------------------------ scratch

# Build output that agents put in a run's scratch folder. A Rust review builds the whole project
# there from nothing, several GB a run, and nothing reads it once the run has finished.
BUILD_DIRS = {"target", "node_modules", ".venv", "venv", "build", "dist", "__pycache__", ".pytest_cache",
              ".mypy_cache", ".ruff_cache", ".gradle", ".tox", ".next", ".turbo", "obj"}
KEEP_LOOSE_BYTES = 1024 * 1024  # small files at the top of a build folder are often an agent's evidence logs


def _is_link(st) -> bool:
    """Symlinks, and on Windows junctions too: removed as links, never followed."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _remove(path: str, dry_run: bool = False):
    """Delete a file or folder without following links. Returns (bytes freed, errors).
    With dry_run, only adds up what would be freed."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    try:
        if _is_link(st):
            if dry_run:
                return 0, 0
            try:
                os.unlink(path)
            except OSError:
                os.rmdir(path)  # a directory link or junction: removes the link, not its target
            return 0, 0
        if stat.S_ISDIR(st.st_mode):
            freed = errors = 0
            with os.scandir(path) as entries:
                children = [e.path for e in entries]
            for child in children:
                f, e = _remove(child, dry_run)
                freed, errors = freed + f, errors + e
            if not dry_run:
                os.rmdir(path)
            return freed, errors
        if dry_run:
            return st.st_size, 0
        if not st.st_mode & stat.S_IWRITE:
            os.chmod(path, stat.S_IWRITE)  # Windows refuses to delete read-only files (git objects)
        os.unlink(path)
        return st.st_size, 0
    except OSError:
        return 0, 1


def prune_scratch(scratch: Path, dry_run: bool = False):
    """Delete build output from a finished run's scratch folder, keeping small loose files at the
    top of each build folder. Returns (bytes freed, errors); with dry_run, nothing is deleted."""
    freed = errors = 0
    stack = [str(scratch)]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                children = list(entries)
        except OSError:
            continue
        for entry in children:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if _is_link(st) or not stat.S_ISDIR(st.st_mode):
                continue
            # Agents name their build folders freely (probe-target, tgt, target-ws...), but Cargo and
            # other build tools mark every output folder with the standard CACHEDIR.TAG file.
            if entry.name not in BUILD_DIRS and not os.path.isfile(os.path.join(entry.path, "CACHEDIR.TAG")):
                stack.append(entry.path)
                continue
            with os.scandir(entry.path) as inner:
                contents = list(inner)
            for child in contents:
                try:
                    cst = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(cst.st_mode) and cst.st_size <= KEEP_LOOSE_BYTES:
                    continue
                f, e = _remove(child.path, dry_run)
                freed, errors = freed + f, errors + e
    return freed, errors


def shared_cache(top, cwd) -> Path:
    """A build cache kept between runs, one per repository, for roles that opt in with {cache}."""
    root = Path(top or cwd)
    digest = hashlib.sha1(str(root).lower().encode("utf-8")).hexdigest()[:8]
    return CREW_HOME / "cache" / f"{slugify(root.name)}-{digest}"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


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


LAST_OUTPUT = [time.time()]  # when the agent last printed anything; the heartbeat reports it


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
        LAST_OUTPUT[0] = time.time()
        out = f"\x1b[{COLOURS[style]}m{text}\x1b[0m" if style and self.colour else text
        print(out, flush=True)
        if self.raw.closed:  # crew's own notes after the agent has finished still belong in the log
            self.raw = open(self.raw.name, "a", encoding="utf-8")
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
        LAST_OUTPUT[0] = time.time()  # an event the view doesn't display still shows the agent is working
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


class OpencodeView(JsonView):
    """Renders `opencode run --format json`: one line per tool call, text in full. Events carry a
    sessionID and a `part`; step_finish parts carry tokens and cost for that step."""

    TOOL_ARG = ("command", "filePath", "file_path", "pattern", "query", "url", "path")
    DENIED_RE = re.compile(r"permission|denied|not allowed|rejected", re.IGNORECASE)

    def __init__(self, task: Path):
        super().__init__(task)
        self.since_tool = []  # text parts after the last tool call: the final answer
        self.all_parts = []
        self.cost = None

    def event(self, e: dict) -> None:
        self.session = e.get("sessionID") or e.get("sessionId") or self.session
        part = e.get("part") if isinstance(e.get("part"), dict) else {}
        kind = e.get("type") or part.get("type")
        if kind == "text":
            text = part.get("text") or e.get("text") or ""
            if text.strip():
                self.since_tool.append(text)
                self.all_parts.append(text)
                self.last_message = "\n\n".join(self.since_tool)
                self.show("")
                self.show(text)
        elif kind in ("tool_use", "tool"):
            self.since_tool = []
            tool = part.get("tool") or e.get("tool") or "tool"
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            args = state.get("input") if isinstance(state.get("input"), dict) else {}
            key = next((k for k in self.TOOL_ARG if k in args), None)
            detail = args[key] if key else (json.dumps(args, ensure_ascii=False) if args else "")
            self.show(f"{tool}: {short(detail, 170)}", "cyan")
            if state.get("status") == "error":
                err = str(state.get("error") or "")
                self.show("  -> error: " + short(err, 160), "yellow")
                if self.DENIED_RE.search(err) and tool not in self.denied:
                    self.denied.append(tool)
        elif kind in ("step_finish", "step-finish"):
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            step = sum(int(_num(tokens.get(k)) or 0) for k in ("input", "output", "reasoning"))
            step += int(_num(cache.get("write")) or 0)
            self.tokens = (self.tokens or 0) + step or self.tokens
            cost = _num(part.get("cost"))
            if cost is not None:
                self.cost = (self.cost or 0.0) + cost
        elif kind == "error":
            err = e.get("error") if isinstance(e.get("error"), dict) else {}
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            self.error = str(data.get("message") or err.get("message") or err.get("name") or e.get("message")
                             or "opencode reported an error")
            self.show(f"ERROR: {self.error}", "red")
        mid = part.get("modelID") or e.get("modelID")
        if mid:
            pid = part.get("providerID") or e.get("providerID")
            self.resolved_model = f"{pid}/{mid}" if pid else mid

    def final_text(self) -> str:
        return (self.last_message or "\n\n".join(self.all_parts)).strip()

    def close(self) -> None:
        if self.tokens or self.cost is not None:
            cost = f"${self.cost:.4f}" if isinstance(self.cost, (int, float)) else "n/a"
            self.show(f"tokens: {self.tokens}  cost: {cost}" + (f"  denied: {', '.join(self.denied)}"
                                                                if self.denied else ""), "dim")
        super().close()


class ApiView(JsonView):
    """Renders crew's own `_api` runner: text lines as they stream, then usage and cost."""

    def event(self, e: dict) -> None:
        kind = e.get("type")
        if kind == "start":
            self.session = e.get("session") or self.session
            self.show(f"{e.get('provider')} {e.get('url')}  model {e.get('model')}", "dim")
        elif kind == "text":
            self.show(e.get("text", ""))
        elif kind == "note":
            self.show("crew: " + str(e.get("text", "")), "yellow")
        elif kind == "done":
            self.last_message = e.get("text") or ""
            self.session = e.get("session") or self.session
            self.resolved_model = e.get("model") or self.resolved_model
            self.tokens = e.get("tokens")
            self.cost = _num(e.get("cost"))
            cost = f"${self.cost:.4f}" if self.cost is not None else "n/a"
            self.show(f"tokens: {self.tokens}  cost: {cost}", "dim")
        elif kind == "error":
            self.error = str(e.get("message") or "the API request failed")
            self.show(f"ERROR: {self.error}", "red")


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
        info["last_output"] = LAST_OUTPUT[0]
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


# ------------------------------------------------------------------ api runner

def _emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def api_body(api: dict, messages: list, minimal: bool = False) -> dict:
    """Chat-completions request. `minimal` drops everything optional, for providers that reject it."""
    body = {"model": api["model"], "messages": messages, "stream": True}
    if minimal:
        return body
    body["stream_options"] = {"include_usage": True}
    effort, style = api.get("effort"), api.get("effort_style")
    if effort and style == "openai":
        body["reasoning_effort"] = effort
    elif effort and style == "openrouter":
        body["reasoning"] = {"effort": effort}
    body.update(api.get("body") or {})
    return body


def api_stream(url: str, headers: dict, body: dict, timeout: float):
    """POST a streaming chat completion; yields parsed SSE data objects."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "text/event-stream" not in ctype:  # a provider that ignored stream=true
            yield json.loads(resp.read().decode("utf-8", "replace"))
            return
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                continue


def cmd_api(args) -> int:
    """Internal: run one api task. Reads cmd.json, streams the answer as JSON lines for ApiView,
    and keeps the conversation (conversation.json) so --resume can continue it."""
    task = Path(args.dir)
    cmd = read_json(task / "cmd.json")
    api = cmd["api"]
    url = api["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": "crew"}
    headers.update(api.get("headers") or {})
    if api.get("key_env"):
        key = read_secret(api["key_env"])
        if not key:
            _emit({"type": "error", "message": f"no API key: set {api['key_env']} or run "
                                               f"`crew.py set-key {api['provider']}`"})
            return 1
        headers["Authorization"] = f"Bearer {key}"
    messages = []
    if api.get("history"):
        try:
            messages = list(read_json(Path(api["history"])))
        except (OSError, ValueError, TypeError):
            _emit({"type": "error", "message": f"cannot read the earlier conversation {api['history']}"})
            return 1
    messages.append({"role": "user", "content": (task / "task.md").read_text(encoding="utf-8")})
    _emit({"type": "start", "provider": api["provider"], "url": url, "model": api["model"], "session": str(task)})

    text, line, usage, model, cost = [], "", None, None, None
    for attempt in ("full", "minimal"):
        try:
            for obj in api_stream(url, headers, api_body(api, messages, attempt == "minimal"),
                                  float(api.get("timeout") or 600)):
                if isinstance(obj.get("error"), dict) or isinstance(obj.get("error"), str):
                    err = obj["error"]
                    msg = err.get("message") if isinstance(err, dict) else err
                    code = err.get("code") if isinstance(err, dict) else None
                    _emit({"type": "error", "message": f"{code or 'error'}: {msg}"})
                    return 1
                model = obj.get("model") or model
                usage = obj.get("usage") or usage
                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or choice.get("message") or {}
                    piece = delta.get("content")
                    if not isinstance(piece, str) or not piece:
                        continue
                    text.append(piece)
                    line += piece
                    while "\n" in line:
                        done, line = line.split("\n", 1)
                        _emit({"type": "text", "text": done})
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:600]
            if e.code in (400, 422) and attempt == "full" and not text:
                _emit({"type": "note", "text": f"HTTP {e.code} with optional fields; retrying a minimal request"})
                continue
            _emit({"type": "error", "message": f"HTTP {e.code} {e.reason}: {short(detail, 500)}"})
            return 1
        except (urllib.error.URLError, OSError, ValueError) as e:
            _emit({"type": "error", "message": f"request to {url} failed: {e}"})
            return 1
    if line:
        _emit({"type": "text", "text": line})
    answer = "".join(text).strip()
    tokens = None
    if isinstance(usage, dict):
        tokens = int(_num(usage.get("total_tokens")) or
                     (_num(usage.get("prompt_tokens")) or 0) + (_num(usage.get("completion_tokens")) or 0)) or None
        cost = _num(usage.get("cost"))
    if answer:
        (task / "final.md").write_text(answer + "\n", encoding="utf-8")
        write_json(task / "conversation.json", messages + [{"role": "assistant", "content": answer}])
    _emit({"type": "done", "text": answer, "model": f"{api['provider']}/{model}" if model else None,
           "tokens": tokens, "cost": cost, "session": str(task)})
    return 0


def openrouter_credit(key_env: str):
    """Credit left on an OpenRouter key with a spending cap, as a usage window; None without a cap.
    GET /api/v1/key reports limit and limit_remaining."""
    key = read_secret(key_env)
    if not key:
        return None
    req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "crew"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = (json.loads(resp.read().decode("utf-8", "replace")) or {}).get("data") or {}
    limit, remaining = _num(data.get("limit")), _num(data.get("limit_remaining"))
    if not limit or remaining is None:
        return None
    return {"credit": {"used": min(max(1 - remaining / limit, 0.0), 1.0), "resets_at": None}}


# Per-session variables the calling agent sets for its own commands (session ids, messaging
# sockets and tokens, effort). Passed on, they would make a delegated agent think it belongs to
# the caller's session, and would hand the caller's tokens to another vendor's process.
# Configuration variables (CLAUDE_CODE_USE_BEDROCK, CODEX_HOME, ...) are kept.
SESSION_VAR_RE = re.compile(
    r"^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|CLAUDE_CODE_ENTRYPOINT"
    r"|CLAUDE_CODE_\w*(SESSION|MESSAGING|BRIDGE|CHILD|EXECPATH)\w*"
    r"|CODEX_(SESSION_ID|THREAD_ID|CI|SANDBOX\w*|VERSION))$")


def after_run(task: Path, cmd: dict, view, status: str, note):
    """crew's own steps once the agent has finished: record quota, handle a usage limit (and
    retry elsewhere), fact-check web answers. A failure here is noted, never fatal to the run."""
    backend = cmd["backend"]
    key = cmd.get("quota_key") or backend  # the provider, when the run used one
    quota = retried_as = checks = None
    try:
        quota = view.quota
        if backend == "codex" and view.session and not cmd.get("provider"):
            quota = codex_quota_windows(view.session)
        if cmd.get("provider") == "openrouter":
            try:
                pcfg = providers(load_config()).get("openrouter") or {}
                quota = openrouter_credit(pcfg.get("key_env") or "OPENROUTER_API_KEY") or quota
            except (OSError, ValueError, AttributeError):
                pass  # the credit check is advisory
        if quota:
            record_quota(key, quota, f"{backend} run {task.name}")
        tail = "\n".join([view.error or ""] + view.text[-30:])
        hit_limit = any((_num((w or {}).get("used")) or 0) >= 1 for w in (quota or {}).values())
        if status in ("agent-error", "empty") and (looks_rate_limited(tail) or hit_limit):
            status = "limited"
            resets = limit_reset_from_text(tail) or max(
                [_num(w.get("resets_at")) for w in (quota or {}).values()
                 if (_num((w or {}).get("used")) or 0) >= 1 and _num(w.get("resets_at"))], default=None)
            record_limit(key, resets, load_config().get("limit_cooldown_minutes", 60))
            note = f"{key} hit its usage limit" + (f" (resets {dt.datetime.fromtimestamp(resets):%H:%M %d %b})"
                                                       if resets else "") + (f": {note}" if note else "")
            req = cmd.get("request") or {}
            if req.get("retry"):
                retried_as, retry_note = retry_elsewhere(task)
                note += f"; {retry_note}"
                view.show(f"crew: {retry_note}", "yellow")
            elif req and not req.get("agent") and not cmd.get("panel"):
                note += "; not retried: no other eligible agent for this role"
    except Exception as e:
        note = f"{note}; " if note else ""
        note += f"crew could not record usage limits: {type(e).__name__}: {e}"
    if status == "ok" and cmd.get("fact_check"):
        view.show("crew: fact-checking the cited URLs and package versions...", "dim")
        checks = fact_check(task)
        for line in summarize_checks(checks):
            view.show(f"  {line}", "yellow" if "fail" in line or "not" in line or "latest is" in line else "dim")
    return status, note, quota, retried_as, checks


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
    for key, value in (cmd.get("env") or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    for var, source in (cmd.get("secret_env") or {}).items():  # API keys, never stored in cmd.json
        value = read_secret(source)
        if value:
            env[var] = value
    return env


def run_agent(task: Path, cmd: dict, started: float) -> None:
    text = (task / "task.md").read_text(encoding="utf-8")
    view = {"codex": CodexView, "claude": ClaudeView, "opencode": OpencodeView,
            "api": ApiView}.get(cmd["backend"], View)(task)
    print_header(cmd, text, task, view)
    top = cmd.get("git_top")
    before = git_state(top) if top else None
    popen_kw = {} if IS_WIN else {"start_new_session": True}
    env = agent_env(cmd)
    env["PWD"] = cmd["cwd"]  # some CLIs (opencode) take their project folder from PWD, not the process cwd
    proc = subprocess.Popen(cmd["argv"], cwd=cmd["cwd"], env=env,
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
    timed_out, stalled = threading.Event(), threading.Event()

    def on_timeout():
        timed_out.set()
        kill_tree(proc)
    timer = threading.Timer(float(cmd.get("max_minutes") or 60) * 60, on_timeout)
    timer.daemon = True
    timer.start()
    LAST_OUTPUT[0] = time.time()
    if cmd.get("stall_minutes"):
        def watch_stall():
            limit = float(cmd["stall_minutes"]) * 60
            while proc.poll() is None:
                if time.time() - LAST_OUTPUT[0] > limit:
                    stalled.set()
                    kill_tree(proc)
                    return
                time.sleep(min(5.0, limit / 4))
        threading.Thread(target=watch_stall, daemon=True).start()
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
    if stalled.is_set():
        status, note = "stalled", f"killed after no output for stall_minutes={cmd.get('stall_minutes')}"
    elif timed_out.is_set():
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

    status, note, quota, retried_as, checks = after_run(task, cmd, view, status, note)
    seconds = round(time.time() - started, 1)
    session_cost = view.cost
    cost = session_cost
    if isinstance(session_cost, (int, float)) and isinstance(cmd.get("prior_session_cost_usd"), (int, float)):
        cost = max(session_cost - cmd["prior_session_cost_usd"], 0.0)
    resolved = view.resolved_model or cmd["model"]
    cap = _num(cmd.get("max_cost_usd"))
    if cap is not None and isinstance(cost, (int, float)) and cost > cap:
        over = f"cost ${cost:.4f} is over max_cost_usd ${cap}"
        note = f"{note}; {over}" if note else over
    worktree = finish_worktree(cmd["worktree"], touched) if cmd.get("worktree") else None
    if worktree:
        wt_note = (f"changes are in worktree {worktree['path']} on branch {worktree['branch']}" if worktree["kept"]
                   else "the worktree had no changes and was removed")
        note = f"{note}; {wt_note}" if note else wt_note
    result = {"status": status, "note": note, "exit": rc, "role": cmd["role"], "agent": cmd["agent"],
              "backend": cmd["backend"], "model": cmd["model"], "resolved_model": resolved,
              "scope": cmd["scope"], "seconds": seconds, "tokens": view.tokens, "cost_usd": cost,
              "session_cost_usd": session_cost, "cost_note": view.cost_note, "model_usage": view.model_usage,
              "session_id": view.session, "touched_files": touched, "violations": violations,
              "denied_tools": view.denied, "quota": quota, "checks": checks, "retried_as": retried_as,
              "worktree": worktree,
              "final": str(final), "finished": now_iso()}
    write_json(task / "result.json", result)
    append_line(LEDGER, json.dumps({
        "ts": now_iso(), "role": cmd["role"], "backend": cmd["backend"], "model": cmd["model"],
        "resolved_model": resolved, "caller": cmd.get("caller"), "caller_session": cmd.get("caller_session"),
        "status": status, "seconds": seconds, "tokens": view.tokens, "cost_usd": cost,
        "cost_reliable": view.cost_note is None, "resumed_from": cmd.get("resumed_from"),
        "retry_of": cmd.get("retry_of"), "panel": cmd.get("panel"), "dir": task.name}))
    if cmd.get("prune_scratch", True):
        freed, errors = prune_scratch(task / "scratch")
        if freed or errors:
            result.update(scratch_pruned_bytes=freed, scratch_prune_errors=errors)
            write_json(task / "result.json", result)
            view.show(f"crew: removed {human_bytes(freed)} of build output from scratch"
                      + (f" ({errors} item(s) could not be removed)" if errors else ""), "dim")
    view.raw.close()
    colour, reset =("\x1b[32m" if status == "ok" else "\x1b[31m", "\x1b[0m") if view.colour else ("", "")
    print("-" * 72)
    print(f"{colour}crew  {status}  exit={rc}  {seconds}s"
          + (f"  tokens={view.tokens}" if view.tokens else "")
          + (f"  touched={len(touched)} file(s)" if touched else "") + reset, flush=True)


# --------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crew.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="dispatch a task")
    r.add_argument("--role", help="role from the config (review, check, implement, ...)")
    r.add_argument("--task", help="what the agent should do, inline")
    r.add_argument("--task-file", help="what the agent should do, from a file")
    r.add_argument("--cd", default=os.getcwd(), help="working directory for the agent (default: here)")
    r.add_argument("--caller", type=caller_arg,
                   help="the agent running crew (codex | claude | agy | opencode:<provider/model> | none); its own "
                        "model family is skipped in fallback lists (default: detected)")
    r.add_argument("--agent", help="use this agent: an alias or backend[+provider]:model[@effort]")
    r.add_argument("--model", help="override just the model name")
    r.add_argument("--effort", help="override reasoning effort (low | medium | high | ...)")
    r.add_argument("--access", choices=ACCESS_LEVELS, help="files: read | verify (build/test, scratch writes) | write")
    r.add_argument("--web", action=argparse.BooleanOptionalAction, default=None, help="allow web search / fetch")
    r.add_argument("--network", action=argparse.BooleanOptionalAction, default=None,
                   help="allow network for commands the agent runs (codex/claude, verify/write only)")
    r.add_argument("--target", default="uncommitted", help="review target: uncommitted | commit:<sha> | base:<branch>")
    r.add_argument("--resume", help="follow up on a finished codex, claude, opencode or api task in the same session")
    r.add_argument("--slug", help="short name for the task directory")
    r.add_argument("--view", choices=("auto", "tab", "hidden"), help="visible terminal or hidden (default from config)")
    r.add_argument("--hold", action=argparse.BooleanOptionalAction, default=None,
                   help="keep the terminal tab open after the task ends")
    r.add_argument("--max-minutes", type=float, help="kill the agent after this long")
    r.add_argument("--stall-minutes", type=float,
                   help="kill the agent if it prints nothing for this long (not for agy, which prints only at the end)")
    r.add_argument("--max-cost", type=float, metavar="USD",
                   help="cost cap: enforced by claude (--max-budget-usd); other agents are flagged afterwards")
    r.add_argument("--worktree", action="store_true",
                   help="run in a new git worktree from HEAD (kept with its branch if the agent changed files)")
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
    pr = sub.add_parser("prune", help="remove build output from finished runs' scratch folders (records are kept)")
    pr.add_argument("dirs", nargs="*", help="run folders (default: every finished run)")
    pr.add_argument("--dry-run", action="store_true", help="only report what would be removed")
    pr.add_argument("--cache", action="store_true", help="remove the shared per-repository build caches instead")
    pr.set_defaults(func=cmd_prune)

    c = sub.add_parser("collect", help="wait for / reprint a task")
    c.add_argument("dir")
    c.add_argument("--timeout", type=float, default=3600)
    c.set_defaults(func=cmd_collect)

    s = sub.add_parser("status", help="recent tasks and their state")
    s.add_argument("--last", type=int, default=10)
    s.set_defaults(func=cmd_status)

    sub.add_parser("stats", help="usage per backend and model").set_defaults(func=cmd_stats)
    ro = sub.add_parser("roles", help="show roles, aliases and which agent each resolves to")
    ro.add_argument("--caller", type=caller_arg)
    ro.set_defaults(func=cmd_roles)
    mo = sub.add_parser("models", help="list the models each installed backend and provider offers")
    mo.add_argument("--provider", help="list the models a provider's API offers (needs its key)")
    mo.add_argument("--changes", action="store_true",
                    help="show models added or removed since the last --changes run, then remember the current lists")
    mo.set_defaults(func=cmd_models)

    sr = sub.add_parser("set-role", help="choose the agent and default scope for a role (saved in your config)")
    sr.add_argument("role")
    sr.add_argument("--agent", nargs="+", help="alias or backend[+provider]:model[@effort]; several = fallback list")
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

    dr = sub.add_parser("doctor", help="check installation, logins, models and permissions")
    dr.add_argument("--ask", action="store_true",
                    help="also send every configured agent a one-word question, to prove it is signed in and answering")
    dr.set_defaults(func=cmd_doctor)
    sub.add_parser("setup-agy-web", help=f"add {AGY_FETCH_RULE} to agy's settings (backs the file up first)"
                   ).set_defaults(func=cmd_setup_agy_web)

    sk = sub.add_parser("set-key", help="store a provider's API key in ~/.crew/keys.json (read from stdin or a prompt)")
    sk.add_argument("name", help="provider name (e.g. openrouter) or the key's variable (e.g. OPENROUTER_API_KEY)")
    sk.add_argument("--remove", action="store_true")
    sk.set_defaults(func=cmd_set_key)

    ap = sub.add_parser("_api")  # internal: the api backend's request runner
    ap.add_argument("--dir", required=True)
    ap.set_defaults(func=cmd_api)

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
