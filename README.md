# gitgate

A tiny git proxy (Python standard library only) that sits between an AI agent and GitHub.
The agent never sees the GitHub token; the proxy holds it and decides what may be pushed.

- **fetch / clone** works for repositories matching `GITGATE_ALLOW` (e.g. `myorg/*`).
- **push** works only if every updated ref matches `refs/heads/hermes/*`.
- `main`, tags, other branch prefixes and push certificates are rejected, and git prints the reason:
  `! [remote rejected] ... (gitgate: only refs/heads/hermes/* can be pushed)`.

Useful on GitHub Free, where private repositories have no branch protection.

## How it works

```
agent --(no credentials)--> gitgate --(token)--> github.com
        http://gitgate:8080/<owner>/<repo>.git
```

The agent keeps full local git (rebase, amend, worktrees, ...). Only the network push is restricted.
The proxy does not open pull requests.

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

Use a fine-grained token with Contents and Pull requests read/write, and without Workflows.

## Configuration

| Variable | Meaning |
|---|---|
| `GITGATE_TOKEN` or `GITGATE_TOKEN_FILE` | GitHub token (required) |
| `GITGATE_ALLOW` | Comma-separated `owner/repo` globs. Empty denies everything. |
| `GITGATE_REF_REGEX` | Refs that may be pushed. Default: `^refs/heads/hermes/[A-Za-z0-9._/-]+$` |
| `GITGATE_REF_HINT` | Text shown in rejections. Default: `refs/heads/hermes/*` |
| `GITGATE_PORT` | Listen port. Default: `8080` |

## Deploy on Coolify

1. `python3 coolify/make_compose.py > coolify/docker-compose.yaml` (after any change to `gitgate.py`).
2. In Coolify: New Resource, Docker Compose (empty), paste the file.
3. Set `GITGATE_TOKEN` (secret), `GITGATE_ALLOW`, and `GITGATE_NETWORK` (the existing Docker network
   your agent containers are on). Do not assign a domain.
4. From the agent's containers the proxy is reachable as `http://gitgate:8080`.

## Tests

```bash
cd proxy && python3 -I -m unittest   # unit tests and end-to-end tests with a fake upstream
python3 proxy/e2e_realgit.py         # real git against a fake GitHub
```

## Limitations

- Force-pushes inside allowed branches are not blocked: the agent may rewrite its own branches.
- One token for all repositories in `GITGATE_ALLOW`.
