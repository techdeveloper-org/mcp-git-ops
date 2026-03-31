# mcp-git-ops — Claude Project Context

**Type:** FastMCP Server
**Transport:** stdio
**Python:** 3.8+

---

## What This Server Does

Structured Git operations via GitPython (no subprocess shell injection risk). Manages branches, commits, push/pull, stash, log, diff, and post-merge cleanup. Provides safe, exception-aware Git primitives for pipeline automation.

---

## Entry Point

```
server.py
```

Run via `python server.py` — communicates over stdio using the MCP protocol.

---

## Available Tools

- `git_status` — Get working tree status (modified, untracked, staged files)
- `git_branch_create` — Create a new branch (optionally from a base branch)
- `git_branch_switch` — Switch to an existing branch
- `git_branch_list` — List all local and remote branches
- `git_branch_delete` — Delete a branch (local only, with safety check)
- `git_commit` — Stage files and create a commit with message
- `git_push` — Push current branch to remote (with upstream tracking)
- `git_pull` — Pull latest changes from remote
- `git_diff` — Show diff for staged, unstaged, or between branches
- `git_stash` — Stash or pop working directory changes
- `git_log` — Get commit log with author, date, message (configurable depth)
- `git_fetch` — Fetch from remote without merging
- `git_post_merge_cleanup` — Delete merged branches and prune remote tracking refs
- `git_get_origin_url` — Get the remote origin URL for the repository

---

## Shared Utilities (in this repo)

- `base/` — Shared MCP infrastructure package (response builder, decorators, persistence, clients)
- `mcp_errors.py` — Structured error response helpers
- `input_validator.py` — Null-byte strip, length limits, prompt injection detection
- `rate_limiter.py` — Token bucket rate limiter (enable via ENABLE_RATE_LIMITING=1)

---

## Environment Variables

- `GIT_REPO_PATH` — Path to git repository root (default: CWD)
- `GIT_DEFAULT_BRANCH` — Default branch name (default: main)

---

## Development

### Running locally

```bash
# Install deps
pip install -r requirements.txt

# Run the MCP server (stdio mode)
python server.py
```

### Testing a tool call manually

```python
import subprocess, json

proc = subprocess.Popen(
    ["python", "server.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
)
# Send MCP initialize + tool call via stdin
```

### File structure

```
mcp-git-ops/
+-- server.py          # Main FastMCP server (entry point)
+-- base/              # Shared base package (response, decorators, persistence, clients)
+-- mcp_errors.py      # Error helpers
+-- input_validator.py # Input validation
+-- rate_limiter.py    # Rate limiting
+-- requirements.txt
+-- .gitignore
+-- README.md
+-- CLAUDE.md
```

---

## Key Rules

1. Do NOT edit `base/` directly — it is a copy from `mcp-base` repo
2. To update shared utilities, edit in `mcp-base` and re-copy
3. Keep `server.py` as the single entry point
4. All tool handlers must use `@mcp_tool_handler` decorator for consistent error handling
5. All responses must use `success()` / `error()` / `MCPResponse` builder from `base.response`

---

**Last Updated:** 2026-03-31
