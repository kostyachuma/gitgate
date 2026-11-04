# gitgate

A tiny git proxy for GitHub (Python standard library only) that sits between an AI agent and GitHub.
The agent never sees the GitHub token; the proxy holds it and decides what may be pushed.

- **fetch / clone** works for repositories matching `GITGATE_ALLOW` (e.g. `myorg/*`).
- **push** works only if every updated ref is a branch under the configured prefix (default `refs/heads/agent/*`).
- With `GITGATE_ENABLE_PR=1` the agent can also open a GitHub pull request (see below).
- `main`, tags, other branch prefixes and push certificates are rejected, and git prints the reason:
  `! [remote rejected] ... (gitgate: only refs/heads/agent/* can be pushed)`.

Useful on GitHub Free, where private repositories have no branch protection, so nothing on GitHub's side stops
an agent with a write token from pushing to `main`.

## How it works

```
agent --(no credentials)--> gitgate --(token)--> github.com
        http://gitgate:8080/<owner>/<repo>.git
```

The agent keeps full local git (rebase, amend, worktrees, ...). Only the network push is restricted.

## Pull requests (optional)

Set `GITGATE_ENABLE_PR=1` and the agent can open a PR without ever holding an API token:

```bash
curl -s -X POST http://gitgate:8080/myorg/myrepo.git/pulls \
  -H 'Content-Type: application/json' \
  -d '{"head": "agent/my-feature", "title": "Add my feature", "body": "Details...", "draft": true}'
# {"number": 12, "html_url": "https://github.com/myorg/myrepo/pull/12"}
```

Rules: `head` must be a branch the push rule allows (no `owner:branch` forks), `base` must be listed in
`GITGATE_PR_BASES` (default `main`), title up to 256 chars, body up to 64 KB. Only PR creation is exposed:
no merge, no comments, no other GitHub API. A human still reviews and merges.

## Quick start

```bash
docker build -t gitgate proxy
docker run -d -p 8080:8080 \
  -e GITGATE_TOKEN=<github-token> \
  -e GITGATE_ALLOW='myorg/*' \
  gitgate
```

Then, in the agent's repository:

```bash
git remote set-url origin http://localhost:8080/myorg/myrepo.git
```

Use a GitHub fine-grained personal access token with Contents and Pull requests read/write, and without Workflows.

## Configuration

| Variable | Meaning |
|---|---|
| `GITGATE_TOKEN` or `GITGATE_TOKEN_FILE` | GitHub token (required) |
| `GITGATE_ALLOW` | Comma-separated `owner/repo` globs. Empty denies everything. |
| `GITGATE_BRANCH_PREFIX` | Branches the agent may push must start with this. Default: `agent/` |
| `GITGATE_REF_REGEX`, `GITGATE_REF_HINT` | Advanced: a full regex and rejection text instead of the prefix |
| `GITGATE_POLICY_FILE` or `GITGATE_POLICY` | Optional TOML policy: several tokens, rules and named agents (below) |
| `GITGATE_ENABLE_PR` | `1` enables `POST /<owner>/<repo>.git/pulls`. Default: off. |
| `GITGATE_PR_BASES` | Comma-separated base branches a PR may target. Default: `main` |
| `GITGATE_PORT` | Listen port. Default: `8080` |

## Deploy on Coolify

1. `python3 coolify/make_compose.py > coolify/docker-compose.yaml` (after any change to `gitgate.py`).
2. In Coolify: New Resource, Docker Compose (empty), paste the file.
3. Set `GITGATE_TOKEN` (secret), `GITGATE_ALLOW`, optionally `GITGATE_BRANCH_PREFIX`, `GITGATE_ENABLE_PR=1`, and `GITGATE_NETWORK` (the existing Docker network
   your agent containers are on). Do not assign a domain.
4. From the agent's containers the proxy is reachable as `http://gitgate:8080`.

## Several repositories, tokens and agents (optional)

Everything above uses one token and one rule. For more, add a TOML policy (`GITGATE_POLICY_FILE` = path, or
`GITGATE_POLICY` = the text itself). It is fully optional: without it nothing changes.

```toml
# Named agents. Each has a secret key, read from an environment variable.
[[client]]
name = "alice"
key_env = "KEY_ALICE"

[[client]]
name = "bob"
key_env = "KEY_BOB"

# The first rule that matches the repository (and the client) wins.
[[rule]]
repos = ["myorg/docs"]
readonly = true                 # fetch only, no pushes at all

[[rule]]
repos = ["myorg/app-*"]
clients = ["alice"]             # only alice, and only her own branches
branch_prefix = "alice/"
token_env = "TOKEN_MYORG"       # a different token for this rule

[[rule]]
repos = ["otherorg/*"]
clients = ["alice", "bob"]
branch_prefix = "agent/"
token_env = "TOKEN_OTHERORG"
enable_pr = true
pr_bases = ["main", "develop"]
```

Rule fields: `repos` (required, `owner/repo` globs), `token_env`, `branch_prefix`, `readonly`, `enable_pr`,
`pr_bases`, `clients`. Anything left out falls back to the plain settings (`GITGATE_TOKEN`, `GITGATE_BRANCH_PREFIX`,
`GITGATE_ENABLE_PR`, `GITGATE_PR_BASES`). If `GITGATE_ALLOW` is set, it acts as the last, catch-all rule.

Once any `[[client]]` exists, every request must authenticate. Put the name and key into the remote URL, git sends
them as Basic auth:

```bash
git remote set-url origin http://alice:<KEY_ALICE>@gitgate:8080/myorg/app-web.git
```

The client name appears in every log line (`[alice] PUSH myorg/app-web refs/heads/alice/fix`), so you can see who did what.
Without any `[[client]]`, access is limited by the network only, as before.

A GitHub token is tied to one owner (a user or an organization). Use one `token_env` per owner, or use a
GitHub App (next section).

## GitHub App instead of a personal token (optional)

A personal access token is long-lived and has to be rotated by hand. A [GitHub App](https://docs.github.com/en/apps)
avoids that: gitgate signs a short-lived JWT with the App's private key and asks GitHub for an installation token
that is valid for one hour, limited to **one repository** and only the permissions the rule needs
(`contents: read` for a `readonly` rule, otherwise `contents: write`, plus `pull_requests: write` when `enable_pr`
is on). Tokens are cached and renewed automatically. If one leaks, it expires within the hour.

Setup:

1. GitHub, Settings, Developer settings, GitHub Apps, New GitHub App. Permissions: Repository, Contents read and write,
   Pull requests read and write. Leave Workflows off. Webhook off.
2. Install the App on the repositories (or the whole account) it may touch.
3. Note the App ID and generate a private key (a `.pem` file).
4. Reference the key and use the App in a rule:

```toml
[[app]]
name = "bot"
app_id = 123456
private_key_env = "APP_KEY"        # environment variable holding the PEM text
# private_key_file = "/run/secrets/app.pem"   # or a file instead
# installation_id = 7890123        # optional; found automatically per repository if omitted

[[rule]]
repos = ["myorg/*"]
app = "bot"                        # instead of token_env
enable_pr = true
```

Apps and personal tokens can be mixed: each rule picks its own. The Docker image contains `openssl`, which gitgate
uses to sign the JWT (Python's standard library has no RSA). The private key is a new secret to protect,
as sensitive as a personal token. If GitHub or the App is unavailable, gitgate answers 502 and sends nothing upstream.

On Coolify only variables listed in the compose file reach the container. If your policy refers to
`TOKEN_MYORG` or `KEY_ALICE`, add `- TOKEN_MYORG=${TOKEN_MYORG}` etc. to the `environment:` list in
`coolify/make_compose.py` and regenerate the compose file.

## Using it with Hermes

Hermes names the branches it creates for worktrees
`hermes/<id>`. The default prefix is `agent/`, so a Hermes push would be rejected. Tell gitgate to accept
Hermes' prefix instead:

```
GITGATE_BRANCH_PREFIX=hermes/
```

Then point the workspace repository at the proxy (`git remote set-url origin http://gitgate:8080/<owner>/<repo>.git`)
and make sure the Hermes containers share a Docker network with gitgate and have no GitHub credentials of their own.
The same approach works for any other agent: set the prefix to whatever it uses for its branches.

## Tests

```bash
cd proxy && python3 -I -m unittest   # unit tests and end-to-end tests with a fake upstream
python3 proxy/e2e_realgit.py         # real git against a fake GitHub
```

## Limitations

- Force-pushes inside allowed branches are not blocked: the agent may rewrite its own branches.
- Client keys travel as HTTP Basic auth, so run gitgate on a private network or behind TLS.
