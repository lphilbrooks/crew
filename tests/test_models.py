"""Model catalogue tests: naming a model, efforts, role names, and the list in SKILL.md.

No vendor CLIs needed. Run from the repo root:  python -m unittest tests.test_models -v
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "skills" / "crew" / "scripts" / "crew.py"
sys.path.insert(0, str(CREW.parent))
import crew  # noqa: E402

CFG = json.loads((ROOT / "skills" / "crew" / "assets" / "config.default.json").read_text(encoding="utf-8"))


class Naming(unittest.TestCase):
    def spec(self, ref):
        return crew.spec_str(crew.expand_agent(CFG, ref)[0])

    def test_names_and_efforts(self):
        cases = {
            "gemini-flash": "agy:gemini-3.8-flash-medium",
            "gemini-flash@high": "agy:gemini-3.8-flash-high",
            "gemini flash medium": "agy:gemini-3.8-flash-medium",
            "Gemini Flash low effort": "agy:gemini-3.8-flash-low",
            "flash": "agy:gemini-3.8-flash-medium",
            "argon": "agy:gemini-4-argon-medium",
            "gemini pro high": "agy:gemini-4-argon-high",
            "argon 4 low": "agy:gemini-4-argon-low",
            "opus via agy": "agy:claude-opus-5-5-high",
            "gpt-oss": "agy:gpt-oss-120b-medium",
            "codex": "codex:gpt-6.1-sol@high",
            "luna@none": "codex:gpt-6-luna@none",
            "sol xhigh": "codex:gpt-6.1-sol@xhigh",
            "claude opus": "claude:claude-opus-5-5@high",
            "haiku": "claude:claude-haiku-5-5@medium",
        }
        for ref, want in cases.items():
            self.assertEqual(self.spec(ref), want, ref)

    def test_specs_and_aliases_still_work(self):
        self.assertEqual(self.spec("codex:gpt-6.1-sol@low"), "codex:gpt-6.1-sol@low")
        self.assertEqual(self.spec("google-fast"), "agy:gemini-3.8-flash-medium")

    def test_unknown_effort_refused(self):
        with self.assertRaises(crew.CrewError) as e:
            crew.expand_agent(CFG, "gemini-flash@max")
        self.assertIn("low | medium | high", str(e.exception))
        with self.assertRaises(crew.CrewError):
            crew.expand_agent(CFG, "sol@none")
        with self.assertRaises(crew.CrewError) as e:
            crew.expand_agent(CFG, "gemini flash xhigh")
        self.assertIn("not 'xhigh'", str(e.exception))

    def test_unknown_name_points_to_models(self):
        with self.assertRaises(crew.CrewError) as e:
            crew.expand_agent(CFG, "gemini ultra")
        self.assertIn("crew.py models", str(e.exception))

    def test_effort_flag_moves_into_agy_model_id(self):
        spec = {"backend": "agy", "model": "gemini-3.8-flash-medium", "effort": None}
        self.assertEqual(crew.settle_effort(CFG, spec, "high"),
                         ({"backend": "agy", "model": "gemini-3.8-flash-high", "effort": None}, None))
        with self.assertRaises(crew.CrewError):
            crew.settle_effort(CFG, spec, "xhigh")

    def test_effort_flag_on_codex_and_unknown_models(self):
        spec = {"backend": "codex", "model": "gpt-6-luna", "effort": "medium"}
        self.assertEqual(crew.settle_effort(CFG, spec, "low")[1], "low")
        with self.assertRaises(crew.CrewError):
            crew.settle_effort(CFG, spec, "ultra")
        other = {"backend": "codex", "model": "gpt-7-preview", "effort": None}
        self.assertEqual(crew.settle_effort(CFG, other, "ultra"), (other, "ultra"))

    def test_names_are_unique(self):
        seen = {}
        for key, entry in crew.catalogue(CFG).items():
            for name in [key] + entry.get("names", []):
                n = crew.norm_name(name)
                self.assertNotIn(n, seen, f"{name} is used by {seen.get(n)} and {key}")
                seen[n] = key

    def test_default_effort_is_listed(self):
        for key, entry in crew.catalogue(CFG).items():
            if entry.get("efforts"):
                self.assertIn(entry["default_effort"], entry["efforts"], key)

    def test_role_aliases_resolve(self):
        for alias in CFG["agents"].values():
            crew.expand_agent(CFG, alias)
        for role in CFG["roles"].values():
            crew.expand_agent(CFG, role["agent"])
        for name, role in CFG["role_names"].items():
            self.assertIn(role, CFG["roles"], name)


class SkillTable(unittest.TestCase):
    def test_every_model_is_in_skill_md(self):
        text = (ROOT / "skills" / "crew" / "SKILL.md").read_text(encoding="utf-8")
        section = text.split("### Naming a model", 1)[1].split("\n#", 1)[0]
        rows = [l for l in section.splitlines() if l.startswith("| `")]
        named = {n for row in rows for n in re.findall(r"`([^`]+)`", row.split("|")[1])}
        self.assertEqual(named, set(crew.catalogue(CFG)))
        for row in rows:
            cells = [c.strip() for c in row.split("|")[1:-1]]
            for key in re.findall(r"`([^`]+)`", cells[0]):
                entry = CFG["models"][key]
                self.assertEqual(cells[1], entry["backend"], key)
                efforts = entry.get("efforts") or []
                if efforts:
                    listed = [e.strip() for e in cells[2].split(",")]
                    self.assertEqual(listed[0], entry["default_effort"], key)
                    self.assertEqual(sorted(listed), sorted(efforts), key)


class ModelsCommand(unittest.TestCase):
    def test_offline_list(self):
        r = subprocess.run([sys.executable, str(CREW), "models", "--offline"], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("gemini-flash", r.stdout)
        self.assertIn("gemini-3.8-flash-<effort>", r.stdout)
        self.assertIn("*medium", r.stdout)

    def test_missing_model_points_to_lookalike(self):
        import contextlib
        import io
        import types
        offered = [{"slug": s, "description": ""} for s in
                   ("gemini-3.8-flash-medium", "gemini-4-argon", "gpt-oss-120b-medium")]
        saved = crew.backend_cmd, crew.agy_models, crew.codex_models, crew.load_config
        crew.backend_cmd = lambda cfg, b: [b] if b == "agy" else None
        crew.agy_models = lambda cfg: offered
        crew.codex_models = lambda: None
        crew.load_config = lambda: CFG
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                crew.cmd_models(types.SimpleNamespace(offline=False))
        finally:
            crew.backend_cmd, crew.agy_models, crew.codex_models, crew.load_config = saved
        text = out.getvalue()
        self.assertIn("argon (gemini-4-argon-{effort}) is not offered by your agy; it offers gemini-4-argon", text)
        self.assertIn("agy-opus (claude-opus-5-5-{effort}) is not offered", text)
        self.assertNotIn("gemini-flash (", text)


if __name__ == "__main__":
    unittest.main()
