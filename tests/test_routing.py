"""Routing and quota tests: model families, caller skipping, quota memory and balancing.

No network and no vendor CLIs: every backend counts as installed and quota lives in a temp dir.
Run from the repo root:  python -m unittest tests.test_routing -v
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "crew" / "scripts"))
import crew  # noqa: E402


def temp_dir():
    """TemporaryDirectory that tolerates Windows file locks; ignore_cleanup_errors is Python 3.10+."""
    try:
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except TypeError:
        return tempfile.TemporaryDirectory()




class Routing(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.cfg = {"agents": {}}
        self.saved_file = crew.QUOTA_FILE
        self.saved_cmd = crew.backend_cmd
        crew.QUOTA_FILE = Path(self.tmp.name) / "quota.json"
        crew.backend_cmd = lambda cfg, b: [b]

    def tearDown(self):
        crew.QUOTA_FILE = self.saved_file
        crew.backend_cmd = self.saved_cmd
        self.tmp.cleanup()

    def backends(self, ref, **kw):
        return [crew.spec_str(s) for s, _, _ in crew.candidates_for(self.cfg, ref, **kw)]

    def set_windows(self, backend, windows):
        crew.record_quota(backend, windows, "test")


class TestFamily(unittest.TestCase):
    def fam(self, text):
        return crew.model_family(crew.parse_spec(text))

    def test_families(self):
        self.assertEqual(self.fam("codex:gpt-6.1-sol@high"), "openai")
        self.assertEqual(self.fam("claude:claude-opus-5-5"), "anthropic")
        self.assertEqual(self.fam("agy:claude-opus-5-5-high"), "anthropic")
        self.assertEqual(self.fam("agy:gpt-oss-120b-medium"), "openai")
        self.assertEqual(self.fam("agy:gemini-3.1-pro-high"), "google")
        self.assertEqual(self.fam("agy:something-else"), "google")


class TestCallerFamily(Routing):
    def test_claude_caller_drops_agy_claude_model(self):
        ref = ["agy:claude-opus-5-5-high", "codex:gpt-6.1-sol@high"]
        self.assertEqual(self.backends(ref, caller="claude"), ["codex:gpt-6.1-sol@high"])

    def test_codex_caller_drops_agy_gpt_model(self):
        ref = ["agy:gpt-oss-120b-medium", "claude:claude-opus-5-5@high"]
        self.assertEqual(self.backends(ref, caller="codex"), ["claude:claude-opus-5-5@high"])

    def test_same_family_only_keeps_them_with_note(self):
        ref = ["claude:claude-opus-5-5@high", "agy:claude-sonnet-5-5-high"]
        spec, _, note = crew.choose_agent(self.cfg, ref, caller="claude")
        self.assertEqual(spec["backend"], "claude")
        self.assertIn("anthropic is also the caller's model family", note)
        self.assertEqual(len(crew.candidates_for(self.cfg, ref, caller="claude")), 2)

    def test_no_caller_no_filter(self):
        ref = ["agy:claude-opus-5-5-high", "codex:gpt-6.1-sol@high"]
        self.assertEqual(self.backends(ref), ref)


class TestExplicit(Routing):
    def test_explicit_bypasses_everything(self):
        crew.record_limit("codex", cooldown_minutes=60)
        ref = ["codex:gpt-6.1-sol@high", "claude:claude-opus-5-5@high"]
        spec, _, note = crew.choose_agent(self.cfg, ref, caller="codex", explicit=True, balance=True)
        self.assertEqual(crew.spec_str(spec), "codex:gpt-6.1-sol@high")
        self.assertIsNone(note)

    def test_no_installed_still_errors(self):
        crew.backend_cmd = lambda cfg, b: None
        with self.assertRaises(crew.CrewError):
            crew.choose_agent(self.cfg, ["codex:m"])


class TestQuota(Routing):
    def test_limit_marks_backend_exhausted(self):
        crew.record_limit("codex", cooldown_minutes=30)
        self.assertEqual(self.backends(["codex:m", "claude:c"]), ["claude:c"])

    def test_limit_with_reset_time_in_future(self):
        crew.record_limit("codex", resets_at=time.time() + 3600)
        entry = crew.read_quota()["codex"]
        self.assertAlmostEqual(entry["limited_until"], time.time() + 3600, delta=5)

    def test_high_window_skips_backend(self):
        self.set_windows("codex", {"five_hour": {"used": 0.97, "resets_at": time.time() + 600}})
        self.assertEqual(self.backends(["codex:m", "claude:c"]), ["claude:c"])

    def test_all_exhausted_keeps_them_with_note(self):
        self.set_windows("codex", {"five_hour": {"used": 0.99, "resets_at": time.time() + 600}})
        self.set_windows("claude", {"five_hour": {"used": 1.0, "resets_at": time.time() + 600}})
        spec, _, note = crew.choose_agent(self.cfg, ["codex:m", "claude:c"])
        self.assertEqual(spec["backend"], "codex")
        self.assertIn("all candidates are near their usage limit", note)

    def test_expired_window_ignored(self):
        self.set_windows("codex", {"five_hour": {"used": 0.99, "resets_at": time.time() - 10}})
        self.assertEqual(self.backends(["codex:m", "claude:c"]), ["codex:m", "claude:c"])
        self.assertEqual(crew.quota_score(crew.read_quota()["codex"]), 0.0)

    def test_expired_limit_ignored(self):
        entry = {"windows": {}, "limited_until": time.time() - 10}
        self.assertEqual(crew.quota_score(entry), 0.0)

    def test_record_quota_keeps_limit(self):
        crew.record_limit("codex", cooldown_minutes=30)
        self.set_windows("codex", {"five_hour": {"used": 0.1, "resets_at": None}})
        entry = crew.read_quota()["codex"]
        self.assertIsNotNone(entry["limited_until"])
        self.assertEqual(entry["source"], "test")

    def test_bad_quota_file_reads_empty(self):
        crew.QUOTA_FILE.write_text("{not json")
        self.assertEqual(crew.read_quota(), {})

    def test_score_none_without_data(self):
        self.assertIsNone(crew.quota_score(None))
        self.assertIsNone(crew.quota_score({}))
        self.assertFalse(crew.quota_exhausted({}))


class TestBalance(Routing):
    def test_headroom_wins(self):
        self.set_windows("codex", {"five_hour": {"used": 0.8, "resets_at": None}})
        self.set_windows("claude", {"five_hour": {"used": 0.1, "resets_at": None}})
        spec, _, _ = crew.choose_agent(self.cfg, ["codex:m", "claude:c"], balance=True)
        self.assertEqual(spec["backend"], "claude")

    def test_without_balance_order_kept(self):
        self.set_windows("codex", {"five_hour": {"used": 0.8, "resets_at": None}})
        self.set_windows("claude", {"five_hour": {"used": 0.1, "resets_at": None}})
        spec, _, _ = crew.choose_agent(self.cfg, ["codex:m", "claude:c"])
        self.assertEqual(spec["backend"], "codex")

    def test_similar_headroom_keeps_list_order(self):
        self.set_windows("codex", {"five_hour": {"used": 0.11, "resets_at": None}})
        self.set_windows("claude", {"five_hour": {"used": 0.2, "resets_at": None}})
        self.assertEqual(self.backends(["codex:m", "claude:c"], balance=True), ["codex:m", "claude:c"])

    def test_unknown_quota_uses_default(self):
        # codex unknown -> 0.5 -> bucket 2; claude at 0.3 -> bucket 1: claude first.
        self.set_windows("claude", {"five_hour": {"used": 0.3, "resets_at": None}})
        self.assertEqual(self.backends(["codex:m", "claude:c"], balance=True), ["claude:c", "codex:m"])
        self.cfg["unknown_quota"] = 0.1
        # codex unknown -> 0.1 -> bucket 0; claude bucket 1: list order kept.
        self.assertEqual(self.backends(["codex:m", "claude:c"], balance=True), ["codex:m", "claude:c"])


class TestParsers(unittest.TestCase):
    def test_claude_allowed(self):
        ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.06, "resetsAt": 1791458400},
            "seven_day": {"utilization": 0.33, "resetsAt": 1791622800}}}}
        self.assertEqual(crew.claude_quota_windows(ev), {
            "five_hour": {"used": 0.06, "resets_at": 1791458400},
            "seven_day": {"used": 0.33, "resets_at": 1791622800}})

    def test_claude_rejected(self):
        ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1791458400}}
        self.assertEqual(crew.claude_quota_windows(ev), {"limit": {"used": 1.0, "resets_at": 1791458400}})

    def test_claude_other_events(self):
        self.assertIsNone(crew.claude_quota_windows({"type": "assistant"}))
        self.assertIsNone(crew.claude_quota_windows({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}))
        self.assertIsNone(crew.claude_quota_windows(None))


class TestCodexWindows(unittest.TestCase):
    SID = "0199abcd-1234-7000-8000-000000000001"

    def make_tree(self, base, lines, day="2026/10/08", name_sid=None):
        d = base / day
        d.mkdir(parents=True)
        f = d / f"rollout-2026-10-08T10-00-00-{name_sid or self.SID}.jsonl"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f

    def test_nested_rate_limits_last_wins(self):
        with temp_dir() as tmp:
            base = Path(tmp) / "sessions"
            old = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": 1}}}}
            new = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "primary": {"used_percent": 4.0, "window_minutes": 300, "resets_at": 1791484245},
                "secondary": {"used_percent": 27.0, "window_minutes": 10080, "resets_at": 1791962803}}}}
            self.make_tree(base, ["not json", json.dumps({"type": "other"}), json.dumps(old), json.dumps(new)])
            self.assertEqual(crew.codex_quota_windows(self.SID, sessions_dir=base), {
                "five_hour": {"used": 0.04, "resets_at": 1791484245},
                "seven_day": {"used": 0.27, "resets_at": 1791962803}})

    def test_finds_session_in_older_day(self):
        with temp_dir() as tmp:
            base = Path(tmp) / "sessions"
            line = json.dumps({"rate_limits": {"primary": {"used_percent": 50.0, "window_minutes": 45}}})
            self.make_tree(base, [line], day="2026/09/01")
            self.make_tree(base, [], day="2026/10/08", name_sid="other-session")
            self.assertEqual(crew.codex_quota_windows(self.SID, sessions_dir=base),
                             {"45_min": {"used": 0.5, "resets_at": None}})

    def test_missing_returns_none(self):
        with temp_dir() as tmp:
            base = Path(tmp) / "sessions"
            self.assertIsNone(crew.codex_quota_windows(self.SID, sessions_dir=base))
            self.make_tree(base, [json.dumps({"type": "event_msg"})])
            self.assertIsNone(crew.codex_quota_windows(self.SID, sessions_dir=base))


class TestLimitText(unittest.TestCase):
    def test_positives(self):
        for text in ["You've hit your usage limit", "usage limit reached", "Error: rate limit exceeded",
                     "rate_limit_error", "Too Many Requests", "HTTP 429", "status 429",
                     "quota exceeded for project", "RESOURCE_EXHAUSTED",
                     "Claude AI usage limit reached|1791458400"]:
            self.assertTrue(crew.looks_rate_limited(text), text)

    def test_negatives(self):
        for text in ["limit the output to 10 lines", "line 429 of file.py", "the limit is 5",
                     "rate limiter config", "", None]:
            self.assertFalse(crew.looks_rate_limited(text), text)

    def test_reset_from_text(self):
        soon = int(time.time()) + 7200
        self.assertEqual(crew.limit_reset_from_text(f"Claude AI usage limit reached|{soon}"), float(soon))
        self.assertIsNone(crew.limit_reset_from_text("usage limit reached"))
        # implausible times (past, or decades ahead) and pipes far from any limit message are ignored
        self.assertIsNone(crew.limit_reset_from_text("usage limit reached|1000000000"))
        self.assertIsNone(crew.limit_reset_from_text("usage limit reached|9999999999"))
        self.assertIsNone(crew.limit_reset_from_text(f"table | {soon}"))

    def test_bad_values_never_raise(self):
        self.assertIsNone(crew.claude_quota_windows(
            {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": "25%"}}}}))
        self.assertEqual(crew.quota_score({"windows": {"w": {"used": "x", "resets_at": "soon"}}}), 0.0)
        self.assertIsNone(crew.quota_score({"windows": {}, "limited_until": "later"}))

    def test_window_without_reset_expires(self):
        crew.record_quota("claude", {"limit": {"used": 1.0, "resets_at": None}}, "test")
        entry = crew.read_quota()["claude"]
        self.assertTrue(crew.quota_exhausted(entry))
        self.assertFalse(crew.quota_exhausted(entry, now=time.time() + 2 * 3600))


if __name__ == "__main__":
    unittest.main()
