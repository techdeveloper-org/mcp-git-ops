"""
Git MCP Server - FastMCP-based Git operations for Claude Code.

Replaces 13+ subprocess calls in git_operations.py with GitPython library.
Backend: GitPython (already in requirements.txt)
Transport: stdio (Claude Code communicates via stdin/stdout)

Tools (14):
  git_status, git_branch_create, git_branch_switch, git_branch_list,
  git_branch_delete, git_commit, git_push, git_pull, git_diff,
  git_stash, git_log, git_fetch, git_post_merge_cleanup, git_get_origin_url
"""

import sys
from pathlib import Path
from typing import Optional

# Ensure src/mcp/ is in path for base package imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both names are probed so this server runs under either major version; the
# API used below (tool decorator, run(transport=...)) is identical in both.
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer

try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover - annotations unsupported on this mcp
    ToolAnnotations = None

from git import GitCommandError
from git.exc import BadName

import hook_shell_fix

from base.decorators import mcp_tool_handler
from base.clients import GitRepoClient

# Must run before any commit: GitPython spawns Windows hooks as a bare
# "bash.exe", which CreateProcess resolves from System32 (the WSL stub) before
# it ever looks at PATH. See hook_shell_fix for the full explanation.
hook_shell_fix.apply()

mcp = MCPServer("git-ops", instructions="Git operations via GitPython (no subprocess)")


def _tool(read_only=False, destructive=True, idempotent=False, open_world=True):
    """Register a tool with explicit MCP ToolAnnotations.

    The MCP specification's per-hint defaults are readOnlyHint=false,
    destructiveHint=true, idempotentHint=false and openWorldHint=true -- every
    default points at the more dangerous value, so an unannotated tool is
    indistinguishable from an explicit worst-case declaration. Every tool on
    this server declares its four hints explicitly so a host's auto-approval and
    automatic-retry decisions rest on a stated property rather than an omission.

    Args:
        read_only: True when the tool has no side effects at all.
        destructive: True when the tool's effect is irreversible.
        idempotent: True only when repeating the call with identical arguments
            leaves the same cumulative effect as a single call.
        open_world: True when the tool reaches a remote (network) system.

    Returns:
        The decorator returned by the underlying MCP tool registration.
    """
    if ToolAnnotations is None:
        return mcp.tool()
    try:
        return mcp.tool(
            annotations=ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=destructive,
                idempotentHint=idempotent,
                openWorldHint=open_world,
            )
        )
    except TypeError:  # pragma: no cover - older mcp without annotations kwarg
        return mcp.tool()


def _safe_ref(value: str, field_name: str) -> str:
    """Validate a value that will be passed to git as a ref or refspec argument.

    GitPython invokes git through a subprocess argument list, so shell
    metacharacters are inert. What is not inert is a value that git itself
    parses as an option: a ``from_ref`` of ``--output=/etc/passwd`` makes
    ``git diff`` write to that path, and a push refspec of
    ``--receive-pack=<cmd>`` selects the program git runs on the remote. Both
    are argument injection, not shell injection, and neither is prevented by
    avoiding a shell. Rejecting a leading dash closes that class; embedded NUL
    and newline are rejected because git's own ref grammar forbids control
    characters in a ref name.

    Args:
        value: Caller-supplied ref, refspec, branch or remote name.
        field_name: Parameter name used in the error message.

    Returns:
        The validated value unchanged.

    Raises:
        ValueError: If the value is empty, begins with '-', or contains a NUL
            or newline character.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value.startswith("-"):
        raise ValueError(
            f"{field_name} must not begin with '-': git would parse "
            f"'{value}' as a command-line option rather than a ref"
        )
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{field_name} must not contain control characters")
    return value


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
@mcp_tool_handler
def git_status(repo_path: str = ".") -> dict:
    """Get repository status (modified, staged, untracked files)."""
    repo = GitRepoClient.for_path(repo_path)
    changed = [item.a_path for item in repo.index.diff(None)]
    try:
        staged = [item.a_path for item in repo.index.diff("HEAD")]
    except BadName:
        # Unborn HEAD: no commit exists yet (fresh `git init`, before the
        # first commit), so there is no tree to diff the index against.
        # Everything currently in the index counts as staged relative to
        # the implicit empty tree.
        staged = sorted({path for (path, _stage) in repo.index.entries.keys()})
    untracked = repo.untracked_files

    return {
        "branch": str(repo.active_branch),
        "is_dirty": repo.is_dirty(untracked_files=True),
        "modified": changed,
        "staged": staged,
        "untracked": untracked,
        "total_changes": len(changed) + len(staged) + len(untracked)
    }


@_tool(read_only=False, destructive=False, idempotent=False, open_world=True)
@mcp_tool_handler
def git_branch_create(name: str, from_branch: str = "main", repo_path: str = ".") -> dict:
    """Create a new branch from specified base with stash safety.

    Workflow: stash -> fetch -> create branch from FETCH_HEAD -> pop stash -> push

    A failed stash pop or a failed upstream push is reported in the result
    rather than discarded. Silently dropping a stash-pop failure is the worst
    case here: the caller believes its working tree was restored while the
    changes are still sitting in the stash.

    Args:
        name: Name of the branch to create.
        from_branch: Base branch to fetch and branch from.
        repo_path: Repository path.

    Returns:
        Dict with branch, from, stash_restored, pushed, and any
        stash_pop_error / push_error detail.
    """
    name = _safe_ref(name, "name")
    from_branch = _safe_ref(from_branch, "from_branch")
    repo = GitRepoClient.for_path(repo_path)
    origin = repo.remotes.origin

    had_stash = False
    if repo.is_dirty(untracked_files=True):
        repo.git.stash("push", "--include-untracked", "-m", f"auto-stash-before-{name}")
        had_stash = True

    try:
        origin.fetch(from_branch)
        base_ref = "FETCH_HEAD"
    except GitCommandError:
        base_ref = from_branch

    repo.git.checkout("-b", name, base_ref)

    stash_restored = False
    stash_pop_error = None
    if had_stash:
        try:
            repo.git.stash("pop")
            stash_restored = True
        except GitCommandError as exc:
            stash_pop_error = str(exc)[:300]

    pushed = False
    push_error = None
    try:
        origin.push(name, set_upstream=True)
        pushed = True
    except GitCommandError as exc:
        push_error = str(exc)[:300]

    result = {
        "branch": name,
        "from": from_branch,
        "had_stash": had_stash,
        "stash_restored": stash_restored,
        "pushed": pushed,
    }
    if stash_pop_error:
        result["stash_pop_error"] = stash_pop_error
        result["stash_pop_hint"] = (
            "Your changes are still in the stash. Resolve the conflict and run "
            "git_stash with action='pop'."
        )
    if push_error:
        result["push_error"] = push_error
    return result


@_tool(read_only=False, destructive=False, idempotent=True, open_world=False)
@mcp_tool_handler
def git_branch_switch(name: str, repo_path: str = ".") -> dict:
    """Switch to an existing branch."""
    name = _safe_ref(name, "name")
    repo = GitRepoClient.for_path(repo_path)
    repo.git.checkout(name)
    return {"branch": name}


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
@mcp_tool_handler
def git_branch_list(repo_path: str = ".") -> dict:
    """List all local and remote branches."""
    repo = GitRepoClient.for_path(repo_path)
    local = [str(b) for b in repo.branches]
    current = str(repo.active_branch)
    remote = []
    for remote_obj in repo.remotes:
        for ref in remote_obj.refs:
            remote.append(str(ref))

    return {
        "current": current,
        "local": local,
        "remote": remote
    }


@_tool(read_only=False, destructive=True, idempotent=False, open_world=False)
@mcp_tool_handler
def git_branch_delete(name: str, force: bool = False, repo_path: str = ".") -> dict:
    """Delete a local branch.

    With force=True this discards unmerged commits that exist nowhere else,
    which is why the tool is annotated destructive.

    Args:
        name: Branch to delete.
        force: Use -D instead of -d, deleting even when unmerged.
        repo_path: Repository path.
    """
    name = _safe_ref(name, "name")
    repo = GitRepoClient.for_path(repo_path)
    flag = "-D" if force else "-d"
    repo.git.branch(flag, name)
    return {"deleted": name, "force": force}


@_tool(read_only=False, destructive=False, idempotent=False, open_world=False)
@mcp_tool_handler
def git_commit(message: str, files: Optional[str] = None, repo_path: str = ".") -> dict:
    """Stage files and create a commit.

    Args:
        message: Commit message (can be multi-line)
        files: Comma-separated file paths to stage. If empty, stages all changes.
        repo_path: Repository path
    """
    repo = GitRepoClient.for_path(repo_path)
    staged_all_changes = not files

    # Stage files. Uses the `git add` porcelain command (repo.git.add), not
    # IndexFile.add() -- the low-level index API ignores .gitignore (it will
    # happily stage an untracked __pycache__/*.pyc that git add would skip)
    # and raises FileNotFoundError when a listed path no longer exists on
    # disk, so it cannot stage a deletion. `git add` handles both correctly.
    if files:
        file_list = [f.strip() for f in files.split(",") if f.strip()]
        repo.git.add(*file_list)
    else:
        repo.git.add("-A")

    # Check if there are staged changes. repo.index.diff("HEAD") raises
    # BadName when HEAD is unborn -- the very first commit in a repo, before
    # any commit exists yet (fresh `git init`) -- because there is no HEAD
    # tree to diff the index against. Fall back to checking whether the
    # index has any entries at all in that case.
    try:
        has_staged_diff = bool(repo.index.diff("HEAD"))
    except BadName:
        has_staged_diff = bool(repo.index.entries)
    if not has_staged_diff and not repo.untracked_files:
        return {"message": "No changes to commit"}

    # Commit
    commit = repo.index.commit(message)

    # Report what actually landed in the commit, not just the requested
    # `files` argument -- this is the ground truth for the `git add -A`
    # path too, so a caller can catch scope creep (e.g. a concurrent
    # background writer's in-flight edits getting swept into an unrelated
    # commit) from the response itself, instead of discovering it later
    # via a separate `git show`.
    result = {
        "commit_hash": str(commit.hexsha)[:7],
        "message": message,
        "author": str(commit.author),
        "files_committed": sorted(commit.stats.files.keys()),
    }
    if staged_all_changes:
        result["staged_all_changes"] = True
    return result


@_tool(read_only=False, destructive=True, idempotent=False, open_world=True)
@mcp_tool_handler
def git_push(
    branch: Optional[str] = None,
    set_upstream: bool = False,
    force: bool = False,
    repo_path: str = "."
) -> dict:
    """Push branch to remote origin.

    Annotated destructive because force=True rewrites published history on the
    remote, which cannot be undone from here and may discard commits other
    clones have already based work on.

    Args:
        branch: Branch to push (current if None)
        set_upstream: Set upstream tracking
        force: Force push (use with caution)
        repo_path: Repository path
    """
    if branch is not None:
        branch = _safe_ref(branch, "branch")
    repo = GitRepoClient.for_path(repo_path)
    origin = repo.remotes.origin
    push_branch = branch or str(repo.active_branch)

    kwargs = {}
    if set_upstream:
        kwargs["set_upstream"] = True
    if force:
        kwargs["force"] = True

    origin.push(push_branch, **kwargs)

    return {
        "branch": push_branch,
        "remote": "origin",
        "force": force
    }


@_tool(read_only=False, destructive=False, idempotent=False, open_world=True)
@mcp_tool_handler
def git_pull(branch: Optional[str] = None, repo_path: str = ".") -> dict:
    """Pull latest changes from remote origin."""
    if branch is not None:
        branch = _safe_ref(branch, "branch")
    repo = GitRepoClient.for_path(repo_path)
    origin = repo.remotes.origin
    pull_branch = branch or str(repo.active_branch)

    result = origin.pull(pull_branch)
    flags = [info.flags for info in result]

    return {
        "branch": pull_branch,
        "flags": flags
    }


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
@mcp_tool_handler
def git_diff(staged: bool = False, from_ref: Optional[str] = None, repo_path: str = ".") -> dict:
    """Get diff output.

    Args:
        staged: If True, show staged changes (--cached)
        from_ref: Compare against this ref (e.g., 'main')
        repo_path: Repository path
    """
    if from_ref:
        from_ref = _safe_ref(from_ref, "from_ref")
    repo = GitRepoClient.for_path(repo_path)

    if from_ref:
        diff_text = repo.git.diff(from_ref, "HEAD", "--stat")
    elif staged:
        diff_text = repo.git.diff("--cached", "--stat")
    else:
        diff_text = repo.git.diff("--stat")

    return {
        "diff_summary": diff_text,
        "staged": staged,
        "from_ref": from_ref
    }


@_tool(read_only=False, destructive=True, idempotent=False, open_world=False)
@mcp_tool_handler
def git_stash(action: str = "push", message: Optional[str] = None, repo_path: str = ".") -> dict:
    """Manage git stash.

    Annotated destructive because action='push' removes changes from the
    working tree, and a subsequent 'pop' can fail on conflict, leaving the
    caller without the state it expected.

    Args:
        action: 'push' to stash changes, 'pop' to restore, 'list' to show stashes
        message: Stash message (only for push)
        repo_path: Repository path
    """
    repo = GitRepoClient.for_path(repo_path)

    if action == "push":
        args = ["push", "--include-untracked"]
        if message:
            args.extend(["-m", message])
        try:
            result = repo.git.stash(*args)
        except GitCommandError as e:
            error_msg = str(e)
            if "No local changes" in error_msg or "No stash entries" in error_msg:
                return {"action": action, "result": "Nothing to stash/pop"}
            raise
        return {"action": "push", "result": result}

    elif action == "pop":
        try:
            result = repo.git.stash("pop")
        except GitCommandError as e:
            error_msg = str(e)
            if "No local changes" in error_msg or "No stash entries" in error_msg:
                return {"action": action, "result": "Nothing to stash/pop"}
            raise
        return {"action": "pop", "result": result}

    elif action == "list":
        result = repo.git.stash("list")
        stashes = [line for line in result.split("\n") if line]
        return {"action": "list", "stashes": stashes}

    else:
        raise ValueError(f"Unknown stash action: {action}")


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
@mcp_tool_handler
def git_log(count: int = 10, repo_path: str = ".") -> dict:
    """Get recent commit log.

    Args:
        count: Number of commits to show (default: 10)
        repo_path: Repository path
    """
    repo = GitRepoClient.for_path(repo_path)
    commits = []
    for commit in repo.iter_commits(max_count=count):
        commits.append({
            "hash": str(commit.hexsha)[:7],
            "message": commit.message.strip().split("\n")[0],
            "author": str(commit.author),
            "date": commit.committed_datetime.isoformat()
        })

    return {
        "commits": commits,
        "count": len(commits),
        "current_branch": str(repo.active_branch)
    }


@_tool(read_only=False, destructive=False, idempotent=True, open_world=True)
@mcp_tool_handler
def git_fetch(remote: str = "origin", branch: Optional[str] = None, prune: bool = False, repo_path: str = ".") -> dict:
    """Fetch from remote.

    Args:
        remote: Remote name (default: origin)
        branch: Specific branch to fetch (None = all)
        prune: Remove stale remote-tracking branches
        repo_path: Repository path
    """
    remote = _safe_ref(remote, "remote")
    if branch is not None:
        branch = _safe_ref(branch, "branch")
    repo = GitRepoClient.for_path(repo_path)
    remote_obj = repo.remote(remote)

    kwargs = {}
    if prune:
        kwargs["prune"] = True

    if branch:
        result = remote_obj.fetch(branch, **kwargs)
    else:
        result = remote_obj.fetch(**kwargs)

    fetched = [str(info.ref) for info in result]

    return {
        "remote": remote,
        "fetched_refs": fetched,
        "pruned": prune
    }


@_tool(read_only=False, destructive=True, idempotent=False, open_world=True)
@mcp_tool_handler
def git_post_merge_cleanup(
    merged_branch: str,
    main_branch: str = "main",
    repo_path: str = "."
) -> dict:
    """Clean up after a PR merge: switch to main, pull, delete branch, prune.

    Orchestrated 4-step workflow:
    1. Checkout main branch
    2. Pull latest from remote (includes merged PR)
    3. Delete local merged branch (force if needed)
    4. Prune stale remote-tracking branches

    Step 3 escalates from -d to -D, which discards unmerged commits, so the
    tool is annotated destructive. When both deletion attempts fail the reason
    is returned rather than dropped: reporting branch_deleted=false with no
    explanation left callers unable to tell a still-checked-out branch from a
    permissions problem.

    Args:
        merged_branch: Branch that was merged (will be deleted locally)
        main_branch: Target branch (default: main)
        repo_path: Repository path

    Returns:
        Dict with cleaned_branch, current_branch, branch_deleted, an optional
        branch_delete_error, and a human-readable message.
    """
    merged_branch = _safe_ref(merged_branch, "merged_branch")
    main_branch = _safe_ref(main_branch, "main_branch")
    repo = GitRepoClient.for_path(repo_path)
    origin = repo.remotes.origin

    repo.git.checkout(main_branch)
    origin.pull(main_branch)

    branch_deleted = False
    branch_delete_error = None
    if merged_branch != main_branch:
        try:
            repo.git.branch("-d", merged_branch)
            branch_deleted = True
        except GitCommandError:
            try:
                repo.git.branch("-D", merged_branch)
                branch_deleted = True
            except GitCommandError as exc:
                branch_delete_error = str(exc)[:300]

    origin.fetch(prune=True)

    result = {
        "cleaned_branch": merged_branch,
        "current_branch": main_branch,
        "branch_deleted": branch_deleted,
        "message": f"Cleaned up {merged_branch}, now on {main_branch} (synced)"
    }
    if branch_delete_error:
        result["branch_delete_error"] = branch_delete_error
        result["message"] = (
            f"Synced {main_branch}, but could not delete {merged_branch}"
        )
    return result


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
@mcp_tool_handler
def git_get_origin_url(repo_path: str = ".") -> dict:
    """Get the remote origin URL of the repository."""
    repo = GitRepoClient.for_path(repo_path)
    url = repo.remotes.origin.url
    return {
        "origin_url": url,
        "is_github": "github.com" in url
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
