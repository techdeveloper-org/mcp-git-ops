# mcp-git-ops

![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue)
![License MIT](https://img.shields.io/badge/License-MIT-green)

A FastMCP server that exposes structured Git operations to Claude Code via the Model Context Protocol (stdio JSON-RPC). Built on GitPython — no subprocess shell calls, no injection surface.

---

## Overview

`mcp-git-ops` provides 14 Git tools covering the full branch lifecycle: create, switch, list, delete; commit and push; pull and fetch; diff and stash; commit log; and an orchestrated post-merge cleanup routine. It is registered in Claude Code's `settings.json` as the `git-ops` MCP server and is used by the Claude Workflow Engine in Step 9 (branch creation after issue is opened) and Step 11 (post-merge cleanup after PR is merged).

All operations use the GitPython library rather than subprocess wrappers. This eliminates shell-injection risk, gives structured exception types instead of raw stderr text, and returns typed JSON objects that the pipeline state machine can act on directly.

---

## Features

- Safe branch creation with automatic stash-save and stash-restore around the checkout
- Fetch-before-branch: always creates from `FETCH_HEAD` so the branch is up to date at creation time
- Commit with selective staging — pass a comma-separated file list or stage everything
- Push with optional upstream tracking and force flag
- Diff in three modes: unstaged, staged (`--cached`), or between two refs
- Stash push / pop / list with graceful handling of empty stash errors
- Structured commit log with hash, author, date, and first-line message
- Fetch with optional prune to remove stale remote-tracking refs
- Post-merge cleanup: checkout main, pull, delete merged branch (soft then force fallback), prune — four steps in one call
- Origin URL lookup with GitHub detection flag
- All tools wrapped with `@mcp_tool_handler` from `mcp-base`: uniform error envelopes, structured logging, input sanitization

---

## Tool Reference

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `git_status` | Get working tree status: modified, staged, and untracked files | `repo_path` (default `.`) |
| `git_branch_create` | Create a new branch from a base branch with stash safety and upstream push | `name` (required), `from_branch` (default `main`), `repo_path` |
| `git_branch_switch` | Check out an existing local branch | `name` (required), `repo_path` |
| `git_branch_list` | List current branch, all local branches, and all remote refs | `repo_path` |
| `git_branch_delete` | Delete a local branch; `force=True` skips merge-safety check | `name` (required), `force` (default `false`), `repo_path` |
| `git_commit` | Stage files and create a commit | `message` (required), `files` (comma-separated paths or empty for all), `repo_path` |
| `git_push` | Push a branch to remote origin | `branch` (default: current), `set_upstream` (default `false`), `force` (default `false`), `repo_path` |
| `git_pull` | Pull latest changes from remote origin | `branch` (default: current), `repo_path` |
| `git_diff` | Show diff summary in three modes | `staged` (default `false`), `from_ref` (compare to ref), `repo_path` |
| `git_stash` | Push, pop, or list stash entries | `action` (`push`/`pop`/`list`, default `push`), `message` (for push), `repo_path` |
| `git_log` | Get recent commits with hash, message, author, and ISO date | `count` (default `10`), `repo_path` |
| `git_fetch` | Fetch from a remote; optionally prune stale refs | `remote` (default `origin`), `branch` (specific branch or all), `prune` (default `false`), `repo_path` |
| `git_post_merge_cleanup` | Orchestrated 4-step cleanup after a PR merge | `merged_branch` (required), `main_branch` (default `main`), `repo_path` |
| `git_get_origin_url` | Return the remote origin URL and a GitHub detection flag | `repo_path` |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/techdeveloper-org/mcp-git-ops.git
cd mcp-git-ops
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

The server depends on three packages: `mcp>=1.0.0`, `fastmcp>=0.1.0`, and `gitpython`. No other runtime dependencies are required.

### 3. Register in Claude Code

Add the server to your `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "git-ops": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-git-ops/server.py"],
      "env": {}
    }
  }
}
```

Replace `/absolute/path/to/mcp-git-ops/server.py` with the actual path on your machine. The server communicates over stdio; no port configuration is needed.

---

## Configuration

This server requires no mandatory environment variables. All tools accept `repo_path` as a per-call parameter (default: `.`, meaning the current working directory at the time Claude Code invokes the tool).

Optional configuration you may add to the `env` block in `settings.json`:

| Variable | Default | Description |
|----------|---------|-------------|
| `GIT_DEFAULT_BRANCH` | — | **NOT IMPLEMENTED — read by nothing.** This row previously claimed it would *"override the default base branch name used when `from_branch` is not specified"*. No code consults it: `git_branch_create` declares `from_branch: str = "main"` as a hard-coded literal. On a repository whose base branch is `master`, setting this changes nothing and branch creation still tries to fetch a `main` that does not exist. Pass `from_branch` explicitly. |
| `GIT_INSTALL_ROOT` | — | Optional. Consulted by `hook_shell_fix.py` (called at import time) when locating the git executable: PATH first, then this, then standard install locations. Useful on Windows when git is not on PATH. |

No tokens or credentials are required. The server uses the Git credential store already configured on the host machine (SSH keys, credential helpers, or HTTPS tokens stored by `git config`).

---

## Usage Examples

### Create a feature branch

Request (Claude Code sends this to the MCP server):

```json
{
  "tool": "git_branch_create",
  "arguments": {
    "name": "feature/PROJ-42-add-payment-endpoint",
    "from_branch": "main",
    "repo_path": "/home/user/projects/my-api"
  }
}
```

Response:

```json
{
  "branch": "feature/PROJ-42-add-payment-endpoint",
  "from": "main",
  "stash_restored": false
}
```

---

### Stage specific files and commit

Request:

```json
{
  "tool": "git_commit",
  "arguments": {
    "message": "feat(payments): add Stripe webhook handler\n\nCloses #42",
    "files": "src/payments/webhook.py,tests/test_webhook.py",
    "repo_path": "/home/user/projects/my-api"
  }
}
```

Response:

```json
{
  "commit_hash": "a3f91bc",
  "message": "feat(payments): add Stripe webhook handler\n\nCloses #42",
  "author": "Piyush Makhija <piyush@example.com>"
}
```

---

### Check diff between branches before opening a PR

Request:

```json
{
  "tool": "git_diff",
  "arguments": {
    "from_ref": "main",
    "repo_path": "/home/user/projects/my-api"
  }
}
```

Response:

```json
{
  "diff_summary": " src/payments/webhook.py | 87 ++++++++++++++++++++++++++\n tests/test_webhook.py   | 44 +++++++++++++\n 2 files changed, 131 insertions(+)",
  "staged": false,
  "from_ref": "main"
}
```

---

### Post-merge cleanup after PR is merged

Request:

```json
{
  "tool": "git_post_merge_cleanup",
  "arguments": {
    "merged_branch": "feature/PROJ-42-add-payment-endpoint",
    "main_branch": "main",
    "repo_path": "/home/user/projects/my-api"
  }
}
```

Response:

```json
{
  "cleaned_branch": "feature/PROJ-42-add-payment-endpoint",
  "current_branch": "main",
  "branch_deleted": true,
  "message": "Cleaned up feature/PROJ-42-add-payment-endpoint, now on main (synced)"
}
```

---

## Optional: use inside an orchestration pipeline

> **Standalone.** This server has no runtime dependency on any other project. It speaks MCP over stdio and works with any MCP client.

One such client is the [claude-workflow-engine](https://github.com/techdeveloper-org/claude-workflow-engine) LangGraph pipeline, which uses this server among 13 others. Nothing below is required to use the tools on their own. It is called at two specific pipeline steps:

**Step 9 — Branch Creation**

After Step 8 opens a GitHub Issue (and optionally a Jira issue), Step 9 calls `git_branch_create` with the branch name derived from the issue number or Jira key. The stash-safety workflow in `git_branch_create` ensures that any in-progress changes are preserved before the checkout, which is important when the pipeline runs against a working repository rather than a clean CI workspace.

**Step 11 — Post-Merge Cleanup**

After the PR is reviewed and merged in Step 11, `git_post_merge_cleanup` is called with the feature branch name. This single tool call handles the full four-step housekeeping sequence: switch to main, pull the merge commit, delete the local feature branch, and prune stale remote-tracking refs. This keeps the local repository clean between pipeline runs.

The server also provides `git_status`, `git_diff`, and `git_log` as diagnostic tools used in Step 10 (implementation) and Step 13 (documentation update) to verify the state of the repository before LLM-generated code is committed.

---

## Repository Structure

```
mcp-git-ops/
+-- server.py              # FastMCP server — 14 tool definitions
+-- requirements.txt       # Runtime dependencies (mcp, fastmcp, gitpython)
+-- input_validator.py     # Input sanitization (null-byte strip, length limits)
+-- mcp_errors.py          # Domain exception hierarchy
+-- rate_limiter.py        # Token-bucket rate limiting per client
+-- base/                  # Copy of mcp-base shared library
|   +-- decorators.py      # @mcp_tool_handler decorator
|   +-- clients.py         # GitRepoClient factory
|   +-- response.py        # MCPResponse builder
|   +-- store.py           # AtomicJsonStore for persistent state
```

---

## Shared Base Package

The `base/` directory is a copy of the [mcp-base](https://github.com/techdeveloper-org/mcp-base) shared library that all 13 MCP servers in this ecosystem include. The key component used by this server is:

- `@mcp_tool_handler` decorator: wraps every tool handler to catch exceptions, format uniform error envelopes, and ensure structured JSON responses even on failure.
- `GitRepoClient.for_path(repo_path)`: factory method that resolves the path, validates it is a Git repository, and returns a `git.Repo` instance.

Do not modify the `base/` directory in this repo. Submit changes upstream to [mcp-base](https://github.com/techdeveloper-org/mcp-base) and pull them in.

---

## Contributing

Contributions are welcome. Please follow these guidelines:

1. Fork the repository and create a feature branch from `main`.
2. Write or update tests for any behavior changes in `server.py`.
3. Keep tool signatures backward compatible — existing parameter names must not be renamed.
4. New tools must include a docstring with an `Args:` section listing all parameters.
5. Run `python -m pytest tests/` before opening a pull request.
6. Keep the tool count in the module docstring at the top of `server.py` up to date.

Pull request titles should follow the format: `feat(git-ops): <short description>` or `fix(git-ops): <short description>`.

---

## Related Repositories

| Repository | Purpose |
|------------|---------|
| [claude-workflow-engine](https://github.com/techdeveloper-org/claude-workflow-engine) | One consumer of this server — a LangGraph orchestration pipeline. Not required. |
| [mcp-base](https://github.com/techdeveloper-org/mcp-base) | Shared base library (MCPResponse, @mcp_tool_handler, AtomicJsonStore) |
| [mcp-github-api](https://github.com/techdeveloper-org/mcp-github-api) | GitHub PR, issue, merge, and label operations |
| [mcp-session-mgr](https://github.com/techdeveloper-org/mcp-session-mgr) | Session lifecycle and context persistence |

---

## License

MIT License. Copyright (c) 2024 techdeveloper-org.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
