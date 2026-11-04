"""gitgate with real git against a fake GitHub (git receive-pack on a local bare repository)."""
import os
import shutil
import subprocess
import tempfile
import unittest

import gitgate
from test_gitgate import FakeHandler, start, stop, address

POLICY = """
[[client]]
name = "bot"
key_env = "BOT_KEY"

[[rule]]
repos = ["me/*"]
clients = ["bot"]
"""


@unittest.skipUnless(shutil.which("git"), "git is needed")
class RealGit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="gitgate-e2e-")
        cls.bare = os.path.join(cls.tmp.name, "me", "app.git")
        cls.work = os.path.join(cls.tmp.name, "work")
        cls.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t", "GIT_TERMINAL_PROMPT": "0",
                   "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}  # no credential helpers

        cls.git("init", "-q", "--bare", "-b", "main", cls.bare, cwd=cls.tmp.name)
        cls.git("init", "-q", "-b", "main", cls.work, cwd=cls.tmp.name)
        cls.git("commit", "-q", "--allow-empty", "-m", "init")
        cls.git("push", "-q", cls.bare, "main")
        cls.main = cls.git("rev-parse", "main").stdout.strip()
        with open(os.path.join(cls.work, "big.bin"), "wb") as f:
            f.write(os.urandom(3_000_000))  # larger than http.postBuffer: git probes before pushing
        cls.git("add", "big.bin")
        cls.git("commit", "-q", "-m", "big")
        cls.git("tag", "v1")

        bare = cls.bare

        class FakeGitHub(FakeHandler):
            def do_GET(self):
                refs = subprocess.run(["git", "receive-pack", "--advertise-refs", "--stateless-rpc", bare],
                                      capture_output=True).stdout
                self.send(200, gitgate.pkt(b"# service=git-receive-pack\n") + b"0000" + refs,
                          "application/x-git-receive-pack-advertisement")

            def do_POST(self):
                out = subprocess.run(["git", "receive-pack", "--stateless-rpc", bare],
                                     input=self.read_body(), capture_output=True).stdout
                self.send(200, out, "application/x-git-receive-pack-result")

        cls.upstream = start(FakeGitHub)
        cls.gate = start(gitgate.make_handler(gitgate.Config({
            "GITGATE_TOKEN": "x", "GITGATE_POLICY": POLICY, "BOT_KEY": "secret",
            "GITGATE_SCHEME": "http", "GITGATE_UPSTREAM": address(cls.upstream)})))

    @classmethod
    def tearDownClass(cls):
        stop(cls.upstream, cls.gate)
        cls.tmp.cleanup()

    @classmethod
    def git(cls, *args, cwd=None, check=True):
        return subprocess.run(["git", *args], cwd=cwd or cls.work, env=cls.env, capture_output=True, text=True,
                              check=check)

    def push(self, user, *refspecs):
        credentials = f"{user}@" if user else ""
        url = f"http://{credentials}{address(self.gate)}/me/app.git"
        result = self.git("push", url, *refspecs, check=False)
        return result.returncode, result.stdout + result.stderr

    def upstream_refs(self):
        return self.git("for-each-ref", "--format=%(refname)", cwd=self.bare).stdout.split()

    def test_wrong_or_missing_key_is_refused(self):
        for user in ("bot:wrong", None):
            self.assertNotEqual(self.push(user, "HEAD:refs/heads/agent/nope")[0], 0, user)
        self.assertNotIn("refs/heads/agent/nope", self.upstream_refs())

    def test_allowed_branch_is_pushed(self):
        code, out = self.push("bot:secret", "HEAD:refs/heads/agent/ok")
        self.assertEqual(code, 0, out)
        self.assertIn("refs/heads/agent/ok", self.upstream_refs())

    def test_forbidden_refs_are_rejected_with_reason(self):
        for refspecs in (["HEAD:refs/heads/main"], ["HEAD:refs/heads/bot/x"], ["v1"],
                         ["HEAD:refs/heads/agent/two", "HEAD:refs/heads/main"]):
            with self.subTest(refspecs=refspecs):
                code, out = self.push("bot:secret", *refspecs)
                self.assertNotEqual(code, 0)
                self.assertIn("gitgate: only refs/heads/agent/* can be pushed", out)
        refs = self.upstream_refs()
        for ref in ("refs/heads/bot/x", "refs/tags/v1", "refs/heads/agent/two"):
            self.assertNotIn(ref, refs)
        self.assertEqual(self.git("rev-parse", "main", cwd=self.bare).stdout.strip(), self.main)


if __name__ == "__main__":
    unittest.main()
