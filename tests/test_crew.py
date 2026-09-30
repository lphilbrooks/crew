"""crew tests. Run from the repo root:  python -m unittest discover -s tests -v

End-to-end tests launch the real runner (hidden) against tests/fake_agent.py, so they
need git but no vendor CLI, account or network.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "skills" / "crew" / "scripts" / "crew.py"
FAKE = Path(__file__).resolve().parent / "fake_agent.py"
sys.path.insert(0, str(CREW.parent))
import crew  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class Sandbox(unittest.TestCase):
    """A temporary CREW_HOME whose backends are the fake agent, plus a git repo."""

    def setUp(self):
        try:
            self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        except TypeError:  # Python < 3.10
            self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "crew-home"
        self.home.mkdir()
        self.repo = base / "repo"
        (self.repo / "sub").mkdir(parents=True)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("one\n")
        (self.repo / "sub" / "b.txt").write_text("two\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        self.agy_settings = base / "agy-settings.json"
        cfg = {"view": "hidden",
               "backends": {"codex": {"exe": [sys.executable, str(FAKE), "codex"]},
                            "claude": {"exe": [sys.executable, str(FAKE), "claude"]},
                            "agy": {"exe": [sys.executable, str(FAKE), "agy"], "settings": str(self.agy_settings)}}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.env = dict(os.environ, CREW_HOME=str(self.home), CREW_CONFIG=str(self.home / "config.json"),
                        CREW_CALLER="none", CLAUDECODE="1")
        self.env.pop("CREW_TASK_DIR", None)
        self.env.pop("CODEX_HOME", None)

    def tearDown(self):
        self.tmp.cleanup()

    def crew(self, *args, mode="ok", **extra_env):
        env = dict(self.env, FAKE_MODE=mode, **extra_env)
        r = subprocess.run([sys.executable, str(CREW), *args], capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=180)
        return r.returncode, r.stdout + r.stderr

    def task_dir(self, out):
        line = [l for l in out.splitlines() if l.startswith("crew: dir=")][-1]
        return Path(line.split("=", 1)[1])

    def result(self, out):
        return json.loads((self.task_dir(out) / "result.json").read_text(encoding="utf-8"))

    def cmd(self, out):
        return json.loads((self.task_dir(out) / "cmd.json").read_text(encoding="utf-8"))


class TestSpecs(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(crew.parse_spec("codex:gpt-6.1-sol@high"),
                         {"backend": "codex", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(crew.parse_spec("agy:gemini-3.8-flash-medium")["effort"], None)
        self.assertEqual(crew.parse_spec("claude:opus@max")["backend"], "claude")
        self.assertIsNone(crew.parse_spec("gemini:flash"))
        self.assertIsNone(crew.parse_spec("nonsense"))

    def test_aliases_and_fallback(self):
        cfg = {"agents": {"a": ["agy:m1", "b"], "b": "codex:m2@low"}}
        self.assertEqual([crew.spec_str(s) for s in crew.expand_agent(cfg, "a")], ["agy:m1", "codex:m2@low"])
        with self.assertRaises(crew.CrewError):
            crew.expand_agent(cfg, "missing")

    def test_session_vars_stripped_config_kept(self):
        saved = dict(os.environ)
        try:
            os.environ.update(CLAUDE_CODE_SESSION_ID="s", CLAUDE_CODE_MESSAGING_TOKEN="t", CLAUDE_PID="1",
                              CLAUDE_EFFORT="max", CODEX_THREAD_ID="x", CODEX_CI="1",
                              CLAUDE_CODE_USE_BEDROCK="1", CODEX_HOME="/h")
            env = crew.agent_env({"env": {"CREW_TASK_DIR": "d"}})
            for gone in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT",
                         "CODEX_THREAD_ID", "CODEX_CI"):
                self.assertNotIn(gone, env)
            self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")
            self.assertEqual(env["CODEX_HOME"], "/h")
            self.assertEqual(env["CREW_TASK_DIR"], "d")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_alias_cycle_is_an_error(self):
        with self.assertRaises(crew.CrewError):
            crew.expand_agent({"agents": {"x": "y", "y": "x"}}, "x")

    def test_deep_merge(self):
        merged = crew.deep_merge({"roles": {"r": {"agent": "a", "access": "read"}}},
                                 {"roles": {"r": {"agent": "b"}}})
        self.assertEqual(merged["roles"]["r"], {"agent": "b", "access": "read"})

    def test_agy_cannot_verify(self):
        with self.assertRaises(crew.CrewError):
            crew.enforcement("agy", {"access": "verify", "web": False, "network": False}, True, False)


@unittest.skipUnless(os.name == "nt", "WMI fallback is Windows-only")
class TestWmiLaunch(unittest.TestCase):
    def test_environment_reaches_wmi_launched_process(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "env.txt"
            os.environ["CREW_WMI_PROBE"] = "probe ö"
            try:
                ok = crew.launch_wmi([sys.executable, "-c",
                                      f"import os; open(r'{out}', 'w', encoding='utf-8')"
                                      f".write(os.environ.get('CREW_WMI_PROBE', 'MISSING'))"], Path(tmp))
            finally:
                del os.environ["CREW_WMI_PROBE"]
            if not ok:
                self.skipTest("WMI process creation unavailable here")
            for _ in range(60):
                if out.exists() and out.read_text(encoding="utf-8"):
                    break
                time.sleep(0.5)
            self.assertEqual(out.read_text(encoding="utf-8"), "probe ö")


class TestGitState(Sandbox):
    def test_detects_edit_to_already_dirty_file_from_subdir(self):
        top = crew.git_top(self.repo / "sub")
        self.assertEqual(Path(top).resolve(), self.repo.resolve())
        (self.repo / "sub" / "b.txt").write_text("dirty\n")
        before = crew.git_state(top)
        (self.repo / "sub" / "b.txt").write_text("dirtier\n")
        (self.repo / "new file é.txt").write_text("n\n")
        after = crew.git_state(top)
        self.assertEqual(crew.changed_paths(before, after), ["new file é.txt", "sub/b.txt"])


class TestEndToEnd(Sandbox):
    def test_codex_check_ok(self):
        rc, out = self.crew("run", "--role", "check", "--cd", str(self.repo), "--task", "Is a.txt fine?", "--wait")
        self.assertEqual(rc, 0, out)
        r = self.result(out)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["tokens"], 75)
        self.assertEqual(r["session_id"], "00000000-0000-0000-0000-000000000001")
        argv = self.cmd(out)["argv"]
        self.assertIn("workspace-write", argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        text = (self.task_dir(out) / "task.md").read_text(encoding="utf-8")
        self.assertIn("[crew task scope]", text)

    def test_access_read_uses_readonly_sandbox_and_web_flag(self):
        rc, out = self.crew("run", "--role", "check", "--access", "read", "--web", "--cd", str(self.repo),
                            "--task", "x", "--wait")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        self.assertIn("read-only", argv)
        self.assertIn('web_search="live"', argv)

    def test_agent_exit_code_is_kept(self):
        rc, out = self.crew("run", "--role", "check", "--cd", str(self.repo), "--task", "x", "--wait", mode="fail")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.result(out)["status"], "agent-error")
        self.assertEqual(self.result(out)["exit"], 3)

    def test_verify_violation_from_subdir(self):
        rc, out = self.crew("run", "--role", "check", "--cd", str(self.repo / "sub"), "--task", "x", "--wait",
                            mode="touch")
        self.assertEqual(rc, 2, out)
        r = self.result(out)
        self.assertEqual(r["status"], "scope-violation")
        self.assertEqual(r["violations"], ["sub/touched-by-agent.txt"])

    def test_write_lists_touched_files(self):
        rc, out = self.crew("run", "--role", "implement", "--cd", str(self.repo), "--task", "x", "--wait",
                            mode="touch")
        self.assertEqual(rc, 0, out)
        r = self.result(out)
        self.assertEqual(r["touched_files"], ["touched-by-agent.txt"])
        self.assertEqual(r["violations"], [])

    def test_builtin_review_without_task(self):
        (self.repo / "a.txt").write_text("changed\n")
        rc, out = self.crew("run", "--role", "review", "--cd", str(self.repo), "--wait")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        self.assertIn("review", argv)
        self.assertIn("--uncommitted", argv)
        self.assertFalse(self.cmd(out)["stdin"])

    def test_review_with_task_on_agy_embeds_diff(self):
        (self.repo / "a.txt").write_text("changed\n")
        rc, out = self.crew("run", "--role", "review", "--agent", "agy:m", "--access", "read", "--cd", str(self.repo),
                            "--task", "Focus on a.txt", "--wait")
        self.assertEqual(rc, 0, out)
        text = (self.task_dir(out) / "task.md").read_text(encoding="utf-8")
        self.assertIn("<diff>", text)
        self.assertIn("+changed", text)

    def test_agy_research_ok_and_fetch_warning(self):
        rc, out = self.crew("run", "--role", "research", "--cd", str(self.repo), "--task", "Latest X?", "--wait")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.result(out)["status"], "ok")
        text = (self.task_dir(out) / "task.md").read_text(encoding="utf-8")
        self.assertIn("page fetching is not permitted", text)
        self.assertIn("NOT allowed", out)

    def test_setup_agy_web_then_fetch_allowed(self):
        self.agy_settings.write_text(json.dumps({"theme": "x"}))
        rc, out = self.crew("setup-agy-web")
        self.assertEqual(rc, 0, out)
        data = json.loads(self.agy_settings.read_text())
        self.assertEqual(data["permissions"]["allow"], ["read_url(*)"])
        self.assertEqual(data["theme"], "x")
        self.assertTrue(list(self.agy_settings.parent.glob("agy-settings.json.bak-*")))
        rc, out = self.crew("run", "--role", "research", "--cd", str(self.repo), "--task", "q", "--wait")
        self.assertIn("read web pages", (self.task_dir(out) / "task.md").read_text(encoding="utf-8"))

    def test_agy_permission_denied(self):
        rc, out = self.crew("run", "--role", "research", "--cd", str(self.repo), "--task", "q", "--wait", mode="deny")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.result(out)["status"], "permission-denied")

    def test_agy_verify_refused(self):
        rc, out = self.crew("run", "--role", "check", "--agent", "agy:m", "--cd", str(self.repo), "--task", "x")
        self.assertEqual(rc, 1)
        self.assertIn("cannot build or test", out)

    def test_long_agy_task_passed_by_file(self):
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "y" * 20000, "--wait")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        self.assertIn(str(self.task_dir(out)), argv)
        self.assertIn("task.md", argv[-1])

    def test_empty_answer(self):
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "x", "--wait", mode="silent")
        self.assertEqual(self.result(out)["status"], "empty")

    def test_timeout_kills_agent(self):
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "x", "--max-minutes", "0.05",
                            "--wait", mode="sleep")
        self.assertEqual(self.result(out)["status"], "timeout")

    def test_resume_codex(self):
        rc, out = self.crew("run", "--role", "implement", "--cd", str(self.repo), "--task", "x", "--wait")
        first = self.task_dir(out)
        rc, out = self.crew("run", "--resume", str(first), "--task", "fix it", "--wait")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        self.assertIn("resume", argv)
        self.assertIn("00000000-0000-0000-0000-000000000001", argv)

    def test_set_role_and_agent(self):
        rc, out = self.crew("set-agent", "mine", "agy:m1", "codex:m2@low")
        self.assertEqual(rc, 0, out)
        rc, out = self.crew("set-role", "review", "--agent", "mine", "--network")
        self.assertEqual(rc, 0, out)
        cfg = json.loads((self.home / "config.json").read_text())
        self.assertEqual(cfg["agents"]["mine"], ["agy:m1", "codex:m2@low"])
        self.assertEqual(cfg["roles"]["review"], {"agent": "mine", "network": True})
        rc, out = self.crew("roles")
        self.assertIn("agy:m1", out)
        rc, out = self.crew("set-role", "review", "--agent", "bogus")
        self.assertEqual(rc, 1)

    def test_status_and_stats(self):
        self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "x", "--wait")
        rc, out = self.crew("status")
        self.assertIn("ok", out)
        rc, out = self.crew("stats")
        self.assertIn("agy:", out)

    def test_silent_terminal_falls_back_to_hidden(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg.update(view="tab", terminal=[sys.executable, "-c", "pass"])  # "succeeds", opens nothing
        (self.home / "config.json").write_text(json.dumps(cfg))
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "x", "--wait")
        self.assertEqual(rc, 0, out)
        self.assertIn("running hidden instead", out)

    def test_second_runner_does_not_rerun(self):
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "x", "--wait")
        task = self.task_dir(out)
        before = (task / "result.json").read_text(encoding="utf-8")
        rc, out = self.crew("_run", "--dir", str(task))
        self.assertIn("already being run", out)
        self.assertEqual((task / "result.json").read_text(encoding="utf-8"), before)

    def test_dead_runner_detected(self):
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "x", mode="sleep")
        task = self.task_dir(out)
        import time
        for _ in range(60):
            if (task / "run.json").exists():
                break
            time.sleep(0.5)
        info = json.loads((task / "run.json").read_text())
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(info["pid"])], capture_output=True)
        else:
            os.kill(info["pid"], 9)
        info["beat"] = 0
        (task / "run.json").write_text(json.dumps(info))
        rc, out = self.crew("collect", str(task), "--timeout", "60")
        self.assertEqual(json.loads((task / "result.json").read_text())["status"], "harness-error")


class TestClaude(Sandbox):
    def settings(self, out):
        return json.loads((self.task_dir(out) / "claude-settings.json").read_text(encoding="utf-8"))

    def arg_after(self, argv, flag):
        return argv[argv.index(flag) + 1]

    def test_verify_scope(self):
        rc, out = self.crew("run", "--role", "check", "--agent", "claude:opus@high", "--cd", str(self.repo),
                            "--task", "x", "--wait")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        tools = self.arg_after(argv, "--tools").split(",")
        self.assertIn("Bash", tools)
        self.assertNotIn("Edit", tools)
        self.assertNotIn("WebFetch", tools)
        self.assertEqual(self.arg_after(argv, "--permission-mode"), "dontAsk")
        self.assertEqual(self.arg_after(argv, "--effort"), "high")
        for flag in ("--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        settings = self.settings(out)
        if os.name == "nt":
            self.assertNotIn("sandbox", settings)
            self.assertIn("Bash", settings["permissions"]["allow"])
        else:
            self.assertEqual(settings["sandbox"]["filesystem"]["denyWrite"], [crew.git_top(self.repo)])
            self.assertEqual(settings["sandbox"]["network"]["allowedDomains"], [])
        r = self.result(out)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["session_id"], "11111111-1111-1111-1111-111111111111")
        self.assertAlmostEqual(r["cost_usd"], 0.0123)
        self.assertEqual(r["tokens"], 75)
        answer = (self.task_dir(out) / "final.md").read_text(encoding="utf-8")
        self.assertIn('"CLAUDECODE": false', answer)      # caller marker stripped
        self.assertIn('"CREW_TASK_DIR": true', answer)    # nesting guard set

    def test_read_scope_with_web(self):
        rc, out = self.crew("run", "--role", "light", "--agent", "claude:haiku", "--web", "--cd", str(self.repo),
                            "--task", "x", "--wait")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        self.assertEqual(self.arg_after(argv, "--tools"), "Read,Glob,Grep,WebSearch,WebFetch")
        self.assertNotIn("--effort", argv)
        self.assertEqual(self.settings(out)["permissions"]["allow"], ["WebSearch", "WebFetch"])

    def test_write_scope(self):
        rc, out = self.crew("run", "--role", "implement", "--agent", "claude:sonnet", "--cd", str(self.repo),
                            "--task", "x", "--wait")
        argv = self.cmd(out)["argv"]
        self.assertIn("Edit", self.arg_after(argv, "--tools").split(","))
        self.assertEqual(self.arg_after(argv, "--permission-mode"), "acceptEdits")

    def test_denied_tools_reported(self):
        rc, out = self.crew("run", "--role", "check", "--agent", "claude:opus", "--cd", str(self.repo),
                            "--task", "x", "--wait", mode="deny")
        r = self.result(out)
        self.assertEqual(r["status"], "permission-denied")
        self.assertEqual(r["denied_tools"], ["Bash"])

    def test_error_result(self):
        rc, out = self.crew("run", "--role", "check", "--agent", "claude:opus", "--cd", str(self.repo),
                            "--task", "x", "--wait", mode="fail")
        self.assertEqual(self.result(out)["status"], "agent-error")

    def test_review_embeds_diff_and_resume(self):
        (self.repo / "a.txt").write_text("changed\n")
        rc, out = self.crew("run", "--role", "review", "--agent", "claude:opus", "--cd", str(self.repo), "--wait")
        self.assertEqual(rc, 0, out)
        self.assertIn("+changed", (self.task_dir(out) / "task.md").read_text(encoding="utf-8"))
        rc, out = self.crew("run", "--resume", str(self.task_dir(out)), "--task", "and the tests?", "--wait")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        self.assertEqual(self.arg_after(argv, "--resume"), "11111111-1111-1111-1111-111111111111")


class TestCaller(Sandbox):
    def agent_for(self, caller, *extra):
        rc, out = self.crew("run", "--role", "review", "--cd", str(self.repo), "--task", "x", *extra,
                            CREW_CALLER=caller)
        self.assertEqual(rc, 0, out)
        return self.cmd(out)["backend"]

    def test_skips_own_vendor(self):
        (self.repo / "a.txt").write_text("changed\n")
        self.assertEqual(self.agent_for("claude"), "codex")
        self.assertEqual(self.agent_for("codex"), "claude")
        self.assertEqual(self.agent_for("none"), "codex")

    def test_explicit_agent_wins(self):
        (self.repo / "a.txt").write_text("changed\n")
        self.assertEqual(self.agent_for("codex", "--agent", "codex:m"), "codex")

    def test_caller_flag_and_same_vendor_note(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["roles"] = {"solo": {"agent": "codex:m", "access": "read"}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        rc, out = self.crew("run", "--role", "solo", "--caller", "codex", "--cd", str(self.repo), "--task", "x")
        self.assertIn("also the caller", out)

    def test_nested_dispatch_refused(self):
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo), "--task", "x",
                            CREW_TASK_DIR="/somewhere")
        self.assertEqual(rc, 1)
        self.assertIn("cannot delegate further", out)

    def test_detect_caller_markers(self):
        keys = ("CREW_CALLER", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_MANAGED_BY_NPM")
        saved = {k: os.environ.pop(k, None) for k in keys}
        try:
            os.environ["CLAUDECODE"] = "1"
            self.assertEqual(crew.detect_caller(), "claude")
            del os.environ["CLAUDECODE"]
            os.environ["CODEX_MANAGED_BY_NPM"] = "1"
            self.assertEqual(crew.detect_caller(), "codex")
            os.environ["CREW_CALLER"] = "agy"
            self.assertEqual(crew.detect_caller(), "agy")
        finally:
            for k in keys:
                os.environ.pop(k, None)
                if saved[k] is not None:
                    os.environ[k] = saved[k]


if __name__ == "__main__":
    unittest.main()
