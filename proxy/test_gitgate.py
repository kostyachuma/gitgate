import base64
import http.client
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gitgate

OLD = "1" * 40
NEW = "2" * 40
HINT = "refs/heads/agent/*"


# ---- helpers -------------------------------------------------------------------

def pkt(text):
    return gitgate.pkt(text.encode())


def push_body(*refs, extra=b"PACKDATA"):
    lines = (f"{OLD} {NEW} {ref}" + ("\0report-status side-band-64k" if i == 0 else "") for i, ref in enumerate(refs))
    return b"".join(pkt(line) for line in lines) + b"0000" + extra


def reader(data):
    f = io.BytesIO(data)

    def read(n):
        chunk = f.read(n)
        if len(chunk) != n:
            raise gitgate.Reject("truncated")
        return chunk
    return read


def basic(user_and_key):
    return "Basic " + base64.b64encode(user_and_key.encode()).decode()


def start(handler):
    """An HTTP server on a free local port, serving in a background thread."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop(*servers):
    for server in servers:
        server.shutdown()
        server.server_close()


def address(server):
    return f"127.0.0.1:{server.server_port}"


def start_gate(env):
    return start(gitgate.make_handler(gitgate.Config(env)))


def request(server, method, path, body=None, headers=None, user=None):
    headers = dict(headers or {})
    if user:
        headers["Authorization"] = basic(user)
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class FakeHandler(BaseHTTPRequestHandler):
    """Base for fake GitHub servers."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() != "chunked":
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b""
        while size := int(self.rfile.readline().strip(), 16):
            body += self.rfile.read(size)
            self.rfile.readline()
        self.rfile.readline()
        return body

    def send(self, status, data=b"", content_type="application/octet-stream"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, obj):
        self.send(status, json.dumps(obj).encode(), "application/json")


def recording_upstream(seen):
    """A fake GitHub that records (method, path, Authorization, body) and answers UPSTREAM-OK."""
    class Upstream(FakeHandler):
        def handle_any(self):
            seen.append((self.command, self.path, self.headers.get("Authorization"), self.read_body()))
            self.send(200, b"UPSTREAM-OK", "application/x-git-upload-pack-result")
        do_GET = do_POST = handle_any
    return start(Upstream)


# ---- push parsing and policy functions -------------------------------------------

class PushParsing(unittest.TestCase):
    def cmds(self, *refs):
        return gitgate.parse_commands(reader(push_body(*refs)))[1]

    def bad(self, *refs, prefix="agent/"):
        return gitgate.bad_refs(self.cmds(*refs), prefix)

    def test_allowed_branches(self):
        self.assertEqual(self.bad("refs/heads/agent/fix-1"), [])
        self.assertEqual(self.bad("refs/heads/agent/a/b", "refs/heads/agent/c"), [])

    def test_rejected_refs(self):
        for ref in ("refs/heads/main", "refs/heads/agents/x", "refs/tags/v1", "refs/heads/bot/x",
                    "refs/heads/agent/../main", "refs/heads/agent/x.lock", "refs/heads/agent/", "refs/heads/agent//x"):
            self.assertEqual(self.bad(ref), [ref], ref)

    def test_mixed_push_names_only_the_bad_ref(self):
        self.assertEqual(self.bad("refs/heads/agent/ok", "refs/heads/main"), ["refs/heads/main"])

    def test_custom_prefix(self):
        cfg = gitgate.Config({"GITGATE_BRANCH_PREFIX": "hermes"})
        self.assertEqual(cfg.branch_prefix, "hermes/")
        self.assertEqual(self.bad("refs/heads/hermes/x", prefix=cfg.branch_prefix), [])
        for ref in ("refs/heads/agent/x", "refs/heads/hermesx/y", "refs/heads/main"):
            self.assertEqual(self.bad(ref, prefix=cfg.branch_prefix), [ref], ref)
        self.assertEqual(gitgate.Config({"GITGATE_BRANCH_PREFIX": ""}).branch_prefix, "agent/")
        with self.assertRaises(ValueError):
            gitgate.Config({"GITGATE_BRANCH_PREFIX": "a b/(x"})

    def test_shallow_lines_skipped_push_cert_rejected(self):
        body = pkt("shallow " + OLD) + push_body("refs/heads/agent/a")
        self.assertEqual(len(gitgate.parse_commands(reader(body))[1]), 1)
        with self.assertRaises(gitgate.Reject):
            gitgate.parse_commands(reader(pkt("push-cert\0caps") + b"0000"))

    def test_garbage(self):
        with self.assertRaises(gitgate.Reject):
            gitgate.parse_commands(reader(b"zzzz"))

    def test_caps_from_first_command(self):
        _, _, caps = gitgate.parse_commands(reader(push_body("refs/heads/agent/a", "refs/heads/agent/b")))
        self.assertEqual(caps, {b"report-status", b"side-band-64k"})

    def test_raw_bytes_stop_at_flush(self):
        raw, _, _ = gitgate.parse_commands(reader(push_body("refs/heads/agent/a")))
        self.assertTrue(raw.endswith(b"0000"))
        self.assertNotIn(b"PACKDATA", raw)

    def test_rejection_report_with_sideband(self):
        cmds = [(OLD, NEW, "refs/heads/main"), (OLD, NEW, "refs/heads/agent/ok")]
        out = gitgate.push_rejection(cmds, {b"report-status", b"side-band-64k"}, ["refs/heads/main"], HINT)
        self.assertTrue(out.endswith(b"0000"))
        self.assertIn(b"\x02gitgate: push rejected", out)
        self.assertIn(b"unpack ok", out)
        self.assertIn(b"ng refs/heads/main gitgate: only refs/heads/agent/* can be pushed", out)
        self.assertIn(b"ng refs/heads/agent/ok gitgate: not applied", out)

    def test_rejection_report_plain_and_none(self):
        cmds = [(OLD, NEW, "refs/heads/main")]
        plain = gitgate.push_rejection(cmds, {b"report-status"}, ["refs/heads/main"], HINT)
        self.assertTrue(plain.startswith(b"000eunpack ok\n"))
        self.assertNotIn(b"\x02", plain)
        self.assertIsNone(gitgate.push_rejection(cmds, set(), ["refs/heads/main"], HINT))


# ---- proxy with a fake upstream ---------------------------------------------------

class GitProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []
        cls.up = recording_upstream(cls.seen)
        cls.gate = start_gate({"GITGATE_TOKEN": "SECRET", "GITGATE_ALLOW": "me/*",
                               "GITGATE_UPSTREAM": address(cls.up), "GITGATE_SCHEME": "http"})

    @classmethod
    def tearDownClass(cls):
        stop(cls.up, cls.gate)

    def setUp(self):
        self.seen.clear()

    def push(self, body, headers=None):
        return request(self.gate, "POST", "/me/app.git/git-receive-pack", body, headers)

    def test_fetch_passes_with_token(self):
        status, data = request(self.gate, "GET", "/me/app.git/info/refs?service=git-upload-pack")
        self.assertEqual((status, data), (200, b"UPSTREAM-OK"))
        self.assertEqual(self.seen[0][2], gitgate.git_auth("SECRET"))

    def test_allowed_push_passes_body_intact(self):
        body = push_body("refs/heads/agent/x", extra=b"P" * 200000)
        status, _ = self.push(body, {"Content-Type": "application/x-git-receive-pack-request"})
        self.assertEqual(status, 200)
        self.assertEqual(self.seen[0][3], body)

    def test_push_to_main_is_reported_and_not_forwarded(self):
        status, data = self.push(push_body("refs/heads/main"))
        self.assertEqual(status, 200)  # the rejection arrives as report-status so git can show the reason
        self.assertIn(b"ng refs/heads/main gitgate:", data)
        self.assertEqual(self.seen, [])

    def test_push_to_main_without_report_status_gets_403(self):
        status, _ = self.push(pkt(f"{OLD} {NEW} refs/heads/main") + b"0000PACK")
        self.assertEqual(status, 403)
        self.assertEqual(self.seen, [])

    def test_probe_request_is_forwarded(self):
        # git sends a lone "0000" before a push larger than http.postBuffer to check access
        status, _ = self.push(b"0000", {"Content-Type": "application/x-git-receive-pack-request"})
        self.assertEqual(status, 200)
        self.assertEqual(self.seen[0][3], b"0000")

    def test_flush_followed_by_junk_is_denied(self):
        self.assertEqual(self.push(b"0000PACKDATA")[0], 403)
        self.assertEqual(self.seen, [])

    def test_bad_chunked_encoding_is_denied(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.gate.server_port)
        try:
            conn.putrequest("POST", "/me/app.git/git-receive-pack")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            conn.send(b"zz\r\nxx\r\n0\r\n\r\n")
            resp = conn.getresponse()
            self.assertEqual((resp.status, resp.read()), (403, b"bad chunked encoding\n"))
        finally:
            conn.close()
        self.assertEqual(self.seen, [])

    def test_big_rejected_push_is_drained(self):
        status, data = self.push(push_body("refs/heads/main", extra=b"P" * 3_000_000))
        self.assertEqual(status, 200)
        self.assertIn(b"ng refs/heads/main", data)
        self.assertEqual(self.seen, [])

    def test_other_owner_denied(self):
        self.assertEqual(request(self.gate, "GET", "/other/app.git/info/refs?service=git-upload-pack")[0], 403)
        self.assertEqual(self.seen, [])

    def test_junk_paths(self):
        for path in ("/", "/me/app.git/HEAD", "/me/app.git/info/../x", "/me/app.git/objects/info/packs",
                     "/me/app.git/info/refs", "/me/app.git/info/refs?service=other"):
            self.assertIn(request(self.gate, "GET", path)[0], (403, 404), path)
        self.assertEqual(self.seen, [])


class PullRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []
        cls.answer = (201, {"number": 7, "html_url": "https://github.com/me/app/pull/7", "secret": "x"})

        class Api(FakeHandler):
            def do_POST(self):
                cls.seen.append((self.path, self.headers.get("Authorization"), json.loads(self.read_body())))
                self.send_json(*cls.answer)

        cls.api = start(Api)
        env = {"GITGATE_TOKEN": "SECRET", "GITGATE_ALLOW": "me/*", "GITGATE_ENABLE_PR": "1",
               "GITGATE_API_UPSTREAM": address(cls.api), "GITGATE_SCHEME": "http"}
        cls.gate = start_gate(env)
        cls.gate_without_pr = start_gate({**env, "GITGATE_ENABLE_PR": ""})

    @classmethod
    def tearDownClass(cls):
        stop(cls.api, cls.gate, cls.gate_without_pr)

    def setUp(self):
        self.seen.clear()

    def post(self, data, path="/me/app.git/pulls", server=None):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return request(server or self.gate, "POST", path, raw, {"Content-Type": "application/json"})

    @staticmethod
    def valid(**fields):
        return {"head": "agent/feat", "title": "Add feat", **fields}

    def test_creates_pr_and_trims_response(self):
        status, out = self.post(self.valid(body="text", draft=True))
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(out), {"number": 7, "html_url": "https://github.com/me/app/pull/7"})
        path, auth, payload = self.seen[0]
        self.assertEqual((path, auth), ("/repos/me/app/pulls", "Bearer SECRET"))
        self.assertEqual((payload["head"], payload["base"], payload["draft"]), ("agent/feat", "main", True))

    def test_bad_requests_are_not_forwarded(self):
        cases = [
            self.valid(head="main"), self.valid(head="bot/x"), self.valid(head="other:agent/x"),
            self.valid(head="agent/../main"), self.valid(base="release"), self.valid(title=""),
            {"head": "agent/x"}, self.valid(body="x" * 70000), [1], b"not json",
        ]
        for case in cases:
            with self.subTest(case=str(case)[:60]):
                self.assertEqual(self.post(case)[0], 400)
                self.assertEqual(self.seen, [])

    def test_other_owner_denied(self):
        self.assertEqual(self.post(self.valid(), path="/other/app.git/pulls")[0], 403)
        self.assertEqual(self.seen, [])

    def test_github_error_is_passed_as_text_only(self):
        type(self).answer = (422, {"message": "Validation Failed", "errors": [{"message": "no commits"}], "x": 1})
        try:
            status, out = self.post(self.valid())
        finally:
            type(self).answer = (201, {"number": 7, "html_url": "u"})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(out), {"error": "Validation Failed", "errors": ["no commits"]})

    def test_disabled_by_default(self):
        self.assertEqual(self.post(self.valid(), server=self.gate_without_pr)[0], 404)
        self.assertEqual(self.seen, [])


# ---- TOML policy -------------------------------------------------------------------

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

    @classmethod
    def setUpClass(cls):
        cls.seen = []
        cls.up = recording_upstream(cls.seen)
        cls.gate = start_gate({"GITGATE_POLICY": cls.POLICY, "KEY_A": "ka", "KEY_B": "kb", "TOKEN_APPS": "APPSTOKEN",
                               "GITGATE_TOKEN": "DEFAULT", "GITGATE_ALLOW": "me/misc",
                               "GITGATE_UPSTREAM": address(cls.up), "GITGATE_SCHEME": "http"})

    @classmethod
    def tearDownClass(cls):
        stop(cls.up, cls.gate)

    def setUp(self):
        self.seen.clear()

    def fetch(self, repo, user, service="git-upload-pack"):
        return request(self.gate, "GET", f"/me/{repo}.git/info/refs?service={service}", user=user)[0]

    def push(self, repo, ref, user):
        return request(self.gate, "POST", f"/me/{repo}.git/git-receive-pack", push_body(ref), user=user)

    def test_anonymous_or_wrong_key_gets_401(self):
        for user in (None, "a:wrong", "zzz:ka"):
            self.assertEqual(self.fetch("misc", user), 401, user)
        self.assertEqual(self.seen, [])

    def test_per_rule_token(self):
        self.assertEqual(self.fetch("app-1", "a:ka"), 200)
        self.assertEqual(self.fetch("misc", "a:ka"), 200)
        self.assertEqual([auth for _, _, auth, _ in self.seen], [gitgate.git_auth("APPSTOKEN"), gitgate.git_auth("DEFAULT")])

    def test_each_client_has_its_own_prefix(self):
        for user, branch in (("a:ka", "bot-a/x"), ("a:ka", "bot-b/x"), ("b:kb", "bot-b/x"), ("b:kb", "bot-a/x")):
            status, data = self.push("app-1", "refs/heads/" + branch, user)
            self.assertEqual(status, 200)
            own_branch = branch.startswith("bot-" + user[0])
            self.assertEqual(b"ng " not in data, own_branch, (user, branch))
        self.assertEqual(len(self.seen), 2)  # only the two allowed pushes reached upstream

    def test_readonly_repo(self):
        self.assertEqual(self.fetch("docs", "a:ka"), 200)
        self.assertEqual(self.fetch("docs", "a:ka", "git-receive-pack"), 403)
        self.assertEqual(self.push("docs", "refs/heads/agent/x", "a:ka")[0], 403)
        self.assertEqual(len(self.seen), 1)

    def test_catch_all_rule_uses_default_prefix(self):
        self.assertNotIn(b"ng ", self.push("misc", "refs/heads/agent/x", "b:kb")[1])
        self.assertIn(b"ng refs/heads/main", self.push("misc", "refs/heads/main", "b:kb")[1])

    def test_unknown_repo_denied(self):
        self.assertEqual(self.fetch("other", "a:ka"), 403)


class PolicyErrors(unittest.TestCase):
    def test_errors(self):
        for policy, env in (
            ('[[rule]]\nrepos = []', {}),
            ('[[rule]]\nrepos = ["a/b"]\ntoken_env = "NOPE"', {}),
            ('[[rule]]\nrepos = ["a/b"]\nclients = ["ghost"]', {}),
            ('[[rule]]\nrepos = ["a/b"]\nbranch_prefix = "a b"', {}),
            ('[[client]]\nname = "x"\nkey_env = "NOPE"', {}),
            ('[[client]]\nname = "bad name"\nkey_env = "K"', {"K": "k"}),
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                gitgate.Config({"GITGATE_POLICY": policy, "GITGATE_TOKEN": "t", **env})
        with self.assertRaises(ValueError):
            gitgate.Config({"GITGATE_ALLOW": "a/b"})  # no token

    def test_no_policy_is_plain_mode(self):
        cfg = gitgate.Config({"GITGATE_TOKEN": "t", "GITGATE_ALLOW": "a/*"})
        self.assertEqual(len(cfg.rules), 1)
        self.assertIsNone(cfg.authenticate(None))  # no clients configured: anonymous allowed


# ---- GitHub App tokens ---------------------------------------------------------------

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
        cls.key, cls.pub = os.path.join(cls.dir, "k.pem"), os.path.join(cls.dir, "pub.pem")
        subprocess.run(["openssl", "genrsa", "-out", cls.key, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", cls.key, "-pubout", "-out", cls.pub], check=True, capture_output=True)
        cls.seen, cls.mints, cls.jwt_ok = [], [], []
        cls.lifetime, cls.installed = 3600, True

        class FakeGitHub(FakeHandler):
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
                    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + cls.lifetime))
                    return self.send_json(201, {"token": f"ghs_{len(cls.mints)}", "expires_at": expires})
                cls.seen.append((self.path, self.headers.get("Authorization"), json.loads(body or b"{}")))
                self.send_json(201, {"number": 1, "html_url": "u"})

        cls.gh = start(FakeGitHub)
        cls.cfg = gitgate.Config({"GITGATE_POLICY": cls.POLICY % {"key": cls.key}, "GITGATE_SCHEME": "http",
                                  "GITGATE_UPSTREAM": address(cls.gh), "GITGATE_API_UPSTREAM": address(cls.gh)})
        cls.gate = start(gitgate.make_handler(cls.cfg))

    @classmethod
    def tearDownClass(cls):
        stop(cls.gh, cls.gate)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def check_jwt(cls, header):
        """Verify the RS256 signature and the claims of the App JWT with openssl."""
        head, body, sig = header.split(" ", 1)[1].split(".")
        decode = lambda part: base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        sigfile = os.path.join(cls.dir, "sig")
        with open(sigfile, "wb") as f:
            f.write(decode(sig))
        ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", cls.pub, "-signature", sigfile],
                            input=f"{head}.{body}".encode(), capture_output=True).returncode == 0
        claims = json.loads(decode(body))
        return ok and claims["iss"] == "123" and claims["exp"] - claims["iat"] <= 660

    def setUp(self):
        for log in (self.seen, self.mints, self.jwt_ok):
            log.clear()
        type(self).lifetime, type(self).installed = 3600, True
        for app in self.cfg.apps.values():
            app.tokens.clear()
            app.installations.clear()

    def fetch(self, repo, service="git-upload-pack"):
        return request(self.gate, "GET", f"/me/{repo}.git/info/refs?service={service}")[0]

    def test_fetch_uses_minted_narrow_token(self):
        self.assertEqual(self.fetch("app-1"), 200)
        self.assertEqual(self.seen, [gitgate.git_auth("ghs_1")])
        self.assertEqual(self.mints, [{"repositories": ["app-1"],
                                       "permissions": {"contents": "write", "pull_requests": "write"}}])
        self.assertEqual(self.jwt_ok, [True, True])  # installation lookup and token request both signed correctly

    def test_token_is_cached_per_repository(self):
        for _ in range(3):
            self.assertEqual(self.fetch("app-1"), 200)
        self.assertEqual(len(self.mints), 1)
        self.assertEqual(self.seen, [gitgate.git_auth("ghs_1")] * 3)
        self.assertEqual(self.fetch("app-2"), 200)
        self.assertEqual(len(self.mints), 2)

    def test_expiring_token_is_renewed(self):
        type(self).lifetime = 120  # less than the safety margin
        self.fetch("app-1")
        self.fetch("app-1")
        self.assertEqual(len(self.mints), 2)

    def test_readonly_rule_gets_read_only_token(self):
        self.assertEqual(self.fetch("docs"), 200)
        self.assertEqual(self.mints[0]["permissions"], {"contents": "read"})
        self.assertEqual(self.fetch("docs", "git-receive-pack"), 403)

    def test_not_installed_gives_502_and_no_upstream_call(self):
        type(self).installed = False
        self.assertEqual(self.fetch("app-9"), 502)
        self.assertEqual(self.seen, [])

    def test_pull_request_uses_app_token(self):
        status, _ = request(self.gate, "POST", "/me/app-1.git/pulls", json.dumps({"head": "agent/x", "title": "t"}))
        self.assertEqual(status, 201)
        self.assertEqual(self.seen[0][:2], ("/repos/me/app-1/pulls", "Bearer ghs_1"))

    def test_config_errors(self):
        base = f'[[app]]\nname = "a"\napp_id = 1\nprivate_key_file = "{self.key}"\n'
        for policy in (base + '[[rule]]\nrepos=["x/y"]\napp="nope"',
                       base + '[[rule]]\nrepos=["x/y"]\napp="a"\ntoken_env="T"',
                       '[[app]]\nname = "a"\napp_id = 1',
                       '[[app]]\nname = "a"\napp_id = 1\nprivate_key_file = "/nonexistent"',
                       f'[[app]]\nname = "a"\nprivate_key_file = "{self.key}"'):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                gitgate.Config({"GITGATE_POLICY": policy, "T": "t"})

    def test_key_from_environment(self):
        with open(self.key) as f:
            pem = f.read()
        cfg = gitgate.Config({"GITGATE_POLICY": '[[app]]\nname = "a"\napp_id = 123\nprivate_key_env = "PEM"\n'
                              '[[rule]]\nrepos = ["me/*"]\napp = "a"', "PEM": pem})
        self.assertEqual(cfg.apps["a"].jwt().count("."), 2)


if __name__ == "__main__":
    unittest.main()
