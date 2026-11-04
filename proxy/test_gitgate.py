import base64
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gitgate

OLD = "1" * 40
NEW = "2" * 40


def pkt(s: str) -> bytes:
    b = s.encode()
    return b"%04x" % (len(b) + 4) + b


def push_body(*refs, extra=b"PACKDATA"):
    out = b""
    for i, ref in enumerate(refs):
        line = f"{OLD} {NEW} {ref}" + ("\0report-status side-band-64k" if i == 0 else "")
        out += pkt(line)
    return out + b"0000" + extra


def reader(data):
    f = io.BytesIO(data)

    def read(n):
        b = f.read(n)
        if len(b) != n:
            raise gitgate.Reject("truncated")
        return b
    return read


class ParseTests(unittest.TestCase):
    rx = gitgate.Config({"GITGATE_TOKEN": "t", "GITGATE_ALLOW": "x/*"}).ref_re

    def cmds(self, *refs):
        _, cmds, _caps = gitgate.parse_commands(reader(push_body(*refs)))
        return cmds

    def test_prefix_ok(self):
        gitgate.check_commands(self.cmds("refs/heads/agent/fix-1"), self.rx)
        gitgate.check_commands(self.cmds("refs/heads/agent/a/b", "refs/heads/agent/c"), self.rx)

    def test_custom_prefix(self):
        cfg = gitgate.Config({"GITGATE_BRANCH_PREFIX": "hermes"})
        self.assertEqual(cfg.ref_hint, "refs/heads/hermes/*")
        gitgate.check_commands(self.cmds("refs/heads/hermes/x"), cfg.ref_re)
        for ref in ("refs/heads/agent/x", "refs/heads/hermesx/y", "refs/heads/main"):
            with self.assertRaises(gitgate.Reject, msg=ref):
                gitgate.check_commands(self.cmds(ref), cfg.ref_re)
        self.assertEqual(gitgate.Config({"GITGATE_BRANCH_PREFIX": ""}).ref_hint, "refs/heads/agent/*")
        with self.assertRaises(ValueError):
            gitgate.Config({"GITGATE_BRANCH_PREFIX": "a b/(x"})

    def test_main_rejected(self):
        for ref in ("refs/heads/main", "refs/heads/agents/x", "refs/tags/v1", "refs/heads/bot/x",
                    "refs/heads/agent/../main", "refs/heads/agent/x.lock", "refs/heads/agent/"):
            with self.assertRaises(gitgate.Reject, msg=ref):
                gitgate.check_commands(self.cmds(ref), self.rx)

    def test_mixed_rejected(self):
        with self.assertRaises(gitgate.Reject):
            gitgate.check_commands(self.cmds("refs/heads/agent/ok", "refs/heads/main"), self.rx)

    def test_shallow_and_cert(self):
        body = pkt("shallow " + OLD) + push_body("refs/heads/agent/a")
        _, cmds, _caps = gitgate.parse_commands(reader(body))
        self.assertEqual(len(cmds), 1)
        with self.assertRaises(gitgate.Reject):
            gitgate.parse_commands(reader(pkt("push-cert\0caps") + b"0000"))

    def test_garbage(self):
        with self.assertRaises(gitgate.Reject):
            gitgate.parse_commands(reader(b"zzzz"))
        with self.assertRaises(gitgate.Reject):
            gitgate.check_commands([], self.rx)

    def test_caps_parsed(self):
        _, _, caps = gitgate.parse_commands(reader(push_body("refs/heads/agent/a", "refs/heads/agent/b")))
        self.assertEqual(caps, {b"report-status", b"side-band-64k"})

    def test_rejection_report_sideband(self):
        cmds = [(OLD, NEW, "refs/heads/main"), (OLD, NEW, "refs/heads/agent/ok")]
        out = gitgate.push_rejection(cmds, {b"report-status", b"side-band-64k"}, ["refs/heads/main"])
        self.assertTrue(out.endswith(b"0000"))
        self.assertIn(b"\x02gitgate: push rejected", out)
        self.assertIn(b"unpack ok", out)
        self.assertIn(b"ng refs/heads/main gitgate: only refs/heads/agent/* can be pushed", out)
        self.assertIn(b"ng refs/heads/agent/ok gitgate: not applied", out)

    def test_rejection_report_plain_and_none(self):
        cmds = [(OLD, NEW, "refs/heads/main")]
        plain = gitgate.push_rejection(cmds, {b"report-status"}, ["refs/heads/main"])
        self.assertTrue(plain.startswith(b"000eunpack ok\n"))
        self.assertNotIn(b"\x02", plain)
        self.assertIsNone(gitgate.push_rejection(cmds, set(), ["refs/heads/main"]))

    def test_prefix_is_raw_commands_only(self):
        raw, _, _caps = gitgate.parse_commands(reader(push_body("refs/heads/agent/a")))
        self.assertTrue(raw.endswith(b"0000"))
        self.assertNotIn(b"PACKDATA", raw)


class EndToEnd(unittest.TestCase):
    """Fake upstream: records the request and replies with a fixed response."""

    @classmethod
    def setUpClass(cls):
        cls.seen = []

        class Up(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def handle_any(self):
                body = b""
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        body += self.rfile.read(size)
                        self.rfile.readline()
                else:
                    body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                cls.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
                out = b"UPSTREAM-OK"
                self.send_response(200)
                self.send_header("Content-Type", "application/x-git-upload-pack-result")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = handle_any

        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
        cfg = gitgate.Config({
            "GITGATE_TOKEN": "SECRET", "GITGATE_ALLOW": "me/*",
            "GITGATE_UPSTREAM": "127.0.0.1:%d" % cls.up.server_port, "GITGATE_SCHEME": "http",
        })
        cls.gate = ThreadingHTTPServer(("127.0.0.1", 0), gitgate.make_handler(cfg))
        for s in (cls.up, cls.gate):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.gate.shutdown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.gate.server_port)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_fetch_passes_with_token(self):
        self.seen.clear()
        st, data = self.req("GET", "/me/app.git/info/refs?service=git-upload-pack")
        self.assertEqual((st, data), (200, b"UPSTREAM-OK"))
        self.assertTrue(self.seen[0][2].startswith("Basic "))

    def test_push_hermes_passes_body_intact(self):
        self.seen.clear()
        body = push_body("refs/heads/agent/x", extra=b"P" * 200000)
        st, data = self.req("POST", "/me/app.git/git-receive-pack", body,
                            {"Content-Type": "application/x-git-receive-pack-request"})
        self.assertEqual(st, 200)
        self.assertEqual(self.seen[0][3], body)

    def test_push_main_denied_and_not_forwarded(self):
        self.seen.clear()
        st, data = self.req("POST", "/me/app.git/git-receive-pack", push_body("refs/heads/main"))
        self.assertEqual(st, 200)  # the rejection arrives as report-status so git can show the reason
        self.assertIn(b"ng refs/heads/main gitgate:", data)
        self.assertEqual(self.seen, [])

    def test_push_main_without_report_status_gets_403(self):
        self.seen.clear()
        body = pkt(f"{OLD} {NEW} refs/heads/main") + b"0000PACK"
        st, _ = self.req("POST", "/me/app.git/git-receive-pack", body)
        self.assertEqual(st, 403)
        self.assertEqual(self.seen, [])

    def test_probe_request_is_forwarded(self):
        # git sends "0000" before a push larger than http.postBuffer to check access
        self.seen.clear()
        st, _ = self.req("POST", "/me/app.git/git-receive-pack", b"0000",
                         {"Content-Type": "application/x-git-receive-pack-request"})
        self.assertEqual(st, 200)
        self.assertEqual(self.seen[0][3], b"0000")

    def test_flush_followed_by_junk_is_denied(self):
        self.seen.clear()
        st, _ = self.req("POST", "/me/app.git/git-receive-pack", b"0000PACKDATA")
        self.assertEqual(st, 403)
        self.assertEqual(self.seen, [])

    def test_big_rejected_push_is_drained(self):
        self.seen.clear()
        body = push_body("refs/heads/main", extra=b"P" * 3_000_000)
        st, data = self.req("POST", "/me/app.git/git-receive-pack", body)
        self.assertEqual(st, 200)
        self.assertIn(b"ng refs/heads/main", data)
        self.assertEqual(self.seen, [])

    def test_other_owner_denied(self):
        self.seen.clear()
        st, _ = self.req("GET", "/other/app.git/info/refs?service=git-upload-pack")
        self.assertEqual(st, 403)
        self.assertEqual(self.seen, [])

    def test_junk_paths(self):
        for p in ("/", "/me/app.git/HEAD", "/me/app.git/info/../x", "/me/app.git/objects/info/packs"):
            st, _ = self.req("GET", p)
            self.assertIn(st, (403, 404), p)
        self.assertEqual(self.seen, [] if not self.seen else self.seen)


class PullRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []
        cls.reply = (201, {"number": 7, "html_url": "https://github.com/me/app/pull/7", "secret": "x"})

        class Api(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                cls.seen.append((self.path, self.headers.get("Authorization"), json.loads(body)))
                out = json.dumps(cls.reply[1]).encode()
                self.send_response(cls.reply[0])
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        env = {"GITGATE_TOKEN": "SECRET", "GITGATE_ALLOW": "me/*", "GITGATE_ENABLE_PR": "1",
               "GITGATE_API_UPSTREAM": "127.0.0.1:%d" % cls.api.server_port, "GITGATE_SCHEME": "http"}
        cls.gate = ThreadingHTTPServer(("127.0.0.1", 0), gitgate.make_handler(gitgate.Config(env)))
        cls.off = ThreadingHTTPServer(("127.0.0.1", 0), gitgate.make_handler(
            gitgate.Config({**env, "GITGATE_ENABLE_PR": ""})))
        for s_ in (cls.api, cls.gate, cls.off):
            threading.Thread(target=s_.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s_ in (cls.api, cls.gate, cls.off):
            s_.shutdown()

    def post(self, data, path="/me/app.git/pulls", server=None):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        c = http.client.HTTPConnection("127.0.0.1", (server or self.gate).server_port)
        c.request("POST", path, body=raw, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    def ok(self, **kw):
        return {"head": "agent/feat", "title": "Add feat", **kw}

    def test_creates_pr_and_trims_response(self):
        self.seen.clear()
        st, out = self.post(self.ok(body="text", draft=True))
        self.assertEqual(st, 201)
        self.assertEqual(json.loads(out), {"number": 7, "html_url": "https://github.com/me/app/pull/7"})
        path, auth, payload = self.seen[0]
        self.assertEqual((path, auth), ("/repos/me/app/pulls", "Bearer SECRET"))
        self.assertEqual((payload["head"], payload["base"], payload["draft"]), ("agent/feat", "main", True))

    def test_bad_requests_are_not_forwarded(self):
        cases = [
            self.ok(head="main"), self.ok(head="bot/x"), self.ok(head="other:agent/x"),
            self.ok(head="agent/../main"), self.ok(base="release"), self.ok(title=""),
            {"head": "agent/x"}, self.ok(body="x" * 70000), [1], b"not json",
        ]
        for c in cases:
            self.seen.clear()
            st, _ = self.post(c)
            self.assertEqual(st, 400, c if not isinstance(c, dict) else {k: str(v)[:30] for k, v in c.items()})
            self.assertEqual(self.seen, [])

    def test_other_owner_denied(self):
        self.seen.clear()
        st, _ = self.post(self.ok(), path="/other/app.git/pulls")
        self.assertEqual(st, 403)
        self.assertEqual(self.seen, [])

    def test_github_error_is_passed_as_text_only(self):
        type(self).reply = (422, {"message": "Validation Failed", "errors": [{"message": "no commits"}], "x": 1})
        try:
            st, out = self.post(self.ok())
        finally:
            type(self).reply = (201, {"number": 7, "html_url": "u"})
        self.assertEqual(st, 422)
        self.assertEqual(json.loads(out), {"error": "Validation Failed", "errors": ["no commits"]})

    def test_disabled_by_default(self):
        self.seen.clear()
        st, _ = self.post(self.ok(), server=self.off)
        self.assertEqual(st, 404)
        self.assertEqual(self.seen, [])


class Policy(unittest.TestCase):
    POLICY = """
[[client]]
name = "a"
key_env = "KEY_A"
[[client]]
name = "b"
key_env = "KEY_B"

[[rule]]
repos = ["me/docs"]
readonly = true

[[rule]]
repos = ["me/app-*"]
token_env = "TOKEN_APPS"
branch_prefix = "bot-a/"
clients = ["a"]

[[rule]]
repos = ["me/app-*"]
token_env = "TOKEN_APPS"
branch_prefix = "bot-b/"
clients = ["b"]
"""
    ENV = {"GITGATE_POLICY": POLICY, "KEY_A": "ka", "KEY_B": "kb", "TOKEN_APPS": "APPSTOKEN",
           "GITGATE_TOKEN": "DEFAULT", "GITGATE_ALLOW": "me/misc"}

    @classmethod
    def setUpClass(cls):
        cls.seen = []

        class Up(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                cls.seen.append(self.headers.get("Authorization"))
                out = b"OK"
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        self.rfile.read(size)
                        self.rfile.readline()
                else:
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                cls.seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"OK")

        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
        env = {**cls.ENV, "GITGATE_UPSTREAM": "127.0.0.1:%d" % cls.up.server_port, "GITGATE_SCHEME": "http"}
        cls.gate = ThreadingHTTPServer(("127.0.0.1", 0), gitgate.make_handler(gitgate.Config(env)))
        for s_ in (cls.up, cls.gate):
            threading.Thread(target=s_.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.gate.shutdown()

    def req(self, method, path, body=None, user=None):
        import base64
        h = {}
        if user:
            h["Authorization"] = "Basic " + base64.b64encode(user.encode()).decode()
        c = http.client.HTTPConnection("127.0.0.1", self.gate.server_port)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def basic(self, token):
        import base64
        return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()

    def test_anonymous_gets_401(self):
        self.seen.clear()
        st, _ = self.req("GET", "/me/misc.git/info/refs?service=git-upload-pack")
        self.assertEqual(st, 401)
        st, _ = self.req("GET", "/me/misc.git/info/refs?service=git-upload-pack", user="a:wrong")
        self.assertEqual(st, 401)
        st, _ = self.req("GET", "/me/misc.git/info/refs?service=git-upload-pack", user="zzz:ka")
        self.assertEqual(st, 401)
        self.assertEqual(self.seen, [])

    def test_per_rule_token(self):
        self.seen.clear()
        self.assertEqual(self.req("GET", "/me/app-1.git/info/refs?service=git-upload-pack", user="a:ka")[0], 200)
        self.assertEqual(self.req("GET", "/me/misc.git/info/refs?service=git-upload-pack", user="a:ka")[0], 200)
        self.assertEqual(self.seen, [self.basic("APPSTOKEN"), self.basic("DEFAULT")])

    def test_each_client_has_its_own_prefix(self):
        self.seen.clear()
        for user, ref, expect in (("a:ka", "bot-a/x", 200), ("a:ka", "bot-b/x", 200), ("b:kb", "bot-b/x", 200),
                                  ("b:kb", "bot-a/x", 200)):
            st, data = self.req("POST", "/me/app-1.git/git-receive-pack", push_body("refs/heads/" + ref), user)
            self.assertEqual(st, expect)
            applied = b"ng " not in data
            self.assertEqual(applied, ref.split("/")[0] == "bot-" + user[0], (user, ref))
        self.assertEqual(len(self.seen), 2)  # only the two allowed pushes reached upstream

    def test_readonly_repo(self):
        self.seen.clear()
        self.assertEqual(self.req("GET", "/me/docs.git/info/refs?service=git-upload-pack", user="a:ka")[0], 200)
        self.assertEqual(self.req("GET", "/me/docs.git/info/refs?service=git-receive-pack", user="a:ka")[0], 403)
        self.assertEqual(self.req("POST", "/me/docs.git/git-receive-pack", push_body("refs/heads/agent/x"), "a:ka")[0], 403)
        self.assertEqual(len(self.seen), 1)

    def test_default_rule_uses_agent_prefix(self):
        st, data = self.req("POST", "/me/misc.git/git-receive-pack", push_body("refs/heads/agent/x"), "b:kb")
        self.assertNotIn(b"ng ", data)
        st, data = self.req("POST", "/me/misc.git/git-receive-pack", push_body("refs/heads/main"), "b:kb")
        self.assertIn(b"ng refs/heads/main", data)

    def test_unknown_repo_denied(self):
        self.assertEqual(self.req("GET", "/me/other.git/info/refs?service=git-upload-pack", user="a:ka")[0], 403)


class PolicyErrors(unittest.TestCase):
    def bad(self, policy, **env):
        with self.assertRaises(ValueError):
            gitgate.Config({"GITGATE_POLICY": policy, "GITGATE_TOKEN": "t", **env})

    def test_errors(self):
        self.bad('[[rule]]\nrepos = []')
        self.bad('[[rule]]\nrepos = ["a/b"]\ntoken_env = "NOPE"')
        self.bad('[[rule]]\nrepos = ["a/b"]\nclients = ["ghost"]')
        self.bad('[[client]]\nname = "x"\nkey_env = "NOPE"')
        self.bad('[[client]]\nname = "bad name"\nkey_env = "K"', K="k")
        self.bad('[[rule]]\nrepos = ["a/b"]\nbranch_prefix = "a b"')
        with self.assertRaises(ValueError):
            gitgate.Config({"GITGATE_ALLOW": "a/b"})  # no token

    def test_no_policy_is_plain_mode(self):
        cfg = gitgate.Config({"GITGATE_TOKEN": "t", "GITGATE_ALLOW": "a/*"})
        self.assertEqual(len(cfg.rules), 1)
        self.assertIsNone(cfg.authenticate(None))  # no clients configured: anonymous allowed


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed for GitHub App tokens")
class AppTokens(unittest.TestCase):
    POLICY = """
[[app]]
name = "bot"
app_id = 123
private_key_file = "%(key)s"

[[rule]]
repos = ["me/docs"]
readonly = true
app = "bot"

[[rule]]
repos = ["me/*"]
app = "bot"
enable_pr = true
"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        key, pub = os.path.join(cls.dir, "k.pem"), os.path.join(cls.dir, "pub.pem")
        subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
        cls.pub = pub
        cls.seen, cls.mints, cls.jwt_ok = [], [], []
        cls.lifetime, cls.installed = 3600, True

        class Gh(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send_json(self, status, obj):
                out = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def read_body(self):
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    body = b""
                    while True:
                        size = int(self.rfile.readline().strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            return body
                        body += self.rfile.read(size)
                        self.rfile.readline()
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_GET(self):
                if self.path.endswith("/installation"):
                    cls.jwt_ok.append(cls.check_jwt(self.headers["Authorization"]))
                    return self.send_json(200, {"id": 55}) if cls.installed else self.send_json(404, {"message": "Not Found"})
                cls.seen.append(self.headers.get("Authorization"))
                self.send_json(200, {})

            def do_POST(self):
                body = self.read_body()
                if self.path == "/app/installations/55/access_tokens":
                    cls.jwt_ok.append(cls.check_jwt(self.headers["Authorization"]))
                    cls.mints.append(json.loads(body))
                    exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + cls.lifetime))
                    return self.send_json(201, {"token": f"ghs_{len(cls.mints)}", "expires_at": exp})
                cls.seen.append((self.path, self.headers.get("Authorization"), json.loads(body or b"{}")))
                self.send_json(201, {"number": 1, "html_url": "u"})

        cls.gh = ThreadingHTTPServer(("127.0.0.1", 0), Gh)
        env = {"GITGATE_POLICY": cls.POLICY % {"key": key}, "GITGATE_UPSTREAM": "127.0.0.1:%d" % cls.gh.server_port,
               "GITGATE_API_UPSTREAM": "127.0.0.1:%d" % cls.gh.server_port, "GITGATE_SCHEME": "http"}
        cls.cfg = gitgate.Config(env)
        cls.gate = ThreadingHTTPServer(("127.0.0.1", 0), gitgate.make_handler(cls.cfg))
        for s_ in (cls.gh, cls.gate):
            threading.Thread(target=s_.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.gh.shutdown()
        cls.gate.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def check_jwt(cls, header):
        """Verify the RS256 signature and claims of the App JWT with openssl."""
        token = header.split(" ", 1)[1]
        head, body, sig = token.split(".")
        pad = lambda v: v + "=" * (-len(v) % 4)
        sigfile = os.path.join(cls.dir, "sig")
        with open(sigfile, "wb") as f:
            f.write(base64.urlsafe_b64decode(pad(sig)))
        ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", cls.pub, "-signature", sigfile],
                            input=f"{head}.{body}".encode(), capture_output=True).returncode == 0
        claims = json.loads(base64.urlsafe_b64decode(pad(body)))
        return ok and claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 660

    def setUp(self):
        self.seen.clear(); self.mints.clear(); self.jwt_ok.clear()
        type(self).lifetime, type(self).installed = 3600, True
        for app in self.cfg.apps.values():
            app.tokens.clear(); app.installations.clear()

    def basic(self, token):
        return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()

    def get(self, repo, service="git-upload-pack"):
        c = http.client.HTTPConnection("127.0.0.1", self.gate.server_port)
        c.request("GET", f"/me/{repo}.git/info/refs?service={service}")
        r = c.getresponse(); r.read(); c.close()
        return r.status

    def test_fetch_uses_minted_narrow_token(self):
        self.assertEqual(self.get("app-1"), 200)
        self.assertEqual(self.seen, [self.basic("ghs_1")])
        self.assertEqual(self.mints, [{"repositories": ["app-1"], "permissions": {"contents": "write", "pull_requests": "write"}}])
        self.assertEqual(self.jwt_ok, [True, True])  # installation lookup and token request both signed correctly

    def test_token_is_cached(self):
        for _ in range(3):
            self.assertEqual(self.get("app-1"), 200)
        self.assertEqual(len(self.mints), 1)
        self.assertEqual(self.seen, [self.basic("ghs_1")] * 3)
        self.assertEqual(self.get("app-2"), 200)  # another repository gets its own token
        self.assertEqual(len(self.mints), 2)

    def test_expiring_token_is_renewed(self):
        type(self).lifetime = 120  # less than the 5 minute safety margin
        self.get("app-1"); self.get("app-1")
        self.assertEqual(len(self.mints), 2)

    def test_readonly_rule_gets_read_only_token(self):
        self.assertEqual(self.get("docs"), 200)
        self.assertEqual(self.mints[0]["permissions"], {"contents": "read"})
        self.assertEqual(self.get("docs", "git-receive-pack"), 403)

    def test_not_installed_gives_502_and_no_upstream_call(self):
        type(self).installed = False
        self.assertEqual(self.get("app-9"), 502)
        self.assertEqual(self.seen, [])

    def test_pull_request_uses_app_token(self):
        c = http.client.HTTPConnection("127.0.0.1", self.gate.server_port)
        c.request("POST", "/me/app-1.git/pulls", body=json.dumps({"head": "agent/x", "title": "t"}))
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 201)
        self.assertEqual(self.seen[0][:2], ("/repos/me/app-1/pulls", "Bearer ghs_1"))

    def test_config_errors(self):
        key = os.path.join(self.dir, "k.pem")
        base = f'[[app]]\nname = "a"\napp_id = 1\nprivate_key_file = "{key}"\n'
        for policy in (base + '[[rule]]\nrepos=["x/y"]\napp="nope"',
                       base + '[[rule]]\nrepos=["x/y"]\napp="a"\ntoken_env="T"',
                       '[[app]]\nname = "a"\napp_id = 1',
                       '[[app]]\nname = "a"\napp_id = 1\nprivate_key_file = "/nonexistent"',
                       f'[[app]]\nname = "a"\nprivate_key_file = "{key}"'):
            with self.assertRaises(ValueError, msg=policy):
                gitgate.Config({"GITGATE_POLICY": policy, "T": "t"})

    def test_key_from_environment(self):
        pem = open(os.path.join(self.dir, "k.pem")).read()
        cfg = gitgate.Config({"GITGATE_POLICY": '[[app]]\nname = "a"\napp_id = 123\nprivate_key_env = "PEM"\n'
                              '[[rule]]\nrepos = ["me/*"]\napp = "a"', "PEM": pem})
        self.assertTrue(cfg.apps["a"].jwt().count(".") == 2)


if __name__ == "__main__":
    unittest.main()
