"""Provider tests: opencode, the api backend, claude/codex routed to other providers, command-line
agents defined in the config, and model families from model names.

End-to-end tests use tests/fake_agent.py and a local HTTP server standing in for an
OpenAI-compatible API, so they need no accounts or network.
Run from the repo root:  python -m unittest tests.test_providers -v
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_crew import FAKE, Sandbox, crew  # noqa: E402


class TestSpecSyntax(unittest.TestCase):
    CFG = {"providers": {"openrouter": {"base_url": "https://x/v1", "key_env": "K"}},
           "backends": {"plain": {"command": ["plain"]}}}

    def test_provider_and_colon_models(self):
        spec = crew.parse_spec("claude+openrouter:z-ai/glm-5@high")
        self.assertEqual(spec, {"backend": "claude", "provider": "openrouter", "model": "z-ai/glm-5", "effort": "high"})
        self.assertEqual(crew.spec_str(spec), "claude+openrouter:z-ai/glm-5@high")
        self.assertEqual(crew.parse_spec("codex+ollama:gpt-oss:20b")["model"], "gpt-oss:20b")
        vertex = crew.parse_spec("opencode:google-vertex-anthropic/claude-x@20250514@high")
        self.assertEqual((vertex["model"], vertex["effort"]), ("google-vertex-anthropic/claude-x@20250514", "high"))
        self.assertEqual(crew.parse_spec("opencode:openrouter/deepseek/deepseek-v4")["model"],
                         "openrouter/deepseek/deepseek-v4")

    def test_validation(self):
        ok = crew.expand_agent(self.CFG, ["opencode:a/b", "api:openrouter/m", "plain:m", "claude+openrouter:m"])
        self.assertEqual(len(ok), 4)
        for bad in ("gemini:flash", "opencode+openrouter:m", "api:m", "claude+nowhere:m"):
            with self.assertRaises(crew.CrewError, msg=bad):
                crew.expand_agent(self.CFG, bad)

    def test_provider_and_quota_key(self):
        self.assertEqual(crew.spec_provider(crew.parse_spec("opencode:openrouter/x/y")), "openrouter")
        self.assertEqual(crew.spec_provider(crew.parse_spec("claude+deepseek:m")), "deepseek")
        self.assertIsNone(crew.spec_provider(crew.parse_spec("claude:m")))
        self.assertEqual(crew.quota_key(crew.parse_spec("api:groq/llama-4")), "groq")
        self.assertEqual(crew.quota_key(crew.parse_spec("codex:gpt-6")), "codex")
        self.assertEqual(crew.api_model(crew.parse_spec("api:openrouter/anthropic/claude-x")), "anthropic/claude-x")


class TestFamilies(unittest.TestCase):
    CFG = {"providers": {"deepseek": {"family": "deepseek"}, "local": {}}}

    def fam(self, text):
        return crew.model_family(crew.parse_spec(text), self.CFG)

    def test_from_model_names(self):
        cases = {
            "opencode:openrouter/anthropic/claude-sonnet-5-5": "anthropic",
            "opencode:openrouter/~anthropic/claude-sonnet-latest": "anthropic",
            "opencode:anthropic/claude-haiku-5-5": "anthropic",
            "api:openrouter/openai/gpt-oss-120b": "openai",
            "api:groq/llama-4-scout": "meta",
            "api:openrouter/deepseek/deepseek-v4:free": "deepseek",
            "codex+ollama:qwen3-coder:30b": "alibaba",
            "claude+openrouter:z-ai/glm-5": "zhipu",
            "claude+moonshot:kimi-k3": "moonshot",
            "api:xai/grok-5": "xai",
            "api:mistral/devstral-medium": "mistral",
            "opencode:google/gemini-3.1-pro": "google",
            "api:openrouter/minimax/minimax-m3": "minimax",
        }
        for text, family in cases.items():
            self.assertEqual(self.fam(text), family, text)

    def test_fallbacks(self):
        self.assertEqual(self.fam("api:deepseek/chat-latest"), "deepseek")  # provider's family
        self.assertEqual(self.fam("api:local/my-model"), "local")  # provider name
        self.assertEqual(self.fam("claude:anything"), "anthropic")
        self.assertEqual(self.fam("agy:something-else"), "google")

    def test_org_names_need_a_boundary(self):
        self.assertIsNone(crew.family_from_name("olmo-ish/stepping"))  # no 'step' family from 'stepping'
        self.assertEqual(crew.family_from_name("o3-mini"), "openai")

    def test_caller_family(self):
        self.assertEqual(crew.caller_family("claude"), "anthropic")
        self.assertEqual(crew.caller_family("opencode:openrouter/anthropic/claude-opus-5-5"), "anthropic")
        self.assertIsNone(crew.caller_family("opencode"))
        self.assertTrue(crew.valid_caller("opencode"))
        self.assertFalse(crew.valid_caller("not a caller"))


class TestScopes(unittest.TestCase):
    def test_opencode_permissions(self):
        read = crew.opencode_config({"access": "read", "web": True, "network": False})["permission"]
        self.assertEqual((read["*"], read["edit"], read["bash"], read["webfetch"]), ("deny", "deny", "deny", "allow"))
        verify = crew.opencode_config({"access": "verify", "web": False, "network": False})["permission"]
        self.assertEqual((verify["edit"], verify["bash"], verify["webfetch"]), ("deny", "allow", "deny"))
        write = crew.opencode_config({"access": "write", "web": False, "network": False})
        self.assertEqual((write["permission"]["edit"], write["share"]), ("allow", "disabled"))

    def test_api_scopes(self):
        scope = {"access": "verify", "web": False, "network": False}
        self.assertIn("read-only", crew.enforcement("api", scope, True, False)["files"])
        for bad in ({"access": "write", "web": False, "network": False},
                    {"access": "read", "web": True, "network": False}):
            with self.assertRaises(crew.CrewError):
                crew.enforcement("api", bad, True, False)
        self.assertIn("no tools", crew.scope_preamble("api", scope, Path("s"), None, False))

    def test_command_backend_scopes(self):
        bcfg = {"access": ["read", "write"], "enforcement": {"write": "the CLI's own approval mode"}}
        self.assertEqual(crew.enforcement("plain", {"access": "write", "web": False, "network": False},
                                          True, False, bcfg)["files"], "the CLI's own approval mode")
        with self.assertRaises(crew.CrewError):
            crew.enforcement("plain", {"access": "verify", "web": False, "network": False}, True, False, bcfg)

    def test_command_args(self):
        bcfg = {"command": ["x", "--model", "{model}"], "effort_args": ["--effort", "{effort}"],
                "access_args": {"write": ["--yolo"]}, "web_args": ["--search"]}
        spec = crew.parse_spec("plain:m1")
        scope = {"access": "write", "web": True, "network": False}
        tail, stdin = crew.command_args(bcfg, spec, "high", scope, Path("t"), Path("c"), "hi")
        self.assertEqual(tail, ["--model", "m1", "--effort", "high", "--yolo", "--search"])
        self.assertTrue(stdin)
        tail, stdin = crew.command_args(dict(bcfg, command=["x", "-p", "{prompt}"]), spec, None,
                                        dict(scope, web=False), Path("t"), Path("c"), "hi")
        self.assertEqual(tail, ["-p", "hi", "--yolo"])
        self.assertFalse(stdin)

    def test_codex_provider_args(self):
        cfg = {"providers": {"openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY",
                                            "responses": True}}}
        spec = crew.parse_spec("codex+openrouter:openai/gpt-6")
        args = crew.codex_args(cfg, spec, None, {"access": "read", "web": False, "network": False},
                               Path("t"), Path("c"), "exec")
        joined = " ".join(args)
        self.assertIn('model_provider="crew-openrouter"', joined)
        self.assertIn('model_providers.crew-openrouter.base_url="https://openrouter.ai/api/v1"', joined)
        self.assertIn('model_providers.crew-openrouter.env_key="OPENROUTER_API_KEY"', joined)
        self.assertIn('wire_api="responses"', joined)


class TestProviderAvailability(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = crew.KEYS_FILE
        crew.KEYS_FILE = Path(self.tmp.name) / "keys.json"
        self.cfg = {"providers": {"p": {"base_url": "u", "key_env": "CREW_TEST_NO_SUCH_KEY", "anthropic_url": "a"},
                                  "local": {"base_url": "u", "key_env": None}}}

    def tearDown(self):
        crew.KEYS_FILE = self.saved
        self.tmp.cleanup()

    def test_key_from_file(self):
        spec = crew.parse_spec("claude+p:m")
        self.assertIn("no API key", crew.provider_unavailable(self.cfg, spec))
        crew.write_json(crew.KEYS_FILE, {"CREW_TEST_NO_SUCH_KEY": "sk"})
        self.assertIsNone(crew.provider_unavailable(self.cfg, spec))

    def test_capabilities(self):
        self.assertIn("Responses API", crew.provider_unavailable(self.cfg, crew.parse_spec("codex+local:m")))
        self.assertIn("Anthropic-compatible", crew.provider_unavailable(self.cfg, crew.parse_spec("claude+local:m")))
        self.assertIsNone(crew.provider_unavailable(self.cfg, crew.parse_spec("api:local/m")))
        self.assertIsNone(crew.provider_unavailable(self.cfg, crew.parse_spec("opencode:anything/m")))


class FakeAPI(http.server.BaseHTTPRequestHandler):
    """Stands in for an OpenAI-compatible API. server.mode: ok | 429 | strict (rejects optional fields)."""

    def do_GET(self):
        if self.path.endswith("/models"):
            models = getattr(self.server, "models", ["fake/model-b", "fake/model-a"])
            self.reply(200, {"data": [{"id": m} for m in models]})
        else:
            self.reply(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        self.server.requests.append({"body": body, "auth": self.headers.get("Authorization")})
        if self.server.mode == "429":
            return self.reply(429, {"error": {"message": "Rate limit exceeded"}})
        if self.server.mode == "strict" and "stream_options" in body:
            return self.reply(400, {"error": {"message": "unknown field stream_options"}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        turns = len(body["messages"])
        for piece in ["fake api answer", f" (turns={turns})\nsecond", " line"]:
            chunk = {"model": body["model"], "choices": [{"delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        usage = {"model": body["model"], "choices": [],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.001}}
        self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())

    def reply(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class ProviderSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        self.server.mode, self.server.requests = "ok", []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}/v1"
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["backends"]["opencode"] = {"exe": [sys.executable, str(FAKE), "opencode"]}
        cfg["backends"]["plain"] = {"command": ["plain", "--model", "{model}"],
                                    "exe": [sys.executable, str(FAKE), "plain"], "access": ["read"],
                                    "family": "plainco"}
        cfg["providers"] = {"local": {"base_url": base, "key_env": "CREW_TEST_API_KEY"},
                            "router": {"base_url": base, "key_env": "CREW_TEST_API_KEY",
                                       "anthropic_url": "http://127.0.0.1:1/anthropic"},
                            "keyless": {"base_url": base, "key_env": None, "responses": True},
                            "apikey": {"base_url": None, "key_env": "CREW_TEST_API_KEY",
                                       "anthropic_url": "http://127.0.0.1:1/coding/", "auth_env": "ANTHROPIC_API_KEY"},
                            "narrow": {"base_url": base, "key_env": None, "allowed_models": ["ok-model"]}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.env["NO_PROXY"] = self.env["no_proxy"] = "127.0.0.1,localhost"
        self.env.pop("CREW_TEST_API_KEY", None)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def run_task(self, *args, **env):
        return self.crew("run", "--role", "light", "--cd", str(self.repo), "--wait", *args, **env)


class TestOpencode(ProviderSandbox):
    def test_research_ok(self):
        rc, out = self.run_task("--agent", "opencode:openrouter/deepseek/deepseek-v4@high", "--web",
                                "--task", "look it up")
        self.assertEqual(rc, 0, out)
        self.assertIn("fake opencode answer; model=openrouter/deepseek/deepseek-v4", out)
        self.assertIn("edit=deny; bash=deny; webfetch=allow", out)
        self.assertIn('"--variant", "high"', out)
        r, cmd = self.result(out), self.cmd(out)
        self.assertEqual((r["status"], r["session_id"], r["tokens"], r["cost_usd"]), ("ok", "ses_fake1", 47, 0.0042))
        self.assertEqual((cmd["quota_key"], cmd["family"]), ("openrouter", "deepseek"))
        self.assertTrue((self.task_dir(out) / "opencode-config.json").exists())

    def test_write_scope_and_resume(self):
        rc, out = self.run_task("--agent", "opencode:anthropic/claude-sonnet-5-5", "--access", "write",
                                "--task", "edit it")
        self.assertEqual(rc, 0, out)
        self.assertIn("edit=allow; bash=allow", out)
        rc, again = self.crew("run", "--resume", str(self.task_dir(out)), "--task", "-and more", "--wait")
        self.assertEqual(rc, 0, again)
        self.assertIn('"-s", "ses_fake1"', again)
        self.assertIsNone(self.cmd(again)["prior_session_cost_usd"])

    def test_denied_tool(self):
        rc, out = self.run_task("--agent", "opencode:a/b", "--task", "x", mode="deny")
        self.assertEqual(self.result(out)["status"], "permission-denied", out)
        self.assertEqual(self.result(out)["denied_tools"], ["bash"])

    def test_limit_recorded_for_the_provider(self):
        rc, out = self.run_task("--agent", "opencode:openrouter/x/y", "--task", "x", FAKE_LIMITED="opencode")
        self.assertEqual(self.result(out)["status"], "limited", out)
        quota = json.loads((self.home / "quota.json").read_text())
        self.assertIn("openrouter", quota)
        self.assertNotIn("opencode", quota)

    def test_models_listing(self):
        rc, out = self.crew("models")
        self.assertIn("opencode:openrouter/deepseek/deepseek-v4", out)
        self.assertIn("local", out)


class TestApi(ProviderSandbox):
    def test_streamed_answer_cost_and_resume(self):
        rc, out = self.run_task("--agent", "api:local/fake/model-a@high", "--task", "summarise",
                                CREW_TEST_API_KEY="sk-test")
        self.assertEqual(rc, 0, out)
        r = self.result(out)
        self.assertEqual((r["status"], r["tokens"], r["cost_usd"]), ("ok", 15, 0.001))
        self.assertEqual(r["resolved_model"], "local/fake/model-a")
        final = (self.task_dir(out) / "final.md").read_text()
        self.assertEqual(final, "fake api answer (turns=1)\nsecond line\n")
        req = self.server.requests[-1]
        self.assertEqual(req["auth"], "Bearer sk-test")
        self.assertEqual(req["body"]["model"], "fake/model-a")
        self.assertIn("no tools", req["body"]["messages"][0]["content"])
        self.assertNotIn("sk-test", (self.task_dir(out) / "cmd.json").read_text())
        rc, again = self.crew("run", "--resume", str(self.task_dir(out)), "--task", "more", "--wait",
                              CREW_TEST_API_KEY="sk-test")
        self.assertEqual(rc, 0, again)
        self.assertIn("turns=3", again)

    def test_key_from_set_key(self):
        rc, out = self.crew("set-key", "local", input_text="sk-file\n")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_task("--agent", "api:local/m", "--task", "x")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.server.requests[-1]["auth"], "Bearer sk-file")

    def test_missing_key_is_not_installed(self):
        rc, out = self.run_task("--agent", "api:local/m", "--task", "x")
        self.assertEqual(rc, 1)
        self.assertIn("no API key", out)

    def test_rate_limit(self):
        self.server.mode = "429"
        rc, out = self.run_task("--agent", "api:keyless/m", "--task", "x")
        self.assertEqual(self.result(out)["status"], "limited", out)

    def test_strict_provider_gets_minimal_request(self):
        self.server.mode = "strict"
        rc, out = self.run_task("--agent", "api:keyless/m", "--task", "x")
        self.assertEqual(self.result(out)["status"], "ok", out)
        self.assertNotIn("stream_options", self.server.requests[-1]["body"])

    def test_write_refused(self):
        rc, out = self.run_task("--agent", "api:keyless/m", "--access", "write", "--task", "x")
        self.assertEqual(rc, 1)
        self.assertIn("cannot edit files", out)

    def test_provider_models(self):
        rc, out = self.crew("models", "--provider", "keyless")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.split(), ["api:keyless/fake/model-a", "api:keyless/fake/model-b"])


class TestRouting(ProviderSandbox):
    def test_claude_via_provider(self):
        rc, out = self.run_task("--agent", "claude+router:z-ai/glm-5", "--task", "x", CREW_TEST_API_KEY="sk-route")
        self.assertEqual(rc, 0, out)
        self.assertIn('"ANTHROPIC_AUTH_TOKEN": true', out)
        self.assertIn('"base_url": "http://127.0.0.1:1/anthropic"', out)
        cmd_text = (self.task_dir(out) / "cmd.json").read_text()
        self.assertNotIn("sk-route", cmd_text)
        cmd = json.loads(cmd_text)
        self.assertEqual((cmd["quota_key"], cmd["family"]), ("router", "zhipu"))
        self.assertEqual(cmd["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "z-ai/glm-5")

    def test_claude_provider_without_key(self):
        rc, out = self.run_task("--agent", "claude+router:m", "--task", "x")
        self.assertEqual(rc, 1)
        self.assertIn("no API key", out)

    def test_codex_via_keyless_provider(self):
        rc, out = self.run_task("--agent", "codex+keyless:gpt-oss:20b", "--task", "x")
        self.assertEqual(rc, 0, out)
        self.assertIn("model_provider=\\\"crew-keyless\\\"", out)

    def test_caller_opencode_skips_its_family(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["roles"] = {"duo": {"agent": ["opencode:anthropic/claude-sonnet-5-5", "codex:gpt-6"], "access": "read"}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        rc, out = self.crew("run", "--role", "duo", "--cd", str(self.repo), "--task", "x", "--wait",
                            "--caller", "opencode:openrouter/anthropic/claude-opus-5-5")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.cmd(out)["backend"], "codex")


class TestCommandBackend(ProviderSandbox):
    def test_plain_agent_reads_stdin(self):
        rc, out = self.run_task("--agent", "plain:m9", "--task", "hello there")
        self.assertEqual(rc, 0, out)
        self.assertIn('fake plain answer; argv=["--model", "m9"]', out)
        self.assertNotIn("stdin_chars=0", out)
        self.assertEqual(self.cmd(out)["family"], "plainco")

    def test_unsupported_access_refused(self):
        rc, out = self.run_task("--agent", "plain:m9", "--access", "write", "--task", "x")
        self.assertEqual(rc, 1)
        self.assertIn("configured for --access read only", out)


if __name__ == "__main__":
    unittest.main()


class TestOpencodeFixes(ProviderSandbox):
    """OpenCode 2's shared server, effort variants, PWD, stdin prompts and private databases."""

    def test_v2_standalone_and_effort_variant(self):
        rc, out = self.crew("run", "--role", "light", "--cd", str(self.repo / "sub"), "--wait", "--agent",
                            "opencode:openrouter/x/y@high", "--task", "hello", FAKE_OPENCODE_VERSION="2.0.31")
        self.assertEqual(rc, 0, out)
        self.assertIn('"run", "--standalone", "--format", "json", "-m", "openrouter/x/y#high"', out)
        self.assertNotIn("--variant", out)
        self.assertIn(f"pwd={self.repo / 'sub'}", out)
        self.assertIn("private --standalone server", json.dumps(self.cmd(out)["enforcement"]))

    def test_v1_prompt_on_stdin(self):
        rc, out = self.run_task("--agent", "opencode:a/b", "--task", "-starts with a dash\nand has two lines")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("--standalone", out)
        cmd = self.cmd(out)
        self.assertTrue(cmd["stdin"])
        self.assertNotIn("two lines", " ".join(cmd["argv"]))
        self.assertNotIn("prompt_chars=0", out)

    def test_private_db_follows_resume(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["backends"]["opencode"]["private_db"] = True
        (self.home / "config.json").write_text(json.dumps(cfg))
        rc, out = self.run_task("--agent", "opencode:a/b", "--task", "x")
        self.assertIn("db=True", out)
        first = self.cmd(out)["opencode_db"]
        self.assertTrue(first.startswith(str(self.task_dir(out))))
        rc, again = self.crew("run", "--resume", str(self.task_dir(out)), "--task", "more", "--wait")
        self.assertEqual(rc, 0, again)
        self.assertEqual(self.cmd(again)["opencode_db"], first)


class TestCodexIsolation(ProviderSandbox):
    def test_mcp_servers_disabled_by_name(self):
        rc, out = self.run_task("--agent", "codex:m", "--task", "x")
        self.assertEqual(rc, 0, out)
        argv = self.cmd(out)["argv"]
        self.assertIn("features.plugins=false", argv)
        self.assertIn("mcp_servers.docs.enabled=false", argv)
        self.assertNotIn("mcp_servers.off.enabled=false", argv)
        self.assertIn("1 codex MCP server(s) disabled", self.cmd(out)["enforcement"]["mcp"])

    def test_listing_failure_is_reported(self):
        rc, out = self.run_task("--agent", "codex:m", "--task", "x", FAKE_CODEX_MCP="fail")
        self.assertEqual(rc, 0, out)
        self.assertIn("could not list your codex MCP servers", self.cmd(out)["enforcement"]["mcp"])

    def test_unsafe_server_name_refused(self):
        rc, out = self.run_task("--agent", "codex:m", "--task", "x",
                                FAKE_CODEX_MCP='[{"name": "a.b", "enabled": true}]')
        self.assertEqual(rc, 1)
        self.assertIn("can't be disabled safely", out)


class TestWindowsShim(unittest.TestCase):
    def test_npm_shim_target(self):
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            script.parent.mkdir(parents=True)
            script.write_text("")
            shim = Path(tmp) / "codex.cmd"
            shim.write_text('@ECHO off\r\nSET "_prog=node"\r\n'
                            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & '
                            '"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
            node = shutil.which("node")
            expected = [node, os.path.normpath(str(script))] if node else None
            self.assertEqual(crew.windows_shim_target(str(shim)), expected)
            exe = Path(tmp) / "tool.exe"
            exe.write_text("")
            (Path(tmp) / "tool.cmd").write_text('@"%~dp0\\tool.exe" %*\r\n')
            self.assertEqual(crew.windows_shim_target(str(Path(tmp) / "tool.cmd")), [os.path.normpath(str(exe))])
            (Path(tmp) / "odd.cmd").write_text("@echo hi\r\n")
            self.assertIsNone(crew.windows_shim_target(str(Path(tmp) / "odd.cmd")))


class TestProviderSettings(ProviderSandbox):
    def test_auth_env_carries_the_key(self):
        rc, out = self.run_task("--agent", "claude+apikey:kimi-k3", "--task", "x", CREW_TEST_API_KEY="sk-k")
        self.assertEqual(rc, 0, out)
        self.assertIn('"ANTHROPIC_AUTH_TOKEN": false', out)
        self.assertIn('"ANTHROPIC_API_KEY": true', out)
        self.assertEqual(self.cmd(out)["secret_env"], {"ANTHROPIC_API_KEY": "CREW_TEST_API_KEY"})

    def test_api_needs_base_url(self):
        rc, out = self.run_task("--agent", "api:apikey/m", "--task", "x", CREW_TEST_API_KEY="sk-k")
        self.assertEqual(rc, 1)
        self.assertIn("no OpenAI-compatible endpoint", out)

    def test_allowed_models(self):
        rc, out = self.run_task("--agent", "api:narrow/other", "--task", "x")
        self.assertEqual(rc, 1)
        self.assertIn("not in providers.narrow.allowed_models", out)
        rc, out = self.run_task("--agent", "api:narrow/ok-model", "--task", "x")
        self.assertEqual(rc, 0, out)

    def test_same_model_through_another_provider_on_limit(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["roles"] = {"duo": {"agent": ["claude:claude-opus-5-5", "claude+router:anthropic/claude-opus-5-5"],
                                "access": "read"}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        rc, out = self.crew("run", "--role", "duo", "--cd", str(self.repo), "--task", "x", "--wait",
                            FAKE_LIMITED="claude-direct", CREW_TEST_API_KEY="sk-r")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.cmd(out)["agent"], "claude+router:anthropic/claude-opus-5-5")


class TestRunControls(ProviderSandbox):
    def test_worktree_kept_with_changes(self):
        rc, out = self.run_task("--agent", "codex:m", "--access", "write", "--worktree", "--task", "x", mode="touch")
        self.assertEqual(rc, 0, out)
        wt = self.result(out)["worktree"]
        self.assertTrue(wt["kept"])
        self.assertTrue((Path(wt["path"]) / "touched-by-agent.txt").exists())
        self.assertFalse((self.repo / "touched-by-agent.txt").exists())
        self.assertIn(wt["branch"], subprocess.run(["git", "-C", str(self.repo), "branch"], capture_output=True,
                                                   text=True).stdout)
        self.assertIn("merge or cherry-pick", out)

    def test_worktree_removed_without_changes(self):
        rc, out = self.run_task("--agent", "codex:m", "--worktree", "--task", "x")
        self.assertEqual(rc, 0, out)
        wt = self.result(out)["worktree"]
        self.assertFalse(wt["kept"])
        self.assertFalse(Path(wt["path"]).exists())
        self.assertNotIn(wt["branch"], subprocess.run(["git", "-C", str(self.repo), "branch"], capture_output=True,
                                                      text=True).stdout)

    def test_worktree_refused_for_reviews(self):
        rc, out = self.crew("run", "--role", "review", "--agent", "codex:m", "--cd", str(self.repo), "--worktree")
        self.assertEqual(rc, 1)
        self.assertIn("doesn't apply to reviews", out)

    def test_stall_kills_a_silent_agent(self):
        rc, out = self.run_task("--agent", "plain:m", "--stall-minutes", "0.05", "--task", "x", mode="stall")
        r = self.result(out)
        self.assertEqual(r["status"], "stalled", out)
        self.assertLess(r["seconds"], 60)

    def test_cost_cap(self):
        rc, out = self.run_task("--agent", "claude:m", "--max-cost", "0.001", "--task", "x",
                                FAKE_CLAUDE_HELP="--max-budget-usd <amount>")
        self.assertIn('"--max-budget-usd", "0.001"', json.dumps(self.cmd(out)["argv"]))
        self.assertIn("over max_cost_usd", self.result(out)["note"])
        rc, out = self.run_task("--agent", "codex:m", "--max-cost", "1", "--task", "x")
        self.assertIn("not enforced by codex", self.cmd(out)["enforcement"]["cost"])


class TestDoctorAndModels(ProviderSandbox):
    def test_doctor_ask(self):
        cfg = json.loads((self.home / "config.json").read_text())
        cfg["agents"] = {"one": "codex:m1", "two": "api:keyless/m2"}
        cfg["roles"] = {"light": {"agent": ["one"], "access": "read"}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        rc, out = self.crew("doctor", "--ask")
        self.assertIn("[ok  ] ask codex:m1", out)
        self.assertIn("[ok  ] ask api:keyless/m2", out)
        self.assertNotIn("FAIL] ask", out)

    def test_model_changes(self):
        rc, out = self.crew("models", "--changes")
        self.assertIn("recorded", out)
        rc, out = self.crew("models", "--changes")
        self.assertIn("no model changes", out)
        self.server.models = ["fake/model-a", "fake/model-c"]
        rc, out = self.crew("models", "--changes")
        self.assertIn("provider:keyless: + fake/model-c", out)
        self.assertIn("provider:keyless: - fake/model-b", out)
