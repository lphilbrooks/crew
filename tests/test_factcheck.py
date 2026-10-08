"""crew fact-check tests. Run from the repo root:  python -m unittest tests.test_factcheck -v

No network: a local HTTP server stands in for websites and package registries, and
CREW_CHECK_ALLOW_LOCAL=1 lets the checker reach 127.0.0.1.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREW = ROOT / "skills" / "crew" / "scripts" / "crew.py"
sys.path.insert(0, str(CREW.parent))
import crew  # noqa: E402

CRATES = {
    "bsa": {"crate": {"max_stable_version": "0.2.1", "max_version": "0.2.1"},
            "versions": [{"num": "0.2.1", "yanked": False}, {"num": "0.1.1", "yanked": False}]},
    "zlib-rs": {"crate": {"max_stable_version": "0.6.8", "max_version": "0.6.8"},
                "versions": [{"num": "0.6.8", "yanked": False}, {"num": "0.6.7", "yanked": False}]},
    "yankedcrate": {"crate": {"max_stable_version": "0.9.0", "max_version": "1.0.0"},
                    "versions": [{"num": "1.0.0", "yanked": True}, {"num": "0.9.0", "yanked": False}]},
}
PYPI = {
    "requests": {"info": {"version": "2.32.0"},
                 "releases": {"2.31.0": [{"yanked": False}], "2.32.0": [{"yanked": False}],
                              "1.0.0": [{"yanked": True}]}},
}
NPM = {"left-pad": {"dist-tags": {"latest": "1.3.0"}, "versions": {"1.3.0": {}, "1.2.0": {}}}}
ENV_KEYS = ("CREW_CHECK_ALLOW_LOCAL", "CREW_REGISTRY_CRATES", "CREW_REGISTRY_PYPI", "CREW_REGISTRY_NPM")


class _Routes(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._serve(send_body=False)

    def do_GET(self):
        self._serve(send_body=True)

    def _send(self, code, payload, send_body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if send_body:
            self.wfile.write(payload)

    def _serve(self, send_body):
        path = self.path
        if path == "/ok":
            return self._send(200, b"ok", send_body)
        if path == "/gone":
            return self._send(404, b"gone", send_body)
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/nohead":  # refuses HEAD like some real servers do
            if send_body:
                return self._send(200, b"ok", True)
            return self._send(405, b"", False)
        parts = path.strip("/").split("/")
        data = None
        if parts[0] == "crates" and len(parts) == 2:
            data = CRATES.get(parts[1])
        elif parts[0] == "pypi" and len(parts) == 3:
            data = PYPI.get(parts[1])
        elif parts[0] == "npm" and len(parts) == 2:
            data = NPM.get(parts[1])
        if data is not None:
            return self._send(200, json.dumps(data).encode(), send_body, "application/json")
        return self._send(404, b"not found", send_body)


class FactCheckBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Routes)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ.update({
            "CREW_CHECK_ALLOW_LOCAL": "1",
            "CREW_REGISTRY_CRATES": self.base + "/crates/{name}",
            "CREW_REGISTRY_PYPI": self.base + "/pypi/{name}/json",
            "CREW_REGISTRY_NPM": self.base + "/npm/{name}",
        })

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestUrls(FactCheckBase):
    def test_extract_urls(self):
        text = ("See [docs](https://example.com/docs). Also https://example.org/a, and "
                "https://example.org/a again; ignore ftp://files.example.net/x and mailto:me@x.org. "
                "Wiki https://en.wikipedia.org/wiki/Foo_(bar) and <https://example.net/c>.")
        self.assertEqual(crew.extract_urls(text), [
            "https://example.com/docs", "https://example.org/a",
            "https://en.wikipedia.org/wiki/Foo_(bar)", "https://example.net/c"])

    def test_url_allowed(self):
        os.environ.pop("CREW_CHECK_ALLOW_LOCAL")
        for url in ("http://127.0.0.1:8000/x", "http://localhost:8000/", "http://foo.local/",
                    "http://x.internal/", "http://10.1.2.3/", "http://192.168.0.5/",
                    "http://169.254.10.10/", "http://[::1]/"):
            self.assertFalse(crew.url_allowed(url), url)
        self.assertTrue(crew.url_allowed("https://crates.io/"))
        self.assertTrue(crew.url_allowed("http://8.8.8.8/"))
        os.environ["CREW_CHECK_ALLOW_LOCAL"] = "1"
        self.assertTrue(crew.url_allowed("http://127.0.0.1:8000/x"))

    def test_check_url_statuses(self):
        ok = crew.check_url(self.base + "/ok")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["status"], 200)
        self.assertTrue(ok["final_url"].endswith("/ok"))
        gone = crew.check_url(self.base + "/gone")
        self.assertFalse(gone["ok"])
        self.assertEqual(gone["status"], 404)
        redirect = crew.check_url(self.base + "/redirect")
        self.assertTrue(redirect["ok"])
        self.assertEqual(redirect["status"], 200)
        self.assertTrue(redirect["final_url"].endswith("/ok"))
        nohead = crew.check_url(self.base + "/nohead")
        self.assertTrue(nohead["ok"])
        self.assertEqual(nohead["status"], 200)

    def test_check_url_unreachable_does_not_raise(self):
        result = crew.check_url("http://127.0.0.1:1/", timeout=2)
        self.assertIsNone(result["status"])
        self.assertFalse(result["ok"])
        self.assertIsNotNone(result["error"])

    def test_check_urls_order_and_skip(self):
        results = crew.check_urls([self.base + "/ok", self.base + "/gone", self.base + "/redirect"])
        self.assertEqual([r["status"] for r in results], [200, 404, 200])
        os.environ.pop("CREW_CHECK_ALLOW_LOCAL")
        skipped = crew.check_urls(["http://127.0.0.1:9/a", "https://10.0.0.1/b"])
        for r in skipped:
            self.assertIsNone(r["ok"])
            self.assertEqual(r["error"], "skipped: private or local address")


class TestVersionClaims(FactCheckBase):
    TEXT = """
| Crate | Version | Released |
|---|---|---|
| **memmap2** | `0.9.11` | June 22 |
| zlib-rs | 0.6.7 | 2026-08-03 |
| Step | 2.1.0 | later |
| calendar | 2024.10 | never |

Use bsa@0.1.2 and bsa@0.1.2 again. Also `serde` 1.0.200, tokio v1.40.0 and regex 1.10.4.
The Python 3.11 release and version 3.12.1 and Figure 2.1 are not packages.
pandas 2024.10 is a date-like version.
"""

    def test_extract_version_claims(self):
        self.assertEqual(crew.extract_version_claims(self.TEXT), [
            ("memmap2", "0.9.11"), ("zlib-rs", "0.6.7"), ("bsa", "0.1.2"),
            ("serde", "1.0.200"), ("tokio", "1.40.0"), ("regex", "1.10.4")])

    def test_detect_ecosystems(self):
        self.assertEqual(crew.detect_ecosystems("Use crates.io and Cargo.toml"), ["crates"])
        self.assertEqual(crew.detect_ecosystems("A Rust crate version"), ["crates"])
        self.assertEqual(crew.detect_ecosystems("Install with pip install; PyPI and npm"), ["pypi", "npm"])
        self.assertEqual(crew.detect_ecosystems("nothing relevant here"), [])


class TestPackages(FactCheckBase):
    def test_check_package_crates(self):
        r = crew.check_package("crates", "bsa", "0.1.1")
        self.assertEqual((r["found"], r["exists"], r["latest"], r["error"]), (True, True, "0.2.1", None))
        r = crew.check_package("crates", "bsa", "0.1.2")
        self.assertEqual((r["found"], r["exists"], r["latest"]), (True, False, "0.2.1"))
        r = crew.check_package("crates", "yankedcrate", "1.0.0")
        self.assertEqual((r["found"], r["exists"], r["latest"]), (True, False, "0.9.0"))
        r = crew.check_package("crates", "nosuch", "0.1.0")
        self.assertEqual((r["found"], r["exists"]), (False, None))

    def test_check_package_pypi(self):
        r = crew.check_package("pypi", "requests", "2.31.0")
        self.assertEqual((r["found"], r["exists"], r["latest"]), (True, True, "2.32.0"))
        r = crew.check_package("pypi", "requests", "1.0.0")
        self.assertEqual((r["found"], r["exists"]), (True, False))
        self.assertEqual(crew.check_package("pypi", "nosuch", "1.0")["found"], False)

    def test_check_package_npm(self):
        r = crew.check_package("npm", "left-pad", "1.3.0")
        self.assertEqual((r["found"], r["exists"], r["latest"]), (True, True, "1.3.0"))
        self.assertEqual(crew.check_package("npm", "nosuch", "1.0.0")["found"], False)

    def test_check_package_network_error(self):
        os.environ["CREW_REGISTRY_NPM"] = "http://127.0.0.1:1/npm/{name}"
        r = crew.check_package("npm", "left-pad", "1.3.0", timeout=2)
        self.assertIsNone(r["found"])
        self.assertIsNotNone(r["error"])

    def test_check_packages_keeps_found_only(self):
        kept = crew.check_packages("bsa 0.1.2 and zlib-rs 0.6.7", task_text="We use Cargo")
        self.assertEqual([(r["name"], r["exists"]) for r in kept], [("bsa", False), ("zlib-rs", True)])
        kept = crew.check_packages("requests 2.31.0 and left-pad 1.3.0 and bsa 0.1.2",
                                   task_text="pip install and npm")
        self.assertEqual([(r["name"], r["ecosystem"]) for r in kept], [("requests", "pypi"), ("left-pad", "npm")])


class TestFactCheck(FactCheckBase):
    def test_fact_check_end_to_end(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            task = Path(tmp)
            (task / "final.md").write_text(
                "Summary.\n\n"
                f"See [the guide]({self.base}/ok) and the old page {self.base}/gone.\n"
                f"Also {self.base}/redirect and {self.base}/nohead.\n\n"
                "| Crate | Version | Released |\n|---|---|---|\n"
                "| bsa | `0.1.2` | June 22 |\n"
                "| zlib-rs | 0.6.7 | 2026-08-03 |\n", encoding="utf-8")
            (task / "task.md").write_text("Rust crates on crates.io", encoding="utf-8")
            (task / "cmd.json").write_text(json.dumps(
                {"role": "research", "scope": {"access": "read", "web": "on", "network": "on"}}), encoding="utf-8")
            summary = crew.fact_check(task)
            self.assertNotIn("error", summary)
            self.assertEqual(summary["urls_checked"], 4)
            self.assertEqual([(f["url"], f["status"]) for f in summary["urls_failed"]],
                             [(self.base + "/gone", 404)])
            packages = {p["name"]: p for p in summary["packages"]}
            self.assertEqual(set(packages), {"bsa", "zlib-rs"})
            self.assertFalse(packages["bsa"]["exists"])
            self.assertEqual(packages["bsa"]["latest"], "0.2.1")
            self.assertTrue(packages["zlib-rs"]["exists"])
            self.assertEqual(packages["zlib-rs"]["latest"], "0.6.8")
            self.assertIn("checked_at", summary)
            written = json.loads((task / "checks.json").read_text(encoding="utf-8"))
            self.assertEqual(len(written["urls"]), 4)
            self.assertEqual(written["role"], "research")

    def test_fact_check_missing_folder_returns_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            summary = crew.fact_check(Path(tmp) / "missing")
            self.assertIn("error", summary)

    def test_summarize_checks(self):
        summary = {"urls_checked": 5,
                   "urls_failed": [{"url": "https://x.example/a", "status": 404, "error": None}],
                   "packages": [{"name": "bsa", "claimed": "0.1.2", "exists": False, "latest": "0.2.1"},
                                {"name": "zlib-rs", "claimed": "0.6.7", "exists": True, "latest": "0.6.8"}]}
        self.assertEqual(crew.summarize_checks(summary), [
            "URL failed (404): https://x.example/a",
            "bsa: claimed 0.1.2 is not a published version (latest 0.2.1)",
            "zlib-rs: claimed 0.6.7 exists, but latest is 0.6.8"])
        self.assertEqual(crew.summarize_checks({"urls_checked": 12, "urls_failed": [], "packages": []}),
                         ["fact check: 12 URLs ok, no version problems"])




class TestReviewFindings(unittest.TestCase):
    """Regression tests for problems found in review of the fact checker."""

    def setUp(self):
        self.saved_env = os.environ.pop("CREW_CHECK_ALLOW_LOCAL", None)

    def tearDown(self):
        if self.saved_env is not None:
            os.environ["CREW_CHECK_ALLOW_LOCAL"] = self.saved_env

    def test_other_spellings_of_local_addresses_are_refused(self):
        for host in ("2130706433", "0x7f000001", "127.1", "0177.0.0.1", "0", "[::ffff:127.0.0.1]",
                     "169.254.169.254", "192.168.1.93", "printer.localhost"):
            self.assertFalse(crew.url_allowed(f"http://{host}/x"), host)

    def test_names_resolving_to_private_addresses_are_refused(self):
        real = crew.socket.getaddrinfo
        crew.socket.getaddrinfo = lambda host, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))]
        try:
            self.assertFalse(crew.url_allowed("http://looks-public.example/"))
        finally:
            crew.socket.getaddrinfo = real

    def test_redirect_to_private_address_is_not_followed(self):
        import http.server
        import threading
        hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                hits.append(self.path)
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/secret")
                else:
                    self.send_response(200)
                self.end_headers()
            do_GET = do_HEAD

            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        real = crew.url_allowed
        crew.url_allowed = lambda url: url.endswith("/start")  # pretend the first hop is a public host
        try:
            result = crew.check_url(f"http://127.0.0.1:{server.server_port}/start", timeout=5)
        finally:
            crew.url_allowed = real
            server.shutdown()
        self.assertNotIn("/secret", hits)
        self.assertFalse(result["ok"])

    def test_blocked_sites_and_passing_mentions_are_not_failures(self):
        saved = crew.check_urls, crew.check_packages
        crew.check_urls = lambda urls: [
            {"url": "https://a.example/", "status": 200, "final_url": None, "ok": True, "error": None},
            {"url": "https://b.example/", "status": 403, "final_url": None, "ok": None,
             "error": "blocked by the site (HTTP 403)"}]

        def pkg(claimed, exists=True):
            return {"ecosystem": "crates", "name": "clap", "claimed": claimed, "found": True,
                    "exists": exists, "latest": "4.6.7", "error": None}
        # the answer gives the latest version and mentions an older one in passing
        crew.check_packages = lambda text, task_text="": [pkg("4.6.7"), pkg("4.5.31")]
        try:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "final.md").write_text("x", encoding="utf-8")
                summary = crew.fact_check(Path(d))
        finally:
            crew.check_urls, crew.check_packages = saved
        self.assertEqual(summary["urls_failed"], [])
        self.assertEqual(summary["urls_blocked"], 1)
        self.assertEqual(summary["packages"], [])
        self.assertIn("could not be checked", " ".join(crew.summarize_checks(summary)))

    def test_versions_compare_numerically(self):
        self.assertEqual(crew.version_key("2.0"), crew.version_key("2.0.0"))
        self.assertEqual(crew.version_key("v1.2.3+build5"), crew.version_key("1.2.3"))
        self.assertNotEqual(crew.version_key("1.2.3-rc1"), crew.version_key("1.2.3"))


if __name__ == "__main__":
    unittest.main()
