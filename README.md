# gitgate

**A git proxy that gives an AI agent push access to GitHub without giving it the token.**
The agent can push only to its own branches (`agent/*`). `main`, tags and everything else are rejected.

```
agent ──(no credentials)──▶ gitgate ──(token)──▶ github.com
         http://gitgate:8080/<owner>/<repo>.git
```

**Why:** a token with write access lets an agent push to `main`, delete branches and rewrite history. Private
repositories on GitHub Free have no branch protection, and a fine-grained token cannot be limited to branches.

## How it behaves

| Request | Result |
|---|---|
| fetch / clone of a repository in `GITGATE_ALLOW` | forwarded with the token |
| push where every ref is `refs/heads/agent/...` | forwarded |
| push to `main`, a tag, another branch, or with a push certificate | rejected, nothing reaches GitHub |
| any other URL or GitHub API call | rejected |

A rejected push is shown by git with the reason:

```
 ! [remote rejected] main -> main (gitgate: only refs/heads/agent/* can be pushed)
```

The agent keeps full local git (rebase, amend, worktrees). gitgate is one Python file with no dependencies.

## Quick start

```bash
docker build -t gitgate proxy
docker run -d -p 127.0.0.1:8080:8080 -e GITGATE_TOKEN=<token> -e GITGATE_ALLOW='myorg/*' gitgate

git remote set-url origin http://localhost:8080/myorg/myrepo.git
git push origin HEAD:agent/fix   # ok
git push origin HEAD:main        # rejected
```

**Token:** fine-grained, only the needed repositories, Contents read/write (plus Pull requests read/write for PRs),
**no Workflows**. Without Workflows, GitHub refuses pushes that change `.github/workflows/`.

## Configuration

| Variable | Default | |
|---|---|---|
| `GITGATE_TOKEN` / `GITGATE_TOKEN_FILE` | required | GitHub token, or a file with it |
| `GITGATE_ALLOW` | deny all | `owner/repo` globs, comma-separated: `myorg/*,me/dotfiles` |
| `GITGATE_BRANCH_PREFIX` | `agent/` | Branches the agent may push |
| `GITGATE_ENABLE_PR` | off | `1` enables [pull requests](#pull-requests) |
| `GITGATE_PR_BASES` | `main` | Branches a PR may target |
| `GITGATE_POLICY` / `GITGATE_POLICY_FILE` | none | [TOML policy](#policy-several-tokens-agents-and-rules): text or path |
| `GITGATE_PORT` | `8080` | Listen port |

Environment variables are enough for one agent and one GitHub owner. The policy is for the rest.

## Limits

- Inside its prefix the agent can force-push and delete branches.
- The agent can read every allowed repository.
- Pushes to `agent/*` trigger existing `on: push` workflows, which may use repository secrets.
- It only helps if the agent has no other GitHub credentials (token in env, SSH key, `gh` login).
- Without [named clients](#policy-several-tokens-agents-and-rules), anyone who can reach the proxy can use it.
  Keys travel as Basic auth: keep it on a private network or behind TLS.

## Deploy on Coolify

[coolify/docker-compose.yaml](coolify/docker-compose.yaml) has the code embedded, so you can paste it as is.

1. New Resource, Docker Compose (empty), paste the file. Do not assign a domain.
2. Set `GITGATE_TOKEN` (secret), `GITGATE_ALLOW` and `GITGATE_NETWORK` (the existing network of your agent
   containers), plus any optional variables.
3. The agents reach the proxy as `http://gitgate:8080`. No ports are published.

After changes in `proxy/`, or to pass extra variables (e.g. `TOKEN_MYORG`, add them to `ENVIRONMENT`
in [make_compose.py](coolify/make_compose.py)), regenerate the file:
`python3 coolify/make_compose.py > coolify/docker-compose.yaml`.

**Hermes** names its worktree branches `hermes/<id>`, so set `GITGATE_BRANCH_PREFIX=hermes/`. Use the same approach
for any other agent's prefix.

## Pull requests

With `GITGATE_ENABLE_PR=1` the agent can open a PR, still without a token. Only creation is exposed, so a human
merges.

```bash
curl -s http://gitgate:8080/myorg/myrepo.git/pulls -H 'Content-Type: application/json' \
  -d '{"head": "agent/fix", "title": "Fix the parser", "body": "Details", "draft": true}'
# {"number": 12, "html_url": "https://github.com/myorg/myrepo/pull/12"}
```

`head` must be a pushable branch. `base` defaults to the first of `GITGATE_PR_BASES`. `title` is required
(max 256 characters), `body` can be up to 64 KB.

## Policy: several tokens, agents and rules

<details>
<summary>TOML rules per repository, named agents with keys, read-only repositories</summary>

```toml
[[client]]                      # once a client exists, every request must authenticate
name = "alice"
key_env = "KEY_ALICE"           # variable holding her key

[[rule]]                        # rules are checked in order, the first match wins
repos = ["myorg/docs"]
readonly = true

[[rule]]
repos = ["myorg/app-*"]
clients = ["alice"]
branch_prefix = "alice/"
token_env = "TOKEN_MYORG"
enable_pr = true
pr_bases = ["main", "develop"]
```

| Rule field | |
|---|---|
| `repos` | required, `owner/repo` globs |
| `token_env` or `app` | token variable, or a [GitHub App](#github-app-instead-of-a-token) |
| `branch_prefix`, `readonly`, `enable_pr`, `pr_bases` | as the variables above |
| `clients` | only these clients may use the rule |

Fields you leave out come from the variables. `GITGATE_ALLOW`, if set, is the last catch-all rule. A token
belongs to one owner, so use one `token_env` per user or organization.

Clients authenticate through the remote URL, and their name appears in the logs:

```bash
git remote set-url origin http://alice:<key>@gitgate:8080/myorg/app-web.git
# gitgate: [alice] PUSH myorg/app-web refs/heads/alice/fix
```

</details>

## GitHub App instead of a token

<details>
<summary>Tokens that last one hour, one per repository, renewed automatically</summary>

For each repository gitgate requests an installation token with only what the rule needs: `contents: read`
for `readonly`, otherwise `contents: write`, plus `pull_requests: write` with `enable_pr`.

1. Create a GitHub App with Contents and Pull requests read/write. Leave Workflows and the webhook off.
2. Install it on the repositories, note the App ID, and generate a private key.
3. Add it to the policy:

```toml
[[app]]
name = "bot"
app_id = 123456
private_key_env = "APP_KEY"     # or private_key_file = "/run/secrets/app.pem"
# installation_id = 7890123     # optional, looked up per repository

[[rule]]
repos = ["myorg/*"]
app = "bot"
```

The JWT is signed with `openssl`, which is included in the image. If GitHub is unavailable, gitgate answers 502.

</details>

## Tests

```bash
cd proxy && python3 -I -m unittest
```

The suite runs unit tests, end-to-end tests against a fake GitHub, and real `git push` runs through the proxy.
It needs Python 3.11+, `openssl`, and a recent git (2.38 breaks large chunked pushes, 2.50 works).
