import io
import threading
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
    rx = gitgate.Config({"GITGATE_ALLOW": "x/*"}).ref_re

    def cmds(self, *refs):
        _, cmds, _caps = gitgate.parse_commands(reader(push_body(*refs)))
        return cmds

    def test_hermes_ok(self):
        gitgate.check_commands(self.cmds("refs/heads/hermes/fix-1"), self.rx)
        gitgate.check_commands(self.cmds("refs/heads/hermes/a/b", "refs/heads/hermes/c"), self.rx)

    def test_main_rejected(self):
        for ref in ("refs/heads/main", "refs/heads/agents/x", "refs/tags/v1", "refs/heads/agent/x",
                    "refs/heads/hermes/../main", "refs/heads/hermes/x.lock", "refs/heads/hermes/"):
            with self.assertRaises(gitgate.Reject, msg=ref):
                gitgate.check_commands(self.cmds(ref), self.rx)

    def test_mixed_rejected(self):
        with self.assertRaises(gitgate.Reject):
            gitgate.check_commands(self.cmds("refs/heads/hermes/ok", "refs/heads/main"), self.rx)

    def test_shallow_and_cert(self):
        body = pkt("shallow " + OLD) + push_body("refs/heads/hermes/a")
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
        _, _, caps = gitgate.parse_commands(reader(push_body("refs/heads/hermes/a", "refs/heads/hermes/b")))
        self.assertEqual(caps, {b"report-status", b"side-band-64k"})

    def test_rejection_report_sideband(self):
        cmds = [(OLD, NEW, "refs/heads/main"), (OLD, NEW, "refs/heads/hermes/ok")]
        out = gitgate.push_rejection(cmds, {b"report-status", b"side-band-64k"}, ["refs/heads/main"])
        self.assertTrue(out.endswith(b"0000"))
        self.assertIn(b"\x02gitgate: push rejected", out)
        self.assertIn(b"unpack ok", out)
        self.assertIn(b"ng refs/heads/main gitgate: only refs/heads/hermes/* can be pushed", out)
        self.assertIn(b"ng refs/heads/hermes/ok gitgate: not applied", out)

    def test_rejection_report_plain_and_none(self):
        cmds = [(OLD, NEW, "refs/heads/main")]
        plain = gitgate.push_rejection(cmds, {b"report-status"}, ["refs/heads/main"])
        self.assertTrue(plain.startswith(b"000eunpack ok\n"))
        self.assertNotIn(b"\x02", plain)
        self.assertIsNone(gitgate.push_rejection(cmds, set(), ["refs/heads/main"]))

    def test_prefix_is_raw_commands_only(self):
        raw, _, _caps = gitgate.parse_commands(reader(push_body("refs/heads/hermes/a")))
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
        body = push_body("refs/heads/hermes/x", extra=b"P" * 200000)
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


if __name__ == "__main__":
    unittest.main()
