"""Проверка gitgate настоящим git: поддельный GitHub (git receive-pack на локальном bare-репозитории).
Запуск: python3 proxy/e2e_realgit.py. Нужен только git. Временный каталог в /tmp можно удалить после."""
import os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gitgate

root = tempfile.mkdtemp(prefix="gitgate-e2e-")
bare = os.path.join(root, "me", "app.git")
os.makedirs(os.path.dirname(bare))
env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", GIT_TERMINAL_PROMPT="0")
def git(*a, cwd=None, check=True):
    r = subprocess.run(["git", *a], cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode: raise SystemExit(f"git {a}: {r.stderr}")
    return r
git("init", "-q", "--bare", "-b", "main", bare)
work = os.path.join(root, "work"); git("init", "-q", "-b", "main", work)
git("commit", "-q", "--allow-empty", "-m", "init", cwd=work)
git("push", "-q", bare, "main", cwd=work)

class Up(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        out = gitgate.pkt(b"# service=git-receive-pack\n") + b"0000" + subprocess.run(
            ["git", "receive-pack", "--advertise-refs", "--stateless-rpc", bare], capture_output=True).stdout
        self.reply(out, "application/x-git-receive-pack-advertisement")
    def do_POST(self):
        body = b""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while True:
                n = int(self.rfile.readline().strip(), 16)
                if n == 0: self.rfile.readline(); break
                body += self.rfile.read(n); self.rfile.readline()
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        out = subprocess.run(["git", "receive-pack", "--stateless-rpc", bare], input=body, capture_output=True).stdout
        self.reply(out, "application/x-git-receive-pack-result")
    def reply(self, out, ctype):
        self.send_response(200); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)

up = ThreadingHTTPServer(("127.0.0.1", 0), Up)
cfg = gitgate.Config({"GITGATE_TOKEN": "x", "GITGATE_ALLOW": "me/*", "GITGATE_SCHEME": "http",
                      "GITGATE_UPSTREAM": f"127.0.0.1:{up.server_port}"})
gate = ThreadingHTTPServer(("127.0.0.1", 0), gitgate.make_handler(cfg))
for s in (up, gate): threading.Thread(target=s.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{gate.server_port}/me/app.git"
git("remote", "add", "gate", url, cwd=work)
git("commit", "-q", "--allow-empty", "-m", "work", cwd=work)
big = os.path.join(work, "big.bin"); open(big, "wb").write(os.urandom(3_000_000))
git("add", "big.bin", cwd=work); git("commit", "-q", "-m", "big", cwd=work)

def run(title, *args):
    r = git(*args, cwd=work, check=False)
    print(f"### {title}  (rc={r.returncode})")
    print("\n".join("   " + l for l in (r.stdout + r.stderr).strip().splitlines() if "gitgate:" not in l.split(" ")[0] or True))
run("hermes/ok (ждём успех, 3 МБ pack)", "push", "gate", "HEAD:refs/heads/hermes/ok")
run("main (ждём понятный отказ)", "push", "gate", "HEAD:refs/heads/main")
run("agent/x", "push", "gate", "HEAD:refs/heads/agent/x")
run("hermes/two + main в одном push", "push", "gate", "HEAD:refs/heads/hermes/two", "HEAD:refs/heads/main")
git("tag", "v1", cwd=work); run("тег", "push", "gate", "v1")
print("### refs в upstream:", git("for-each-ref", "--format=%(refname)", cwd=bare).stdout.split())
