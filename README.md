# gitgate

A small git proxy that lets an AI agent work with GitHub repositories without holding a GitHub token, and limits
what it can push.

## Why

An agent that pushes code needs a token with write access. With that token it can push to `main`, delete branches,
move tags or rewrite history, whether by mistake or because it was prompted to. GitHub's usual protection,
branch protection rules, is not available for private repositories on GitHub Free. A fine-grained token can be limited
to particular repositories, but not to particular branches.

gitgate sits between the agent and GitHub. The agent talks to the proxy with no credentials. The proxy holds the
token, forwards fetches, and lets a push through only if every ref it updates is a branch under an allowed prefix
(default `agent/`). Everything else is rejected, and git shows the agent why:

```
 ! [remote rejected] main -> main (gitgate: only refs/heads/agent/* can be pushed)
```

A human then reviews the agent's branch and merges it. gitgate can also let the agent open the pull request itself
(see [Pull requests](#pull-requests-optional)).

## How it works

```
agent --(no credentials)--> gitgate --(token)--> github.com
        http://gitgate:8080/<owner>/<repo>.git
```

- **fetch / clone**: allowed for repositories matching `GITGATE_ALLOW` (e.g. `myorg/*`).
- **push**: allowed only if every updated ref is `refs/heads/<prefix>...`. Pushes to `main`, tags, other branches,
  and pushes with a push certificate are rejected. Nothing is sent to GitHub.
- **anything else** (the rest of the GitHub API, other URLs): rejected.

The agent keeps full local git (commit, rebase, amend, worktrees). Only what reaches GitHub is restricted.
gitgate is a single Python file that uses only the standard library.

## What it protects, and what it does not

Protected:

- `main`, tags and every branch outside the prefix cannot be changed through the proxy.
- The agent never sees the GitHub token, so it cannot reach GitHub with it some other way.

Not protected:

- **The agent's own branches.** Inside the prefix the agent may force-push and delete branches.
- **Reading.** The agent can fetch every repository that `GITGATE_ALLOW` (or a policy rule) allows.
- **CI.** A push to an `agent/` branch triggers your existing GitHub Actions workflows that run `on: push`, and
  those workflows may use repository secrets. Use a token **without** the Workflows permission. GitHub then refuses
  any push that changes `.github/workflows/`, so the agent cannot add or edit a workflow.
- **Other credentials.** gitgate only helps if the agent has no other way to reach GitHub with write access: no
  token in its environment, no SSH key, no `gh` login.
- **The network.** Without named clients (see [the policy](#several-repositories-tokens-and-agents-optional)),
  anyone who can reach the proxy can use it. Client keys travel as HTTP Basic auth. Run gitgate on a private
  network, or behind TLS.

## Quick start

1. Create a GitHub fine-grained personal access token: Repository access limited to the repositories the agent
   needs, permissions Contents read and write (plus Pull requests read and write if you enable PRs), and
   **no** Workflows permission.
2. Run the proxy:

   ```bash
   docker build -t gitgate proxy
   docker run -d -p 127.0.0.1:8080:8080 \
     -e GITGATE_TOKEN=<github-token> \
     -e GITGATE_ALLOW='myorg/*' \
     gitgate
   ```

   `127.0.0.1` keeps the port private to this machine, because the proxy has no authentication by default.
3. Point the agent's repository at the proxy:

   ```bash
   git remote set-url origin http://localhost:8080/myorg/myrepo.git
   git push origin HEAD:agent/my-change   # works
   git push origin HEAD:main              # rejected
   ```

## Configuration

Two levels, and most setups need only the first:

- **Environment variables**: one token, one set of repositories, one branch prefix. Enough for one agent and
  one GitHub owner.
- **TOML policy** (optional): several tokens or owners, different prefixes per repository, read-only repositories,
  named agents with their own keys, GitHub Apps. See [below](#several-repositories-tokens-and-agents-optional).

| Variable | Default | Meaning |
|---|---|---|
| `GITGATE_TOKEN` or `GITGATE_TOKEN_FILE` | required | GitHub token, or a file that contains it |
| `GITGATE_ALLOW` | empty: deny all | Comma-separated `owner/repo` globs, e.g. `myorg/*,me/dotfiles` |
| `GITGATE_BRANCH_PREFIX` | `agent/` | Branches the agent may push must start with this |
| `GITGATE_ENABLE_PR` | off | `1` enables the pull request endpoint |
| `GITGATE_PR_BASES` | `main` | Comma-separated branches a pull request may target |
| `GITGATE_POLICY` or `GITGATE_POLICY_FILE` | none | TOML policy: the text itself, or a path to it |
| `GITGATE_PORT` | `8080` | Listen port |

At startup gitgate logs every rule it loaded. Every request is logged too, never with tokens or headers.

## Deploy on Coolify

[coolify/docker-compose.yaml](coolify/docker-compose.yaml) is self-contained: the proxy code is embedded, so no
git repository is needed on the Coolify side.

1. In Coolify: New Resource, Docker Compose (empty), paste the file.
2. Set the environment variables: `GITGATE_TOKEN` (as a secret), `GITGATE_ALLOW`, and `GITGATE_NETWORK` (the name of
   the existing Docker network your agent containers are on). Optionally `GITGATE_BRANCH_PREFIX`,
   `GITGATE_ENABLE_PR=1`, `GITGATE_PR_BASES`, and `GITGATE_POLICY` (the TOML text, a multi-line value).
3. Do not assign a domain. The proxy publishes no ports: from the agent's containers it is reachable as
   `http://gitgate:8080`.

Only the variables listed in the compose file reach the container. If you change anything in `proxy/`, or your
policy refers to extra variables such as `TOKEN_MYORG` or `KEY_ALICE`, add them to `ENVIRONMENT` in
[coolify/make_compose.py](coolify/make_compose.py) and regenerate the file:

```bash
python3 coolify/make_compose.py > coolify/docker-compose.yaml
```

## Using it with Hermes

The Hermes agent names the branches it creates for its worktrees `hermes/<id>`. Accept that prefix instead of
the default `agent/`:

```
GITGATE_BRANCH_PREFIX=hermes/
```

Then point the workspace repository at the proxy (`git remote set-url origin http://gitgate:8080/<owner>/<repo>.git`).
Make sure the Hermes containers share a Docker network with gitgate and have no GitHub credentials of their own.
The same works for any other agent: set the prefix to whatever it uses for its branches.

## Pull requests (optional)

With `GITGATE_ENABLE_PR=1` the agent can open a pull request, still without holding a token:

```bash
curl -s -X POST http://gitgate:8080/myorg/myrepo.git/pulls \
  -H 'Content-Type: application/json' \
  -d '{"head": "agent/my-feature", "title": "Add my feature", "body": "Details...", "draft": true}'
# {"number": 12, "html_url": "https://github.com/myorg/myrepo/pull/12"}
```

- `head` must be a branch the agent may push (no `owner:branch` forks).
- `base` must be listed in `GITGATE_PR_BASES`. If it is left out, the first entry is used.
- `title` is required, up to 256 characters. `body` is optional, up to 64 KB.

Only creating a pull request is exposed: no merge, no comments, no other GitHub API. A human still reviews and
merges.

## Several repositories, tokens and agents (optional)

A TOML policy adds rules on top of the environment variables. Without one, nothing changes.

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

| Rule field | Meaning |
|---|---|
| `repos` | Required. `owner/repo` globs |
| `token_env` | Environment variable with the token for this rule |
| `app` | Use a GitHub App instead of a token (see below) |
| `branch_prefix` | Branches that may be pushed |
| `readonly` | `true`: fetch only |
| `enable_pr`, `pr_bases` | Pull requests, as above |
| `clients` | Only these named clients may use the rule |

Fields you leave out fall back to the environment variables (`GITGATE_TOKEN`, `GITGATE_BRANCH_PREFIX`,
`GITGATE_ENABLE_PR`, `GITGATE_PR_BASES`). If `GITGATE_ALLOW` is set, it acts as the last, catch-all rule.

**Named clients.** Without any `[[client]]`, access is limited by the network only. Once a `[[client]]` exists,
every request must authenticate. Put the name and key into the remote URL, and git sends them as Basic auth:

```bash
git remote set-url origin http://alice:<KEY_ALICE>@gitgate:8080/myorg/app-web.git
```

The client name then appears in every log line, so you can see who did what:
`[alice] PUSH myorg/app-web refs/heads/alice/fix`.

**Several owners.** A GitHub token belongs to one owner (a user or an organization). Use one `token_env` per owner,
or a GitHub App.

## GitHub App instead of a personal token (optional)

A personal access token is long-lived and has to be rotated by hand. With a [GitHub App](https://docs.github.com/en/apps)
gitgate asks GitHub for a token that is valid for one hour, limited to **one repository** and to only the
permissions the rule needs: `contents: read` for a `readonly` rule, otherwise `contents: write`, plus
`pull_requests: write` when `enable_pr` is on. Tokens are cached and renewed automatically, and a leaked one
expires within the hour.

1. GitHub, Settings, Developer settings, GitHub Apps, New GitHub App. Repository permissions: Contents read and
   write, Pull requests read and write. Leave Workflows off and the webhook off.
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

Apps and personal tokens can be mixed: each rule picks its own. The private key is as sensitive as a personal
token. gitgate signs the App's JWT with `openssl` (included in the Docker image), because Python's standard
library has no RSA. If GitHub or the App is unavailable, gitgate answers 502 and sends nothing upstream.

## Tests

```bash
cd proxy && python3 -I -m unittest
```

These are unit tests, end-to-end tests against a fake GitHub, and real `git push` runs through the proxy (when
`git` is installed). They need Python 3.11+, `openssl` for the GitHub App tests, and a recent git for the real-git
tests: git 2.38 is known to break chunked pushes larger than `http.postBuffer`, while 2.50 passes.
