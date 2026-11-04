#!/usr/bin/env python3
"""gitgate: a git smart-HTTP proxy for GitHub that holds the token and limits what a client may push.

Fetch works for allowed repositories, push only to branches under a prefix (default agent/), and
optionally a pull request can be opened from such a branch. Configuration and usage: see README.md.
Standard library only. Tokens and request headers are never logged.
"""
import atexit
import base64
import datetime
import fnmatch
import hmac
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PREFIX = "agent/"
GIT_PATH_RE = re.compile(
    r"^/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/(info/refs|git-upload-pack|git-receive-pack)$")
PR_PATH_RE = re.compile(r"^/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/pulls$")
INFO_QUERY_RE = re.compile(r"service=git-(upload|receive)-pack")
PUSH_COMMAND_RE = re.compile(r"([0-9a-f]{40}) ([0-9a-f]{40}) (\S+)")
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
PREFIX_RE = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*/?")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")

FORWARD_REQ_HEADERS = ("Content-Type", "Accept", "Content-Encoding", "Git-Protocol", "User-Agent")
FORWARD_RESP_HEADERS = ("Content-Type", "Cache-Control", "Expires", "Pragma")

MAX_PR_TITLE = 256
MAX_PR_BODY = 64 * 1024
MAX_API_RESPONSE = 1024 * 1024
MAX_DRAIN = 256 * 1024 * 1024  # how much of a rejected push is read so the client can see the answer
TOKEN_MARGIN = 300             # renew App tokens this many seconds before they expire


class Reject(Exception):
    """The request is refused; the message is shown to the client."""


class Unauthorized(Exception):
    """Missing or wrong client credentials."""


class TokenError(Exception):
    """No GitHub token could be obtained (GitHub App problems)."""


# ---- small helpers ------------------------------------------------------------

def split_list(text):
    return [item.strip() for item in text.split(",") if item.strip()]


def read_file(path):
    if not path:
        return ""
    with open(path) as f:
        return f.read()


def require_env(env, name, what):
    value = env.get(name, "") if name else ""
    if not value:
        raise ValueError(f"{what}: environment variable {name!r} is empty or not set")
    return value


def unique_name(kind, name, seen):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in seen:
        raise ValueError(f"{kind} name {name!r} is invalid or duplicated")
    return name


def normalize_prefix(prefix):
    """'agent' or 'agent/' -> 'agent/'. Empty means the default."""
    prefix = (prefix or DEFAULT_PREFIX).strip()
    if not PREFIX_RE.fullmatch(prefix):
        raise ValueError("branch prefix must look like 'agent/' or 'bots/claude/'")
    return prefix.rstrip("/") + "/"


def ref_allowed(ref, prefix):
    """True for refs/heads/<prefix><something> with a sane branch name."""
    branch = ref.removeprefix("refs/heads/")
    return (branch != ref and branch.startswith(prefix) and len(branch) > len(prefix)
            and bool(BRANCH_RE.fullmatch(branch)) and ".." not in branch and "//" not in branch
            and not branch.endswith(("/", ".lock")))


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def git_auth(token):
    return "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def connect(host, scheme, timeout):
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    return cls(host, timeout=timeout)


def github_api(host, scheme, method, path, bearer, payload=None):
    """Call the GitHub REST API. Returns (status, JSON object or {}). Raises OSError/HTTPException."""
    conn = connect(host, scheme, timeout=30)
    try:
        conn.request(method, path, body=None if payload is None else json.dumps(payload).encode(), headers={
            "Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "User-Agent": "gitgate"})
        resp = conn.getresponse()
        raw = resp.read(MAX_API_RESPONSE)
    finally:
        conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return resp.status, data if isinstance(data, dict) else {}


def private_key_file(pem):
    """openssl reads the key from a file: write it to a private temporary file, removed at exit."""
    directory = tempfile.mkdtemp(prefix="gitgate-key-")  # mode 0700
    path = os.path.join(directory, "app.pem")
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(pem)
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    return path


# ---- GitHub App tokens ----------------------------------------------------------

class GitHubApp:
    """Mints short-lived installation tokens for a GitHub App, narrowed to one repository.

    The RS256 JWT is signed with the `openssl` command line tool (the standard library has no RSA).
    Tokens are cached until shortly before they expire."""

    def __init__(self, name, app_id, key_file, api_host, scheme, installation_id=None):
        self.name, self.app_id, self.key_file = name, str(app_id), key_file
        self.api_host, self.scheme = api_host, scheme
        self.installation_id = str(installation_id) if installation_id else None
        self.lock = threading.Lock()
        self.tokens = {}         # (owner, repo, permissions) -> (token, expires_at)
        self.installations = {}  # (owner, repo) -> installation id

    def jwt(self):
        now = int(time.time())
        head = b64url(b'{"alg":"RS256","typ":"JWT"}')
        body = b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())
        try:
            sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key_file], input=f"{head}.{body}".encode(),
                                 capture_output=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            raise TokenError(f"cannot sign the App JWT ({type(e).__name__}); is openssl installed and the key valid?")
        return f"{head}.{body}.{b64url(sig)}"

    def api(self, method, path, jwt, payload=None):
        try:
            return github_api(self.api_host, self.scheme, method, path, jwt, payload)
        except (OSError, http.client.HTTPException) as e:
            raise TokenError(f"GitHub API unreachable ({type(e).__name__})")

    def installation_for(self, owner, repo, jwt):
        if self.installation_id:
            return self.installation_id
        if (owner, repo) not in self.installations:
            status, info = self.api("GET", f"/repos/{owner}/{repo}/installation", jwt)
            if status != 200 or "id" not in info:
                raise TokenError(f"App {self.name} is not installed on {owner}/{repo} (HTTP {status})")
            self.installations[owner, repo] = info["id"]
        return self.installations[owner, repo]

    def token(self, owner, repo, permissions):
        key = (owner, repo, tuple(sorted(permissions.items())))
        with self.lock:
            cached = self.tokens.get(key)
            if cached and cached[1] - time.time() > TOKEN_MARGIN:
                return cached[0]
            jwt = self.jwt()
            installation = self.installation_for(owner, repo, jwt)
            status, info = self.api("POST", f"/app/installations/{installation}/access_tokens", jwt,
                                    {"repositories": [repo], "permissions": permissions})
            if status != 201 or not info.get("token"):
                raise TokenError(f"GitHub refused to issue a token for {owner}/{repo} "
                                 f"(HTTP {status}: {str(info.get('message', ''))[:200]})")
            try:
                expires = datetime.datetime.fromisoformat(info["expires_at"]).timestamp()
            except (KeyError, TypeError, ValueError):
                expires = time.time() + 600
            self.tokens[key] = (info["token"], expires)
            return info["token"]


# ---- configuration --------------------------------------------------------------

@dataclass
class Rule:
    """What one group of repositories allows: token, pushable branches, PRs, and who may use it."""
    name: str
    repos: list
    token: str | None = None
    app: GitHubApp | None = None
    branch_prefix: str = DEFAULT_PREFIX
    readonly: bool = False
    enable_pr: bool = False
    pr_bases: list = field(default_factory=lambda: ["main"])
    clients: list | None = None  # None: any client

    @property
    def push_hint(self):
        return f"refs/heads/{self.branch_prefix}*"

    def matches(self, owner, repo, client):
        if self.clients is not None and client not in self.clients:
            return False
        return any(fnmatch.fnmatchcase(f"{owner}/{repo}", glob) for glob in self.repos)

    def token_for(self, owner, repo):
        """The static token, or a fresh App token with only the permissions this rule needs."""
        if self.app is None:
            return self.token
        permissions = {"contents": "read" if self.readonly else "write"}
        if self.enable_pr and not self.readonly:
            permissions["pull_requests"] = "write"
        return self.app.token(owner, repo, permissions)


class Config:
    """Plain settings from the environment plus the optional TOML policy (see README)."""

    def __init__(self, env=os.environ):
        self.token = env.get("GITGATE_TOKEN") or read_file(env.get("GITGATE_TOKEN_FILE")).strip()
        self.allow = split_list(env.get("GITGATE_ALLOW", ""))
        self.branch_prefix = normalize_prefix(env.get("GITGATE_BRANCH_PREFIX"))
        self.enable_pr = env.get("GITGATE_ENABLE_PR") == "1"
        self.pr_bases = split_list(env.get("GITGATE_PR_BASES", "")) or ["main"]
        self.port = int(env.get("GITGATE_PORT", "8080"))
        self.upstream = env.get("GITGATE_UPSTREAM", "github.com")             # changed only in tests
        self.api_upstream = env.get("GITGATE_API_UPSTREAM", "api.github.com")  # changed only in tests
        self.scheme = env.get("GITGATE_SCHEME", "https")                      # changed only in tests

        text = env.get("GITGATE_POLICY") or read_file(env.get("GITGATE_POLICY_FILE"))
        policy = tomllib.loads(text) if text else {}
        self.clients = self._parse_clients(policy.get("client", []), env)
        self.apps = self._parse_apps(policy.get("app", []), env)
        self.rules = [self._parse_rule(f"rule #{i}", table, env) for i, table in enumerate(policy.get("rule", []), 1)]
        if self.allow:  # the plain settings act as the last, catch-all rule
            if not self.token:
                raise ValueError("GITGATE_TOKEN or GITGATE_TOKEN_FILE is required when GITGATE_ALLOW is set")
            self.rules.append(Rule("GITGATE_ALLOW", self.allow, token=self.token, branch_prefix=self.branch_prefix,
                                   enable_pr=self.enable_pr, pr_bases=self.pr_bases))

    @staticmethod
    def _parse_clients(tables, env):
        clients = {}
        for table in tables:
            name = unique_name("client", table.get("name", ""), clients)
            clients[name] = require_env(env, table.get("key_env"), f"client {name}")
        return clients

    def _parse_apps(self, tables, env):
        apps = {}
        for table in tables:
            name = unique_name("app", table.get("name", ""), apps)
            if not table.get("app_id"):
                raise ValueError(f"app {name}: 'app_id' is required")
            pem, key_file = env.get(table.get("private_key_env", ""), ""), table.get("private_key_file")
            if bool(pem) == bool(key_file):
                raise ValueError(f"app {name}: set exactly one of private_key_env (non-empty) or private_key_file")
            if key_file and not os.path.isfile(key_file):
                raise ValueError(f"app {name}: private_key_file does not exist")
            apps[name] = GitHubApp(name, table["app_id"], key_file or private_key_file(pem), self.api_upstream,
                                   self.scheme, installation_id=table.get("installation_id"))
        return apps

    def _parse_rule(self, name, table, env):
        repos = table.get("repos")
        if not (isinstance(repos, list) and repos and all(isinstance(glob, str) for glob in repos)):
            raise ValueError(f"{name}: 'repos' must be a non-empty list of owner/repo globs")
        clients = table.get("clients")
        if clients is not None and not (isinstance(clients, list) and set(clients) <= set(self.clients)):
            raise ValueError(f"{name}: 'clients' must list names defined in [[client]]")
        bases = table.get("pr_bases", self.pr_bases)
        if not (isinstance(bases, list) and bases):
            raise ValueError(f"{name}: 'pr_bases' must be a non-empty list")
        app = token = None
        if table.get("app"):
            if table.get("token_env"):
                raise ValueError(f"{name}: use either 'app' or 'token_env', not both")
            app = self.apps.get(table["app"])
            if app is None:
                raise ValueError(f"{name}: unknown app {table['app']!r} (define it with [[app]])")
        else:
            token = require_env(env, table["token_env"], name) if table.get("token_env") else self.token
            if not token:
                raise ValueError(f"{name}: no token (set token_env, app or GITGATE_TOKEN)")
        return Rule(name, repos, token=token, app=app,
                    branch_prefix=normalize_prefix(table.get("branch_prefix", self.branch_prefix)),
                    readonly=bool(table.get("readonly", False)), enable_pr=bool(table.get("enable_pr", self.enable_pr)),
                    pr_bases=bases, clients=clients)

    def authenticate(self, header):
        """Client name from 'Authorization: Basic name:key'; None when no clients are configured."""
        if not self.clients:
            return None
        try:
            scheme, _, encoded = (header or "").partition(" ")
            if scheme.lower() != "basic":
                raise ValueError
            name, _, key = base64.b64decode(encoded).decode().partition(":")
        except ValueError:
            raise Unauthorized("client credentials required") from None
        expected = self.clients.get(name)
        if expected is None or not hmac.compare_digest(expected.encode(), key.encode()):
            raise Unauthorized("invalid client credentials")
        return name

    def find_rule(self, owner, repo, client=None):
        """The first rule that matches the repository and the client, or None."""
        return next((rule for rule in self.rules if rule.matches(owner, repo, client)), None)


# ---- git protocol -----------------------------------------------------------------

def pkt(data):
    return b"%04x" % (len(data) + 4) + data


def parse_commands(read):
    """Read the pkt-line push commands up to the flush ('0000'). read(n) must return exactly n bytes.
    Returns (raw bytes read, [(old, new, ref), ...], client capabilities)."""
    raw, cmds, caps = b"", [], set()
    while True:
        header = read(4)
        raw += header
        if not re.fullmatch(rb"[0-9a-f]{4}", header):
            raise Reject("bad pkt-line header")
        size = int(header, 16)
        if size == 0:
            return raw, cmds, caps
        if not 4 <= size <= 65520:
            raise Reject("bad pkt-line length")
        payload = read(size - 4)
        raw += payload
        line, _, capstr = payload.partition(b"\0")
        line = line.rstrip(b"\n").decode("utf-8", "replace")
        if line.startswith("shallow "):
            continue
        if line.startswith("push-cert"):
            raise Reject("push certificates are not allowed")
        m = PUSH_COMMAND_RE.fullmatch(line)
        if not m:
            raise Reject("bad push command")
        if not cmds:
            caps = set(capstr.split())
        cmds.append(m.groups())


def bad_refs(cmds, prefix):
    return [ref for _, _, ref in cmds if not ref_allowed(ref, prefix)]


def push_rejection(cmds, caps, bad, hint):
    """A receive-pack answer that git shows as "remote rejected" with a reason.
    None if the client did not ask for report-status (then a plain 403 is sent)."""
    if not caps & {b"report-status", b"report-status-v2"}:
        return None
    report = pkt(b"unpack ok\n")
    for _, _, ref in cmds:
        why = f"gitgate: only {hint} can be pushed" if ref in bad else \
            "gitgate: not applied, another ref in this push was rejected"
        report += pkt(f"ng {ref} {why}\n".encode())
    report += b"0000"
    if caps & {b"side-band", b"side-band-64k"}:
        message = f"gitgate: push rejected, only {hint} can be pushed\n".encode()
        return pkt(b"\x02" + message) + pkt(b"\x01" + report) + b"0000"
    return report


def check_pull_request(data, prefix, bases):
    """Validate a PR request (parsed JSON). Returns the payload to send to GitHub."""
    if not isinstance(data, dict):
        raise Reject("JSON object expected")
    head, base = data.get("head"), data.get("base", bases[0])
    title, text = data.get("title"), data.get("body", "")
    if not isinstance(head, str) or not BRANCH_RE.fullmatch(head):
        raise Reject("head must be a branch name in this repository")
    if not ref_allowed("refs/heads/" + head, prefix):
        raise Reject("head branch is not one that gitgate lets you push")
    if base not in bases:
        raise Reject("base must be one of: " + ", ".join(bases))
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_PR_TITLE:
        raise Reject(f"title is required (max {MAX_PR_TITLE} chars)")
    if not isinstance(text, str) or len(text) > MAX_PR_BODY:
        raise Reject("body must be a string up to 64 KB")
    return {"title": title, "head": head, "base": base, "body": text,
            "draft": data.get("draft") is True, "maintainer_can_modify": False}


def pr_result(status, info):
    """What the client sees of GitHub's answer: the PR number and link, or only the error text."""
    if status == 201:
        return {"number": info.get("number"), "html_url": info.get("html_url")}
    out = {"error": str(info.get("message", "GitHub error"))[:500]}
    if isinstance(info.get("errors"), list):
        out["errors"] = [str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in info["errors"]][:5]
    return out


class Body:
    """HTTP request body, with Content-Length or chunked."""

    def __init__(self, rfile, headers):
        self.rfile = rfile
        self.chunked = headers.get("Transfer-Encoding", "").lower() == "chunked"
        self.left = int(headers.get("Content-Length") or 0)  # bytes left: whole body, or current chunk
        self.done = not self.chunked and self.left == 0

    def read_some(self, n=65536):
        if self.done:
            return b""
        if self.chunked and self.left == 0:
            try:
                self.left = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
            except ValueError:
                raise Reject("bad chunked encoding") from None
            if self.left == 0:
                while self.rfile.readline().strip():  # trailers
                    pass
                self.done = True
                return b""
        data = self.rfile.read(min(n, self.left))
        self.left -= len(data)
        if not self.chunked and (not data or self.left == 0):
            self.done = True
        elif self.chunked and self.left == 0:
            self.rfile.readline()  # CRLF after the chunk
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
        while part := self.read_some():
            yield part


# ---- HTTP handler -------------------------------------------------------------------

def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gitgate"
        client = None

        def log_message(self, fmt, *args):
            who = f"[{self.client}] " if self.client else ""
            sys.stderr.write(f"gitgate: {who}{fmt % args}\n")

        def reply(self, code, data, content_type, headers=()):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def deny(self, code, message):
            auth = [("WWW-Authenticate", 'Basic realm="gitgate"')] if code == 401 else []
            self.reply(code, (message + "\n").encode(), "text/plain; charset=utf-8", auth)
            self.log_message("DENY %s %s: %s", self.command, self.path.split("?")[0], message)

        def route(self):
            path, _, query = self.path.partition("?")
            if m := PR_PATH_RE.match(path):
                owner, repo = m.groups()
                if rule := self.authorize(owner, repo):
                    if rule.enable_pr:
                        self.handle_pr(owner, repo, rule)
                    else:
                        self.deny(404, "not a git endpoint")
            elif m := GIT_PATH_RE.match(path):
                owner, repo, service = m.groups()
                if rule := self.authorize(owner, repo):
                    self.handle_git(owner, repo, service, query, rule)
            else:
                self.deny(404, "not a git endpoint")

        do_GET = do_POST = route

        def authorize(self, owner, repo):
            """The rule for this client and repository, or None after answering 401/403."""
            try:
                self.client = cfg.authenticate(self.headers.get("Authorization"))
            except Unauthorized as e:
                return self.deny(401, str(e))
            rule = cfg.find_rule(owner, repo, self.client)
            if rule is None:
                self.deny(403, f"repository {owner}/{repo} is not allowed")
            return rule

        def github_token(self, owner, repo, rule):
            """The GitHub token for this request, or None after answering 502."""
            try:
                return rule.token_for(owner, repo)
            except TokenError as e:
                self.log_message("TOKEN ERROR %s", e)
                return self.deny(502, "could not obtain a GitHub token for this repository")

        # -- pull requests --

        def read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if not 0 < length <= MAX_PR_BODY + 4096:
                raise Reject("Content-Length required, request too large or empty")
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                raise Reject("invalid JSON") from None

        def handle_pr(self, owner, repo, rule):
            if self.command != "POST":
                return self.deny(405, "POST required")
            try:
                payload = check_pull_request(self.read_json(), rule.branch_prefix, rule.pr_bases)
            except Reject as e:
                return self.deny(400, str(e))
            token = self.github_token(owner, repo, rule)
            if token is None:
                return
            try:
                status, info = github_api(cfg.api_upstream, cfg.scheme, "POST", f"/repos/{owner}/{repo}/pulls",
                                          token, payload)
                out = pr_result(status, info)
            except (OSError, http.client.HTTPException) as e:
                self.log_message("UPSTREAM ERROR %s", type(e).__name__)
                status, out = 502, {"error": "upstream unavailable"}
            self.reply(status, json.dumps(out).encode(), "application/json")
            self.log_message("PR %s/%s head=%s -> %s", owner, repo, payload["head"], status)

        # -- git --

        def handle_git(self, owner, repo, service, query, rule):
            if service == "info/refs":
                if self.command != "GET" or not INFO_QUERY_RE.fullmatch(query):
                    return self.deny(403, "only smart HTTP is supported")
            elif self.command != "POST":
                return self.deny(405, "POST required")
            is_push = service == "git-receive-pack" or query == "service=git-receive-pack"
            if is_push and rule.readonly:
                return self.deny(403, f"repository {owner}/{repo} is read-only")

            body = Body(self.rfile, self.headers) if self.command == "POST" else None
            head = b""
            if service == "git-receive-pack":
                if self.headers.get("Content-Encoding"):
                    return self.deny(403, "encoded push bodies are not allowed")
                checked = self.check_push(body, rule)
                if checked is None:
                    return
                head, cmds = checked
                self.log_message("PUSH %s/%s %s", owner, repo, " ".join(ref for _, _, ref in cmds) or "(probe)")
            elif service == "git-upload-pack":
                self.log_message("FETCH %s/%s", owner, repo)
            else:
                self.log_message("%s %s/%s", "INFO-PUSH" if is_push else "INFO-FETCH", owner, repo)
            self.forward(owner, repo, service, query, body, head, rule)

        def check_push(self, body, rule):
            """Read and check the ref updates of a push.
            Returns (bytes read, commands), or None after answering with a refusal."""
            try:
                head, cmds, caps = parse_commands(body.read)
                if not cmds and body.read_some():
                    # a lone flush is fine: git probes access that way before pushes larger than http.postBuffer
                    raise Reject("no ref updates in push")
            except Reject as e:
                return self.deny(403, str(e))
            bad = bad_refs(cmds, rule.branch_prefix)
            if bad:
                return self.reject_push(body, cmds, caps, bad, rule)
            return head, cmds

        def reject_push(self, body, cmds, caps, bad, rule):
            reason = f"push allowed only to {rule.push_hint}; rejected: " + ", ".join(bad)
            out = push_rejection(cmds, caps, bad, rule.push_hint)
            if out is None:
                return self.deny(403, reason)
            try:  # git reads the answer only after sending the whole pack
                drained = 0
                for part in body.rest():
                    drained += len(part)
                    if drained > MAX_DRAIN:
                        break
            except (OSError, Reject):
                pass
            self.reply(200, out, "application/x-git-receive-pack-result", [("Cache-Control", "no-cache")])
            self.log_message("DENY PUSH %s: %s", self.path.split("?")[0], reason)

        def forward(self, owner, repo, service, query, body, head, rule):
            token = self.github_token(owner, repo, rule)
            if token is None:
                return
            headers = {"Authorization": git_auth(token)}
            headers.update((h, self.headers[h]) for h in FORWARD_REQ_HEADERS if self.headers.get(h))
            path = f"/{owner}/{repo}.git/{service}" + (f"?{query}" if query else "")
            conn = connect(cfg.upstream, cfg.scheme, timeout=600)
            try:
                if body is None:
                    conn.request("GET", path, headers=headers)
                else:
                    def stream():
                        if head:
                            yield head
                        yield from body.rest()
                    headers["Transfer-Encoding"] = "chunked"
                    conn.request("POST", path, body=stream(), headers=headers, encode_chunked=True)
                resp = conn.getresponse()
                self.send_response(resp.status)
                for h in FORWARD_RESP_HEADERS:
                    if resp.getheader(h):
                        self.send_header(h, resp.getheader(h))
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while chunk := resp.read1(16384):
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (OSError, http.client.HTTPException, Reject) as e:
                self.log_message("UPSTREAM ERROR %s", type(e).__name__)
                self.close_connection = True
            finally:
                conn.close()

    return Handler


def describe(rule):
    flags = [f"repos={rule.repos}", f"push={rule.push_hint}"]
    if rule.readonly:
        flags.append("read-only")
    if rule.enable_pr:
        flags.append("pr")
    if rule.clients is not None:
        flags.append("clients=" + ",".join(rule.clients))
    if rule.app:
        flags.append("app=" + rule.app.name)
    return f"{rule.name}: " + " ".join(flags)


def main():
    try:
        cfg = Config()
    except (ValueError, TypeError, KeyError, OSError) as e:
        sys.exit(f"gitgate: bad configuration: {e}")
    if not cfg.rules:
        sys.stderr.write("gitgate: no rules (GITGATE_ALLOW is empty and no policy), every repository will be denied\n")
    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cfg))
    server.daemon_threads = True
    sys.stderr.write(f"gitgate: listening on :{cfg.port}, {len(cfg.rules)} rule(s), {len(cfg.clients)} client(s)\n")
    for rule in cfg.rules:
        sys.stderr.write(f"gitgate:   {describe(rule)}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
