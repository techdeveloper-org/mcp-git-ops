# mcp-git-ops

A FastMCP server providing **Git Ops** capabilities for Claude Code workflows.

---

## Overview

Structured Git operations via GitPython (no subprocess shell injection risk). Manages branches, commits, push/pull, stash, log, diff, and post-merge cleanup. Provides safe, exception-aware Git primitives for pipeline automation.

---

## Tools

| Tool | Description |
|------|-------------|
| `git_status` | Get working tree status (modified, untracked, staged files) |
| `git_branch_create` | Create a new branch (optionally from a base branch) |
| `git_branch_switch` | Switch to an existing branch |
| `git_branch_list` | List all local and remote branches |
| `git_branch_delete` | Delete a branch (local only, with safety check) |
| `git_commit` | Stage files and create a commit with message |
| `git_push` | Push current branch to remote (with upstream tracking) |
| `git_pull` | Pull latest changes from remote |
| `git_diff` | Show diff for staged, unstaged, or between branches |
| `git_stash` | Stash or pop working directory changes |
| `git_log` | Get commit log with author, date, message (configurable depth) |
| `git_fetch` | Fetch from remote without merging |
| `git_post_merge_cleanup` | Delete merged branches and prune remote tracking refs |
| `git_get_origin_url` | Get the remote origin URL for the repository |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/techdeveloper-org/mcp-git-ops.git
cd mcp-git-ops
```

### 2. Install dependencies

```bash
pip install mcp fastmcp gitpython
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

---

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `GIT_REPO_PATH` | Path to git repository root (default: CWD) |
| `GIT_DEFAULT_BRANCH` | Default branch name (default: main) |

---

## Usage in Claude Code

Add to your `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "git-ops": {
      "command": "python",
      "args": [
        "/path/to/mcp-git-ops/server.py"
      ],
      "env": {}
    }
  }
}
```

---

## Benefits

- GitPython library calls instead of subprocess — no shell injection risk
- Structured exceptions instead of raw stderr parsing
- Post-merge cleanup automates branch housekeeping in CI workflows
- Safe branch deletion checks for unmerged commits before deleting

---

## Requirements

- Python 3.8+
- `mcp fastmcp gitpython`
- See `requirements.txt` for pinned versions

---

## Project Context

This MCP server is part of the **Claude Workflow Engine** ecosystem — a LangGraph-based
orchestration pipeline for automating Claude Code development workflows.

Related repos:
- [`claude-workflow-engine`](https://github.com/techdeveloper-org/claude-workflow-engine) — Main pipeline
- [`mcp-base`](https://github.com/techdeveloper-org/mcp-base) — Shared base utilities used by all MCP servers

---

## License

Private — techdeveloper-org
