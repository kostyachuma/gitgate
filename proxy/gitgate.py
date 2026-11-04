#!/usr/bin/env python3
"""gitgate: a git smart-HTTP proxy that injects a GitHub token and filters pushes.

The client (e.g. an AI agent) talks to http://gitgate:8080/<owner>/<repo>.git with no credentials.
- fetch (info/refs, git-upload-pack): allowed for repositories matching GITGATE_ALLOW.
- push (git-receive-pack): allowed only if ALL updated refs match GITGATE_REF_REGEX
  (default: refs/heads/agent/...).
- optional (GITGATE_ENABLE_PR=1): POST /<owner>/<repo>.git/pulls opens a pull request, but only
  from a branch the push rule allows into an allowed base branch. Nothing else of the API is exposed.
- everything else: 403/404.

Rules: with GITGATE_ALLOW alone there is one rule for all allowed repositories. A TOML policy
(GITGATE_POLICY_FILE or GITGATE_POLICY) can add [[rule]] and [[client]] tables: per-repository
tokens, branch prefixes, read-only repositories, and named clients (Basic auth: name:key). The
first matching rule wins; GITGATE_ALLOW is the last, catch-all rule. See README.

Settings (environment variables):
  GITGATE_TOKEN or GITGATE_TOKEN_FILE   GitHub token (required)
  GITGATE_ALLOW                          comma-separated owner/repo globs, e.g. "myorg/*"
                                         (empty = deny everything)
  GITGATE_BRANCH_PREFIX                  branches that may be pushed start with this (default: agent/)
  GITGATE_REF_REGEX, GITGATE_REF_HINT    advanced: full regex / hint instead of the prefix
  (policy: [[app]] tables define GitHub Apps; a rule with app = "name" uses short-lived tokens)
  GITGATE_POLICY_FILE / GITGATE_POLICY   TOML policy (file path / inline text), optional
  GITGATE_ENABLE_PR                      1 = enable the pull request endpoint (default: off)
  GITGATE_PR_BASES                       comma-separated base branches PRs may target (default: main)
  GITGATE_PORT                           listen port (8080)
  GITGATE_UPSTREAM, GITGATE_SCHEME       github.com / https (only changed in tests)
  GITGATE_API_UPSTREAM                   api.github.com (only changed in tests)

Standard library only. The token and request headers are never logged.
"""
import base64
import atexit
import datetime
import fnmatch
import hmac
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ZERO = "0" * 40
PATH_RE = re.compile(
    r"^/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/(info/refs|git-upload-pack|git-receive-pack)$"
)
PR_PATH_RE = re.compile(r"^/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/pulls$")
MAX_PR_BODY = 64 * 1024
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
FORWARD_REQ_HEADERS = ("Content-Type", "Accept", "Content-Encoding", "Git-Protocol", "User-Agent")
FORWARD_RESP_HEADERS = ("Content-Type", "Cache-Control", "Expires", "Pragma")


class Reject(Exception):
    pass


class Unauthorized(Exception):
    pass


NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
PREFIX_RE = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/?")


def ref_policy(prefix=None, regex=None, hint=None):
    """Build (compiled ref regex, hint text) from a branch prefix, or from an explicit regex."""
    prefix = (prefix or "agent/").strip()
    if not PREFIX_RE.fullmatch(prefix):
        raise ValueError("branch prefix must look like 'agent/' or 'bots/claude/'")
    prefix = prefix.rstrip("/") + "/"
    ref_re = re.compile(regex or "^refs/heads/" + re.escape(prefix) + r"[A-Za-z0-9._/-]+$")
    return ref_re, hint or f"refs/heads/{prefix}*"


class TokenError(Exception):
    pass


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GitHubApp:
    """Mints short-lived installation tokens for a GitHub App, narrowed to one repository.

    The RS256 JWT is signed with the `openssl` command line tool (the Python standard library has no RSA).
    Tokens are cached until five minutes before they expire."""

    def __init__(self, name, app_id, key_path, api_host, scheme, installation_id=None, key_pem=None):
        self.name, self.app_id, self.installation_id = name, str(app_id), installation_id
        self.api_host, self.scheme = api_host, scheme
        self.lock = threading.Lock()
        self.tokens = {}         # (owner, repo, perms) -> (token, expires_at)
        self.installations = {}  # (owner, repo) -> installation id
        if key_pem:
            d = tempfile.mkdtemp(prefix="gitgate-key-")  # mode 0700
            key_path = os.path.join(d, "app.pem")
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key_pem)
            atexit.register(lambda: (os.path.exists(key_path) and os.unlink(key_path), os.rmdir(d)))
        self.key_path = key_path

    def jwt(self):
        now = int(time.time())
        head = b64url(b'{"alg":"RS256","typ":"JWT"}')
        body = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())
        try:
            sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key_path],
                                 input=f"{head}.{body}".encode(), capture_output=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise TokenError(f"cannot sign the App JWT ({type(e).__name__}); is openssl installed and the key valid?")
        return f"{head}.{body}.{b64url(sig)}"

    def api(self, method, path, bearer, payload=None):
        conn_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(self.api_host, timeout=30)
        try:
            conn.request(method, path, body=json.dumps(payload).encode() if payload is not None else None, headers={
                "Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
                "Content-Type": "application/json", "User-Agent": "gitgate"})
            resp = conn.getresponse()
            raw = resp.read(1024 * 1024)
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, {}
        except (OSError, http.client.HTTPException) as e:
            raise TokenError(f"GitHub API unreachable ({type(e).__name__})")
        finally:
            conn.close()

    def installation_for(self, owner, repo, jwt):
        if self.installation_id:
            return self.installation_id
        key = (owner, repo)
        if key not in self.installations:
            status, info = self.api("GET", f"/repos/{owner}/{repo}/installation", jwt)
            if status != 200 or "id" not in info:
                raise TokenError(f"App {self.name} is not installed on {owner}/{repo} (HTTP {status})")
            self.installations[key] = info["id"]
        return self.installations[key]

    def token(self, owner, repo, perms):
        key = (owner, repo, tuple(sorted(perms.items())))
        with self.lock:
            cached = self.tokens.get(key)
            if cached and cached[1] - time.time() > 300:
                return cached[0]
            jwt = self.jwt()
            inst = self.installation_for(owner, repo, jwt)
            status, info = self.api("POST", f"/app/installations/{inst}/access_tokens", jwt,
                                    {"repositories": [repo], "permissions": perms})
            if status != 201 or not info.get("token"):
                raise TokenError(f"GitHub refused to issue a token for {owner}/{repo} (HTTP {status}: "
                                 f"{str(info.get('message', ''))[:200]})")
            try:
                expires = datetime.datetime.strptime(info["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except (KeyError, ValueError):
                expires = time.time() + 600
            self.tokens[key] = (info["token"], expires)
            return info["token"]


class Rule:
    """What one group of repositories allows: token, pushable refs, PR settings, who may use it."""

    def __init__(self, repos, token, ref_re, ref_hint, readonly=False, enable_pr=False,
                 pr_bases=("main",), clients=None, name="rule", app=None):
        self.repos, self.token, self.ref_re, self.ref_hint = list(repos), token, ref_re, ref_hint
        self.readonly, self.enable_pr, self.pr_bases = readonly, enable_pr, list(pr_bases)
        self.clients, self.name, self.app = clients, name, app

    def matches(self, owner, repo, client):
        if self.clients is not None and client not in self.clients:
            return False
        full = f"{owner}/{repo}"
        return any(fnmatch.fnmatchcase(full, g) for g in self.repos)

    def token_for(self, owner, repo):
        """The token to use for this repository: the static one, or a fresh narrowly scoped App token."""
        if self.app is None:
            return self.token
        perms = {"contents": "read" if self.readonly else "write"}
        if self.enable_pr and not self.readonly:
            perms["pull_requests"] = "write"
        return self.app.token(owner, repo, perms)

    def auth_header(self, owner, repo):
        return "Basic " + base64.b64encode(f"x-access-token:{self.token_for(owner, repo)}".encode()).decode()


def _load_policy(env):
    text, path = env.get("GITGATE_POLICY"), env.get("GITGATE_POLICY_FILE")
    if not text and path:
        with open(path) as f:
            text = f.read()
    if not text:
        return {}
    import tomllib
    return tomllib.loads(text)


def _env_token(env, name, what):
    value = env.get(name, "") if name else ""
    if not value:
        raise ValueError(f"{what}: environment variable {name!r} is empty or not set")
    return value


class Config:
    def __init__(self, env=os.environ):
        token = env.get("GITGATE_TOKEN", "")
        tfile = env.get("GITGATE_TOKEN_FILE")
        if not token and tfile:
            with open(tfile) as f:
                token = f.read().strip()
        self.token = token
        self.allow = [p.strip() for p in env.get("GITGATE_ALLOW", "").split(",") if p.strip()]
        self.ref_re, self.ref_hint = ref_policy(env.get("GITGATE_BRANCH_PREFIX"),
                                                env.get("GITGATE_REF_REGEX"), env.get("GITGATE_REF_HINT"))
        self.upstream = env.get("GITGATE_UPSTREAM", "github.com")
        self.scheme = env.get("GITGATE_SCHEME", "https")
        self.api_upstream = env.get("GITGATE_API_UPSTREAM", "api.github.com")
        self.enable_pr = env.get("GITGATE_ENABLE_PR", "") == "1"
        self.pr_bases = [b.strip() for b in env.get("GITGATE_PR_BASES", "main").split(",") if b.strip()] or ["main"]
        self.port = int(env.get("GITGATE_PORT", "8080"))

        policy = _load_policy(env)
        self.clients = {}
        for c in policy.get("client", []):
            name = c.get("name", "")
            if not NAME_RE.fullmatch(name) or name in self.clients:
                raise ValueError(f"client name {name!r} is invalid or duplicated")
            self.clients[name] = _env_token(env, c.get("key_env"), f"client {name}")
        self.apps = {}
        for a in policy.get("app", []):
            name = a.get("name", "")
            if not NAME_RE.fullmatch(name) or name in self.apps:
                raise ValueError(f"app name {name!r} is invalid or duplicated")
            if not a.get("app_id"):
                raise ValueError(f"app {name}: 'app_id' is required")
            pem = env.get(a["private_key_env"], "") if a.get("private_key_env") else ""
            if bool(pem) == bool(a.get("private_key_file")):
                raise ValueError(f"app {name}: set exactly one of private_key_env (non-empty) or private_key_file")
            if a.get("private_key_file") and not os.path.isfile(a["private_key_file"]):
                raise ValueError(f"app {name}: private_key_file does not exist")
            inst = a.get("installation_id")
            self.apps[name] = GitHubApp(name, a["app_id"], a.get("private_key_file"), self.api_upstream, self.scheme,
                                        installation_id=str(inst) if inst else None, key_pem=pem or None)
        self.rules = []
        for i, r in enumerate(policy.get("rule", []), 1):
            what = f"rule #{i}"
            repos = r.get("repos")
            if not isinstance(repos, list) or not repos or not all(isinstance(g, str) for g in repos):
                raise ValueError(f"{what}: 'repos' must be a non-empty list of owner/repo globs")
            app = None
            if r.get("app"):
                if r.get("token_env"):
                    raise ValueError(f"{what}: use either 'app' or 'token_env', not both")
                if r["app"] not in self.apps:
                    raise ValueError(f"{what}: unknown app {r['app']!r} (define it with [[app]])")
                app, rule_token = self.apps[r["app"]], None
            else:
                rule_token = _env_token(env, r["token_env"], what) if r.get("token_env") else token
                if not rule_token:
                    raise ValueError(f"{what}: no token (set token_env, app or GITGATE_TOKEN)")
            clients = r.get("clients")
            if clients is not None and (not isinstance(clients, list) or not set(clients) <= set(self.clients)):
                raise ValueError(f"{what}: 'clients' must list names defined in [[client]]")
            if "branch_prefix" in r or "ref_regex" in r:
                ref_re, hint = ref_policy(r.get("branch_prefix"), r.get("ref_regex"), r.get("ref_hint"))
            else:
                ref_re, hint = self.ref_re, self.ref_hint
            bases = r.get("pr_bases", self.pr_bases)
            if not isinstance(bases, list) or not bases:
                raise ValueError(f"{what}: 'pr_bases' must be a non-empty list")
            self.rules.append(Rule(repos, rule_token, ref_re, hint, readonly=bool(r.get("readonly", False)),
                                   enable_pr=bool(r.get("enable_pr", self.enable_pr)), pr_bases=bases,
                                   clients=clients, name=what, app=app))
        if self.allow and not token:
            raise ValueError("GITGATE_TOKEN or GITGATE_TOKEN_FILE is required when GITGATE_ALLOW is set")
        if self.allow:  # the plain environment settings act as the last, catch-all rule
            self.rules.append(Rule(self.allow, token, self.ref_re, self.ref_hint, enable_pr=self.enable_pr,
                                   pr_bases=self.pr_bases, name="GITGATE_ALLOW"))

    def authenticate(self, header):
        """Client name from 'Authorization: Basic name:key', None when no clients are configured."""
        if not self.clients:
            return None
        try:
            scheme, _, b64 = (header or "").partition(" ")
            if scheme.lower() != "basic":
                raise ValueError
            name, _, key = base64.b64decode(b64).decode().partition(":")
        except ValueError:
            raise Unauthorized("client credentials required")
        expected = self.clients.get(name)
        ok = hmac.compare_digest((expected or "\0").encode(), key.encode()) and expected is not None
        if not ok:
            raise Unauthorized("invalid client credentials")
        return name

    def find_rule(self, owner, repo, client=None):
        return next((r for r in self.rules if r.matches(owner, repo, client)), None)

    def repo_allowed(self, owner, repo):
        return self.find_rule(owner, repo) is not None


# ---- push command parsing -----------------------------------------------------

def parse_commands(read):
    """read(n) must return exactly n bytes. Reads pkt-line commands up to the flush ('0000').
    Returns (raw bytes, [(old, new, ref), ...], client capabilities)."""
    raw = b""
    cmds = []
    caps = set()
    while True:
        hdr = read(4)
        raw += hdr
        if not re.fullmatch(rb"[0-9a-f]{4}", hdr):
            raise Reject("bad pkt-line header")
        n = int(hdr, 16)
        if n == 0:
            return raw, cmds, caps
        if n < 4 or n > 65520:
            raise Reject("bad pkt-line length")
        payload = read(n - 4)
        raw += payload
        head, _, capstr = payload.partition(b"\0")
        line = head.rstrip(b"\n").decode("utf-8", "replace")
        if line.startswith("shallow "):
            continue
        if line.startswith("push-cert"):
            raise Reject("push certificates are not allowed")
        m = re.fullmatch(r"([0-9a-f]{40}) ([0-9a-f]{40}) (\S+)", line)
        if not m:
            raise Reject("bad push command")
        if not cmds:
            caps = set(capstr.split())
        cmds.append((m.group(1), m.group(2), m.group(3)))


def bad_refs(cmds, ref_re):
    return [ref for _, _, ref in cmds
            if not ref_re.match(ref) or ".." in ref or "//" in ref or ref.endswith("/") or ref.endswith(".lock")]


def check_commands(cmds, ref_re, hint="refs/heads/agent/*"):
    if not cmds:
        raise Reject("no ref updates in push")
    bad = bad_refs(cmds, ref_re)
    if bad:
        raise Reject(f"push allowed only to {hint}; rejected: " + ", ".join(bad))


def check_pull_request(data, ref_re, bases):
    """Validate a PR request body (parsed JSON). Returns the payload to send to GitHub."""
    if not isinstance(data, dict):
        raise Reject("JSON object expected")
    head, base = data.get("head"), data.get("base", bases[0] if bases else "")
    title, text = data.get("title"), data.get("body", "")
    if not isinstance(head, str) or not BRANCH_RE.fullmatch(head) or ":" in head:
        raise Reject("head must be a branch name in this repository")
    if bad_refs([(None, None, "refs/heads/" + head)], ref_re):
        raise Reject("head branch is not one that gitgate lets you push")
    if base not in bases:
        raise Reject("base must be one of: " + ", ".join(bases))
    if not isinstance(title, str) or not title.strip() or len(title) > 256:
        raise Reject("title is required (max 256 chars)")
    if not isinstance(text, str) or len(text) > MAX_PR_BODY:
        raise Reject("body must be a string up to 64 KB")
    return {"title": title, "head": head, "base": base, "body": text,
            "draft": data.get("draft") is True, "maintainer_can_modify": False}


def pkt(data):
    return b"%04x" % (len(data) + 4) + data


def push_rejection(cmds, caps, bad, hint="refs/heads/agent/*"):
    """A receive-pack response that git shows as "remote rejected" with a reason.
    None if the client did not ask for report-status (then a plain 403 is sent)."""
    if b"report-status" not in caps and b"report-status-v2" not in caps:
        return None
    report = pkt(b"unpack ok\n")
    for _, _, ref in cmds:
        why = (f"gitgate: only {hint} can be pushed" if ref in bad
               else "gitgate: not applied, another ref in this push was rejected")
        report += pkt(f"ng {ref} {why}\n".encode())
    report += b"0000"
    if b"side-band-64k" in caps or b"side-band" in caps:
        msg = f"gitgate: push rejected, only {hint} can be pushed\n".encode()
        return pkt(b"\x02" + msg) + pkt(b"\x01" + report) + b"0000"
    return report


MAX_DRAIN = 256 * 1024 * 1024  # how much of a rejected push we drain so the client can read the response


class Body:
    """HTTP request body: Content-Length or chunked."""

    def __init__(self, rfile, headers):
        self.rfile = rfile
        self.chunked = headers.get("Transfer-Encoding", "").lower() == "chunked"
        self.left = int(headers.get("Content-Length") or 0)
        self.cur = 0
        self.done = not self.chunked and self.left == 0

    def read_some(self, n=65536):
        if self.done:
            return b""
        if not self.chunked:
            data = self.rfile.read(min(n, self.left))
            self.left -= len(data)
            if not data or self.left == 0:
                self.done = True
            return data
        if self.cur == 0:
            size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                while self.rfile.readline().strip():
                    pass
                self.done = True
                return b""
            self.cur = size
        data = self.rfile.read(min(n, self.cur))
        self.cur -= len(data)
        if self.cur == 0:
            self.rfile.readline()
        return data

    def read(self, n):
        out = b""
        while len(out) < n:
            part = self.read_some(n - len(out))
            if not part:
                raise Reject("truncated request body")
            out += part
        return out

    def rest(self):
        while True:
            part = self.read_some()
            if not part:
                return
            yield part


# ---- HTTP handler -------------------------------------------------------

def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gitgate"

        client = None

        def log_message(self, fmt, *args):
            who = f"[{self.client}] " if self.client else ""
            sys.stderr.write("gitgate: " + who + (fmt % args) + "\n")

        def deny(self, code, msg):
            data = (msg + "\n").encode()
            self.send_response(code)
            if code == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="gitgate"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True
            self.log_message("DENY %s %s: %s", self.command, self.path.split("?")[0], msg)

        def do_GET(self):
            self.handle_git()

        def do_POST(self):
            self.handle_git()

        def authorize(self, owner, repo):
            """Authenticate the client and find the rule for the repo; answers 401/403 and returns None if not."""
            try:
                self.client = cfg.authenticate(self.headers.get("Authorization"))
            except Unauthorized as e:
                self.deny(401, str(e))
                return None
            rule = cfg.find_rule(owner, repo, self.client)
            if rule is None:
                self.deny(403, f"repository {owner}/{repo} is not allowed")
            return rule

        def handle_pr(self, owner, repo, rule):
            if self.command != "POST":
                return self.deny(405, "POST required")
            try:
                length = int(self.headers.get("Content-Length") or -1)
                if not 0 < length <= MAX_PR_BODY + 4096:
                    raise Reject("Content-Length required, request too large or empty")
                try:
                    payload = check_pull_request(json.loads(self.rfile.read(length)), rule.ref_re, rule.pr_bases)
                except ValueError:
                    raise Reject("invalid JSON")
            except Reject as e:
                return self.deny(400, str(e))
            try:
                bearer = rule.token_for(owner, repo)
            except TokenError as e:
                self.log_message("TOKEN ERROR %s", e)
                return self.deny(502, "could not obtain a GitHub token for this repository")
            conn_cls = http.client.HTTPSConnection if cfg.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(cfg.api_upstream, timeout=30)
            try:
                conn.request("POST", f"/repos/{owner}/{repo}/pulls", body=json.dumps(payload).encode(), headers={
                    "Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json", "User-Agent": "gitgate"})
                resp = conn.getresponse()
                raw = resp.read(1024 * 1024)
                try:
                    info = json.loads(raw)
                except ValueError:
                    info = {}
                if resp.status == 201:
                    out = {"number": info.get("number"), "html_url": info.get("html_url")}
                else:  # only pass GitHub's error text, never headers or the rest of the response
                    out = {"error": str(info.get("message", "GitHub error"))[:500]}
                    if isinstance(info.get("errors"), list):
                        out["errors"] = [str(e.get("message", e)) if isinstance(e, dict) else str(e)
                                         for e in info["errors"]][:5]
                status = resp.status
            except (OSError, http.client.HTTPException) as e:
                self.log_message("UPSTREAM ERROR %s", type(e).__name__)
                status, out = 502, {"error": "upstream unavailable"}
            finally:
                conn.close()
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True
            self.log_message("PR %s/%s head=%s -> %s", owner, repo, payload["head"], status)

        def handle_git(self):
            path, _, query = self.path.partition("?")
            pm = PR_PATH_RE.match(path)
            m = PATH_RE.match(path)
            if pm and any(r.enable_pr for r in cfg.rules):
                owner, repo = pm.groups()
                rule = self.authorize(owner, repo)
                if rule is None:
                    return
                if rule.enable_pr:
                    return self.handle_pr(owner, repo, rule)
                return self.deny(404, "not a git endpoint")
            if not m:
                return self.deny(404, "not a git endpoint")
            owner, repo, what = m.groups()
            rule = self.authorize(owner, repo)
            if rule is None:
                return
            is_push = what == "git-receive-pack" or (what == "info/refs" and "service=git-receive-pack" in query)
            if is_push and rule.readonly:
                return self.deny(403, f"repository {owner}/{repo} is read-only")
            if what == "info/refs":
                if self.command != "GET" or not re.fullmatch(r"service=git-(upload|receive)-pack", query):
                    return self.deny(403, "only smart HTTP is supported")
            elif self.command != "POST":
                return self.deny(405, "POST required")

            body = None
            prefix = b""
            if self.command == "POST":
                body = Body(self.rfile, self.headers)
                if what == "git-receive-pack":
                    if self.headers.get("Content-Encoding"):
                        return self.deny(403, "encoded push bodies are not allowed")
                    cmds = []
                    try:
                        prefix, cmds, caps = parse_commands(body.read)
                        if cmds:
                            check_commands(cmds, rule.ref_re, rule.ref_hint)
                        elif body.read_some():
                            raise Reject("no ref updates in push")
                        # an empty request (a single flush): git probes access this way before pushes over 1 MB
                    except Reject as e:
                        if cmds and bad_refs(cmds, rule.ref_re):
                            return self.reject_push(body, cmds, caps, str(e), rule)
                        return self.deny(403, str(e))
                    self.log_message("PUSH %s/%s %s", owner, repo, " ".join(c[2] for c in cmds) or "(probe)")
                else:
                    self.log_message("FETCH %s/%s", owner, repo)
            else:
                self.log_message("%s %s/%s", "INFO-PUSH" if is_push else "INFO-FETCH", owner, repo)

            self.forward(owner, repo, what, query, body, prefix, rule)

        def reject_push(self, body, cmds, caps, reason, rule):
            out = push_rejection(cmds, caps, bad_refs(cmds, rule.ref_re), rule.ref_hint)
            if out is None:
                return self.deny(403, reason)
            try:
                drained = 0
                for part in body.rest():
                    drained += len(part)
                    if drained > MAX_DRAIN:
                        break
            except OSError:
                pass
            self.send_response(200)
            self.send_header("Content-Type", "application/x-git-receive-pack-result")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(out)
            self.close_connection = True
            self.log_message("DENY PUSH %s: %s", self.path.split("?")[0], reason)

        def forward(self, owner, repo, what, query, body, prefix, rule):
            upath = f"/{owner}/{repo}.git/{what}" + ("?" + query if query else "")
            try:
                headers = {"Authorization": rule.auth_header(owner, repo)}
            except TokenError as e:
                self.log_message("TOKEN ERROR %s", e)
                return self.deny(502, "could not obtain a GitHub token for this repository")
            for h in FORWARD_REQ_HEADERS:
                if self.headers.get(h):
                    headers[h] = self.headers[h]
            conn_cls = http.client.HTTPSConnection if cfg.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(cfg.upstream, timeout=600)
            try:
                if body is None:
                    conn.request("GET", upath, headers=headers)
                else:
                    def gen():
                        if prefix:
                            yield prefix
                        yield from body.rest()
                    headers["Transfer-Encoding"] = "chunked"
                    conn.request("POST", upath, body=gen(), headers=headers, encode_chunked=True)
                resp = conn.getresponse()
                self.send_response(resp.status)
                for h in FORWARD_RESP_HEADERS:
                    if resp.getheader(h):
                        self.send_header(h, resp.getheader(h))
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read1(16384)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (OSError, http.client.HTTPException) as e:
                self.log_message("UPSTREAM ERROR %s", type(e).__name__)
                self.close_connection = True
            finally:
                conn.close()

    return Handler


def main():
    try:
        cfg = Config()
    except (ValueError, OSError, KeyError) as e:
        sys.exit(f"gitgate: bad configuration: {e}")
    if not cfg.rules:
        sys.stderr.write("gitgate: no rules (GITGATE_ALLOW is empty and no policy), every repository will be denied\n")
    srv = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cfg))
    srv.daemon_threads = True
    sys.stderr.write(f"gitgate: listening on :{cfg.port}, {len(cfg.rules)} rule(s), "
                     f"{len(cfg.clients)} client(s)\n")
    for r in cfg.rules:
        sys.stderr.write(f"gitgate:   {r.name}: repos={r.repos} push={r.ref_hint}"
                         f"{' read-only' if r.readonly else ''}{' pr' if r.enable_pr else ''}"
                         f"{' clients=' + ','.join(r.clients) if r.clients is not None else ''}"
                         f"{' app=' + r.app.name if r.app else ''}\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
