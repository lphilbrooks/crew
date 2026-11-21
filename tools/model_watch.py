#!/usr/bin/env python3
"""model_watch: report model changes that crew's built-in list doesn't know about yet.

Compares skills/crew/assets/config.default.json with public, keyless sources:

  codex   Codex's bundled model list (openai/codex, codex-rs/models-manager/models.json):
          new or removed slugs, and changed reasoning efforts.
  agy     Antigravity's models page (antigravity.google/docs/models): model names in the
          selector, and models marked for removal.
  claude  models.dev (the catalogue OpenCode uses): new Anthropic models.
  gemini  models.dev: new Google models (for the Gemini API and as an early hint for agy).

It changes nothing. It prints a Markdown report and exits 1 when there is something to look at,
0 when crew is up to date, and 2 when a source could not be read. A maintainer (or an agent)
then edits config.default.json, SKILL.md, README.md and tests/test_models.py as for any release.

Models deliberately left out go in tools/model_watch_ignore.json so they stop being reported.

Standard library only, Python 3.9+.
    python tools/model_watch.py                # fetch and report
    python tools/model_watch.py --json         # machine-readable
    python tools/model_watch.py --offline DIR  # read codex_models.json, agy_models.html and
                                               # models_dev.json from DIR (for tests)
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "skills" / "crew" / "assets" / "config.default.json"
IGNORE = Path(__file__).resolve().parent / "model_watch_ignore.json"

SOURCES = {
    "codex": ("codex_models.json",
              "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json"),
    "agy": ("agy_models.html", "https://antigravity.google/docs/models"),
    "models_dev": ("models_dev.json", "https://models.dev/api.json"),
}
EFFORT_SUFFIX = re.compile(r"-(?:none|minimal|low|medium|high|xhigh|max)$")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "crew-model-watch"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def load_sources(offline: str | None) -> tuple[dict, list]:
    out, errors = {}, []
    for name, (filename, url) in SOURCES.items():
        try:
            out[name] = (Path(offline) / filename).read_text(encoding="utf-8") if offline else fetch(url)
        except Exception as e:  # noqa: BLE001 - any failure just means this source is skipped
            errors.append(f"{name}: could not read {filename if offline else url}: {e}")
    return out, errors


def known_models(cfg: dict) -> dict:
    """Model ids crew already lists, per backend. agy ids lose their effort part."""
    known = {"codex": {}, "claude": set(), "agy": set(), "gemini_api": set()}
    for entry in cfg["models"].values():
        if not isinstance(entry, dict):
            continue
        backend, model = entry["backend"], entry["model"]
        if backend == "codex":
            known["codex"][model] = list(entry.get("efforts") or [])
        elif backend == "claude":
            known["claude"].add(model)
        elif backend == "agy":
            known["agy"].add(EFFORT_SUFFIX.sub("", model.replace("-{effort}", "")))
        elif backend == "api" and model.startswith("gemini/"):
            known["gemini_api"].add(model.split("/", 1)[1])
    for spec in cfg.get("agents", {}).values():
        if isinstance(spec, str) and spec.startswith("api:gemini/"):
            known["gemini_api"].add(spec.split("/", 1)[1].split("@")[0])
    return known


def check_codex(text: str, known: dict, ignore: set) -> list:
    models = [m for m in json.loads(text).get("models", []) if m.get("visibility", "list") == "list"]
    offered = {m["slug"]: [lv.get("effort", lv) if isinstance(lv, dict) else lv
                           for lv in m.get("supported_reasoning_levels") or []] for m in models}
    findings = []
    for slug in sorted(set(offered) - set(known) - ignore):
        findings.append(f"new in Codex: `{slug}` (efforts: {', '.join(offered[slug]) or 'none'})")
    for slug in sorted(set(known) - set(offered)):
        findings.append(f"no longer listed by Codex: `{slug}`")
    for slug in sorted(set(known) & set(offered)):
        if offered[slug] and sorted(known[slug]) != sorted(offered[slug]):
            findings.append(f"efforts changed for `{slug}`: crew has {', '.join(known[slug])};"
                            f" Codex offers {', '.join(offered[slug])}")
    return findings


def page_text(raw: str) -> str:
    raw = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))


def agy_id(name: str) -> str:
    """'Gemini 4 Neon' -> gemini-4-neon; 'Claude Opus 5.5 (Thinking)' -> claude-opus-5-5;
    'GPT-OSS 120B (Medium)' -> gpt-oss-120b. Matches how agy writes its ids."""
    name = re.sub(r"\s*\(.*?\)", "", name).strip().lower()
    slug = re.sub(r"\s+", "-", name)
    if slug.startswith("claude-"):
        slug = slug.replace(".", "-")
    return slug


AGY_NAME = re.compile(r"\b(Gemini \d+(?:\.\d+)? [A-Z][a-z]+|Claude [A-Z][a-z]+ \d+(?:\.\d+)?|GPT-OSS[ -]120[Bb])")


def check_agy(raw: str, known: set, ignore: set) -> list:
    text = page_text(raw)
    names = {agy_id(n): n for n in AGY_NAME.findall(text)}
    findings = []
    for slug in sorted(set(names) - known - ignore):
        findings.append(f"in Antigravity's model list but not in crew: {names[slug]} (`{slug}-<effort>`?"
                        " check the exact id with `agy models`)")
    for slug in sorted(known - set(names)):
        findings.append(f"crew lists `{slug}` for agy but Antigravity's model page doesn't mention it")
    removal = re.search(r"\* Will be removed on ([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if removal:
        marked = {agy_id(m.group(1)) for m in re.finditer(AGY_NAME.pattern + r"[^*✅❌]{0,20}\*(?!\*)", text)}
        for slug in sorted(marked & known):
            findings.append(f"Antigravity removes `{slug}` on {removal.group(1)}")
    return findings


def check_models_dev(text: str, provider: str, known: set, ignore: set, prefix: str) -> list:
    models = json.loads(text).get(provider, {}).get("models", {})
    dates = [m.get("release_date") or "" for k, m in models.items() if k in known]
    since = min((d for d in dates if d), default="")
    findings = []
    for mid, m in sorted(models.items(), key=lambda kv: kv[1].get("release_date") or ""):
        date = m.get("release_date") or ""
        if (mid in known or mid in ignore or not mid.startswith(prefix) or not date or date < since
                or "latest" in mid or m.get("status") == "deprecated"):
            continue
        findings.append(f"new {provider} model on models.dev: `{mid}` ({m.get('name', mid)}, released {date})")
    return findings


def report(cfg: dict, sources: dict, ignore: dict) -> dict:
    known = known_models(cfg)
    out = {}
    if "codex" in sources:
        out["codex"] = check_codex(sources["codex"], known["codex"], set(ignore.get("codex", [])))
    if "agy" in sources:
        out["agy"] = check_agy(sources["agy"], known["agy"], set(ignore.get("agy", [])))
    if "models_dev" in sources:
        out["claude"] = check_models_dev(sources["models_dev"], "anthropic", known["claude"],
                                         set(ignore.get("claude", [])), "claude-")
        out["gemini"] = check_models_dev(sources["models_dev"], "google",
                                         known["gemini_api"] | known["agy"],
                                         set(ignore.get("gemini", [])), "gemini-")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--config", default=str(CONFIG))
    args = ap.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    ignore = json.loads(IGNORE.read_text(encoding="utf-8")) if IGNORE.exists() else {}
    sources, errors = load_sources(args.offline)
    found = report(cfg, sources, ignore)
    if args.json:
        print(json.dumps({"findings": found, "errors": errors}, indent=2))
    else:
        print("# Model watch\n")
        for section, items in found.items():
            print(f"## {section}\n")
            print("\n".join(f"- {i}" for i in items) if items else "- up to date")
            print()
        for e in errors:
            print(f"> {e}")
    if errors:
        return 2
    return 1 if any(found.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
