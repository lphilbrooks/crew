"""tools/model_watch.py against small stand-in sources (no network)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import model_watch  # noqa: E402

CFG = {
    "models": {
        "sol": {"backend": "codex", "model": "gpt-6.1-sol", "efforts": ["low", "medium", "high"]},
        "old": {"backend": "codex", "model": "gpt-5-old", "efforts": ["low"]},
        "fable": {"backend": "claude", "model": "claude-fable-5-1"},
        "hydrogen": {"backend": "agy", "model": "gemini-4-hydrogen-{effort}"},
        "agy-opus": {"backend": "agy", "model": "claude-opus-5-5-{effort}"},
        "gpt-oss": {"backend": "agy", "model": "gpt-oss-120b-medium"},
        "carbon": {"backend": "api", "model": "gemini/gemini-4-carbon"},
    },
    "agents": {},
}
CODEX = {"models": [
    {"slug": "gpt-6.2-sol", "visibility": "list", "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},
    {"slug": "gpt-6.1-sol", "visibility": "list",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"}, {"effort": "max"}]},
    {"slug": "hidden-thing", "visibility": "hide"},
]}
AGY = """<html><body><script>Gemini 9 Ghost</script><table>
<tr><td>Gemini 4 Neon</td></tr><tr><td>Gemini 4 Hydrogen</td></tr>
<tr><td>Claude Opus 5.5 (Thinking)</td></tr><tr><td>GPT-OSS-120b *</td><td>&#x2705;</td></tr>
</table><p>* Will be removed on November 30, 2026.</p></body></html>"""
MODELS_DEV = {
    "anthropic": {"models": {
        "claude-fable-5-1": {"name": "Claude Fable 5.1", "release_date": "2026-09-01"},
        "claude-fable-5-2": {"name": "Claude Fable 5.2", "release_date": "2026-11-09"},
        "claude-opus-4": {"name": "Claude Opus 4", "release_date": "2025-05-22"},
    }},
    "google": {"models": {
        "gemini-4-carbon": {"name": "Gemini 4 Carbon", "release_date": "2026-11-10"},
        "gemini-4-neon": {"name": "Gemini 4 Neon", "release_date": "2026-11-18"},
        "gemini-flash-latest": {"name": "Gemini Flash Latest", "release_date": "2026-11-19"},
    }},
}


class ModelWatch(unittest.TestCase):
    def run_report(self, ignore=None):
        return model_watch.report(CFG, {"codex": json.dumps(CODEX), "agy": AGY,
                                        "models_dev": json.dumps(MODELS_DEV)}, ignore or {})

    def test_codex(self):
        codex = self.run_report()["codex"]
        self.assertIn("new in Codex: `gpt-6.2-sol` (efforts: low, high)", codex)
        self.assertIn("no longer listed by Codex: `gpt-5-old`", codex)
        self.assertTrue(any(f.startswith("efforts changed for `gpt-6.1-sol`") for f in codex))
        self.assertFalse(any("hidden-thing" in f for f in codex))

    def test_agy(self):
        agy = self.run_report()["agy"]
        self.assertTrue(any("Gemini 4 Neon" in f for f in agy), agy)
        self.assertFalse(any("Ghost" in f for f in agy))  # script contents are not page text
        self.assertFalse(any("hydrogen" in f.lower() for f in agy))
        self.assertFalse(any("claude-opus-5-5" in f for f in agy))
        self.assertIn("Antigravity removes `gpt-oss-120b` on November 30, 2026", agy)

    def test_models_dev_reports_only_newer_unknown_models(self):
        found = self.run_report()
        self.assertEqual(found["claude"], ["new anthropic model on models.dev: `claude-fable-5-2`"
                                           " (Claude Fable 5.2, released 2026-11-09)"])
        self.assertEqual(len(found["gemini"]), 1)
        self.assertIn("gemini-4-neon", found["gemini"][0])

    def test_ignore_list(self):
        found = self.run_report({"codex": ["gpt-6.2-sol"], "claude": ["claude-fable-5-2"]})
        self.assertFalse(any("gpt-6.2-sol" in f for f in found["codex"]))
        self.assertEqual(found["claude"], [])

    def test_agy_id(self):
        self.assertEqual(model_watch.agy_id("Gemini 3.8 Flash"), "gemini-3.8-flash")
        self.assertEqual(model_watch.agy_id("Claude Sonnet 5.5 (Thinking)"), "claude-sonnet-5-5")
        self.assertEqual(model_watch.agy_id("GPT-OSS 120B"), "gpt-oss-120b")

    def test_offline_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "codex_models.json").write_text(json.dumps(CODEX))
            (Path(d) / "agy_models.html").write_text(AGY)
            (Path(d) / "models_dev.json").write_text(json.dumps(MODELS_DEV))
            cfg = Path(d) / "cfg.json"
            cfg.write_text(json.dumps(CFG))
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(model_watch.main(["--offline", d, "--config", str(cfg)]), 1)
                (Path(d) / "models_dev.json").unlink()
                self.assertEqual(model_watch.main(["--offline", d, "--config", str(cfg)]), 2)

    def test_shipped_config_parses(self):
        cfg = json.loads(model_watch.CONFIG.read_text(encoding="utf-8"))
        known = model_watch.known_models(cfg)
        self.assertIn("gpt-6.2-sol", known["codex"])
        self.assertIn("gemini-4-neon", known["agy"])
        self.assertIn("gpt-oss-120b", known["agy"])


if __name__ == "__main__":
    unittest.main()
