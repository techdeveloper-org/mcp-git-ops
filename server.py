"""
Git MCP Server - FastMCP-based Git operations for Claude Code.

Replaces 13+ subprocess calls in git_operations.py with GitPython library.
Backend: GitPython (already in requirements.txt)
Transport: stdio (Claude Code communicates via stdin/stdout)

Tools (14):
  git_status, git_branch_create, git_branch_switch, git_branch_list,
  git_branch_delete, git_commit, git_push, git_pull, git_diff,
  git_stash, git_log, git_fetch, git_post_merge_cleanup, git_get_origin_url

WARNING -- every tool's ``repo_path`` defaults to ``"."``, which resolves
relative to THIS SERVER PROCESS's own working directory (fixed once at
launch), never the calling agent's. That's fine in a normal single-checkout
session. It is NOT fine the moment more than one checkout of the same repo
exists at once -- most commonly a `git worktree` used to isolate a
subagent's changes -- because every call that omits repo_path still lands
on this server's one fixed checkout, silently switching its branch or
staging/stashing its state instead of the caller's own worktree. See
GitRepoClient's docstring in base/clients.py for a real incident writeup.
ALWAYS pass repo_path as an absolute path whenever multiple checkouts of
the repo could exist concurrently.
"""

import os
import sys
from pathlib import Path
from typing import Optional

# Ensure src/mcp/ is in path for base package imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

# GH-16: without this, a git subprocess that prompts for credentials over a
# plain terminal (SSH host-key confirmation, HTTPS basic-auth prompt on a
# machine without a GUI credential helper) blocks forever on stdin -- which
# this server never supplies interactively, since its own stdin carries the
# MCP protocol. Failing fast here turns an indefinite hang into an immediate,
# reportable git error. This does NOT cover GUI credential helpers (Git
# Credential Manager on Windows, osxkeychain prompts) -- those bypass the
# terminal entirely, which is why every network call below also carries an
# explicit kill_after_timeout.
os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

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

# GH-16: applied to every Remote.fetch()/push()/pull() call. This server
# handles one tool call at a time (stdio, synchronous), so an untimed-out
# subprocess -- most commonly a GUI credential-manager prompt nothing in
# this flow can answer -- freezes every queued tool call behind it, which
# looks to the caller like the whole MCP connection died. 60s is generous
# for a real network round-trip and short enough that a hang surfaces as a
# clear timeout error instead of an indefinite freeze.
_NETWORK_TIMEOUT_SECONDS = 60

# GitPython's kill_after_timeout implements the timeout by starting a
# background thread that SIGKILLs the git subprocess after the deadline.
# That mechanism is POSIX-only -- GitPython raises
# "'kill_after_timeout' feature is not supported on Windows" the moment the
# kwarg is passed on os.name == "nt", turning every fetch/push/pull call
# above into a hard failure on Windows regardless of network conditions.
# _network_timeout_kwargs() is the single source of truth for this kwarg:
# it degrades to no timeout on Windows (a hang there is at least visible to
# the user as a stuck process, whereas the exception above hard-fails every
# single call) and keeps the GH-16 protection on POSIX where it works.
def _network_timeout_kwargs() -> dict:
    if os.name == "nt":
        return {}
    return {"kill_after_timeout": _NETWORK_TIMEOUT_SECONDS}


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
        "repo_path": str(repo.working_dir),
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
        repo_path: Repository path. Defaults to "." (this server process's
            own cwd, NOT the caller's) -- pass an absolute path explicitly
            whenever a git worktree or other second checkout of this repo
            could exist. See module docstring WARNING. Getting this wrong
            here is worse than on a read-only tool: this call stashes,
            checks out, and pops against whatever repo_path resolves to.

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
        origin.fetch(from_branch, **_network_timeout_kwargs())
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
        origin.push(name, set_upstream=True, **_network_timeout_kwargs())
        pushed = True
    except GitCommandError as exc:
        push_error = str(exc)[:300]

    result = {
        "repo_path": str(repo.working_dir),
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
    """Switch to an existing branch.

    repo_path defaults to "." (this server's own cwd, not the caller's) --
    pass it explicitly in any multi-checkout / git-worktree setup. See
    module docstring WARNING.
    """
    name = _safe_ref(name, "name")
    repo = GitRepoClient.for_path(repo_path)
    repo.git.checkout(name)
    return {"repo_path": str(repo.working_dir), "branch": name}


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
        "repo_path": str(repo.working_dir),
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
    return {"repo_path": str(repo.working_dir), "deleted": name, "force": force}


@_tool(read_only=False, destructive=False, idempotent=False, open_world=False)
@mcp_tool_handler
def git_commit(message: str, files: Optional[str] = None, repo_path: str = ".") -> dict:
    """Stage files and create a commit.

    Args:
        message: Commit message (can be multi-line)
        files: Comma-separated file paths to stage. If empty, stages all changes.
        repo_path: Repository path. Defaults to "." (this server process's
            own cwd, NOT the caller's) -- pass an absolute path explicitly
            in any git-worktree or multi-checkout setup, or this stages and
            commits against the wrong working directory. See module
            docstring WARNING.
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
        # `files` must be exclusive, not additive: reset the index to HEAD
        # first (working tree untouched) so anything already staged from
        # an earlier `git add` or `git reset --soft` doesn't ride along
        # into this commit alongside the caller's named files. Without
        # this, a caller has no way to make a truly scoped commit if
        # anything else happens to be staged (#11).
        try:
            repo.git.reset("HEAD", "--")
        except GitCommandError:
            # HEAD is unborn (first commit ever) - nothing to reset from.
            pass
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
        return {"repo_path": str(repo.working_dir), "message": "No changes to commit"}

    # Commit
    commit = repo.index.commit(message)

    # Report what actually landed in the commit, not just the requested
    # `files` argument -- this is the ground truth for the `git add -A`
    # path too, so a caller can catch scope creep (e.g. a concurrent
    # background writer's in-flight edits getting swept into an unrelated
    # commit) from the response itself, instead of discovering it later
    # via a separate `git show`.
    result = {
        "repo_path": str(repo.working_dir),
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
        repo_path: Repository path. Defaults to "." (this server process's
            own cwd, NOT the caller's) -- pass an absolute path explicitly
            in any git-worktree or multi-checkout setup. See module
            docstring WARNING.
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

    kwargs.update(_network_timeout_kwargs())
    origin.push(push_branch, **kwargs)

    return {
        "repo_path": str(repo.working_dir),
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

    result = origin.pull(pull_branch, **_network_timeout_kwargs())
    flags = [info.flags for info in result]

    return {
        "repo_path": str(repo.working_dir),
        "branch": pull_branch,
        "flags": flags
    }


@_tool(read_only=False, destructive=False, idempotent=False, open_world=True)
@mcp_tool_handler
def git_merge(
    source: str,
    message: Optional[str] = None,
    squash: bool = True,
    repo_path: str = "."
) -> dict:
    """Merge a source branch or ref into the current branch.

    Added because ``github_merge_pr`` (mcp-github-api) has no local fallback
    when GitHub's own mergeability computation stalls -- observed live
    2026-09-09 (a PR sat at ``mergeable: null`` for several minutes while two
    sibling PRs against the same repo merged cleanly seconds apart), and
    that server deliberately refuses to guess in that state rather than
    risk merging something GitHub has not actually validated. This tool
    lets the caller merge the same already-reviewed branch locally and push
    it, without reaching for raw ``git`` via a shell (see this module's
    header comment on why ``repo_path`` must be explicit and why this
    server exists at all).

    ``squash=True`` (the default) matches this project's own convention of
    squash-merging PRs via ``github_merge_pr``'s default ``method="squash"``
    -- one commit on the target branch per source branch, regardless of how
    many commits the source branch accumulated. ``squash=False`` performs an
    ordinary merge commit (``--no-ff``), preserving the source branch's own
    commit history.

    Args:
        source: Branch or ref to merge into the current branch. Must already
            be available locally -- run git_fetch first if it only exists on
            the remote.
        message: Commit message. Required when squash=True (squash merges do
            not get an automatic message from git the way merge commits do).
            Ignored when squash=False and a fast-forward merge is possible;
            used as the merge commit message otherwise.
        squash: Squash all of source's commits into one commit on the
            current branch (default True). False performs a --no-ff merge
            commit, preserving source's own commit history.
        repo_path: Repository path. Defaults to "." (this server process's
            own cwd, NOT the caller's) -- pass an absolute path explicitly
            in any git-worktree or multi-checkout setup, or this merges
            into the wrong working directory. See module docstring WARNING.

    Returns:
        Dict with repo_path, source, target branch, squash, and
        commit_hash of the resulting commit (the new squash commit, or the
        merge commit -- never the fast-forwarded tip's original hash if one
        already existed under a different name, since a fast-forward does
        not create a new commit).

    Raises:
        ValueError: If squash=True and message is not provided, or source
            fails ref-safety validation (see _safe_ref).
        GitCommandError: If the merge conflicts. The working tree is left
            in the conflicted state for manual resolution -- this tool does
            not attempt to auto-resolve or abort on conflict, since guessing
            a resolution is exactly the kind of silent behavior this
            module's callers must never rely on.
    """
    source = _safe_ref(source, "source")
    if squash and not message:
        raise ValueError("message is required when squash=True")

    repo = GitRepoClient.for_path(repo_path)
    target_branch = str(repo.active_branch)

    if squash:
        repo.git.merge("--squash", source)
        commit = repo.index.commit(message)
        commit_hash = str(commit.hexsha)[:7]
    else:
        merge_args = ["--no-ff", source]
        if message:
            merge_args = ["--no-ff", "-m", message, source]
        repo.git.merge(*merge_args)
        commit_hash = str(repo.head.commit.hexsha)[:7]

    return {
        "repo_path": str(repo.working_dir),
        "source": source,
        "target": target_branch,
        "squash": squash,
        "commit_hash": commit_hash,
    }


def _diff_target_args(commit: Optional[str], from_ref: Optional[str], staged: bool) -> list:
    """Build the positional args ``git diff`` needs to select its comparison.

    Precedence is commit > from_ref > staged > working tree, matching the
    order git_diff itself documents and checks in.

    Args:
        commit: A single commit to diff against its first parent, or None.
        from_ref: A ref to diff against HEAD, or None.
        staged: Whether to diff the index against HEAD.

    Returns:
        Positional arguments for ``repo.git.diff(*args)``, before any
        ``--stat`` flag is appended.
    """
    if commit:
        return [f"{commit}^", commit]
    if from_ref:
        return [from_ref, "HEAD"]
    if staged:
        return ["--cached"]
    return []


@_tool(read_only=True, destructive=False, idempotent=True, open_world=False)
@mcp_tool_handler
def git_diff(
    staged: bool = False,
    from_ref: Optional[str] = None,
    commit: Optional[str] = None,
    full: bool = False,
    repo_path: str = ".",
) -> dict:
    """Get diff output.

    Args:
        staged: If True, show staged changes (--cached)
        from_ref: Compare this ref against HEAD (e.g., 'main')
        commit: Show one commit's own changes -- the diff between it and its
            first parent, like ``git show <commit>`` without the message.
            Takes precedence over from_ref and staged when given.
        full: If True, also return the actual patch text in ``diff``, not
            just the ``--stat`` summary. Full patches can be large, so this
            defaults to False.
        repo_path: Repository path
    """
    if from_ref:
        from_ref = _safe_ref(from_ref, "from_ref")
    if commit:
        commit = _safe_ref(commit, "commit")
    repo = GitRepoClient.for_path(repo_path)

    target_args = _diff_target_args(commit, from_ref, staged)
    diff_summary = repo.git.diff(*target_args, "--stat")

    result = {
        "repo_path": str(repo.working_dir),
        "diff_summary": diff_summary,
        "staged": staged,
        "from_ref": from_ref,
        "commit": commit,
    }
    if full:
        result["diff"] = repo.git.diff(*target_args)
    return result


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
                return {"repo_path": str(repo.working_dir), "action": action, "result": "Nothing to stash/pop"}
            raise
        return {"repo_path": str(repo.working_dir), "action": "push", "result": result}

    elif action == "pop":
        try:
            result = repo.git.stash("pop")
        except GitCommandError as e:
            error_msg = str(e)
            if "No local changes" in error_msg or "No stash entries" in error_msg:
                return {"repo_path": str(repo.working_dir), "action": action, "result": "Nothing to stash/pop"}
            raise
        return {"repo_path": str(repo.working_dir), "action": "pop", "result": result}

    elif action == "list":
        result = repo.git.stash("list")
        stashes = [line for line in result.split("\n") if line]
        return {"repo_path": str(repo.working_dir), "action": "list", "stashes": stashes}

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
        "repo_path": str(repo.working_dir),
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

    kwargs = _network_timeout_kwargs()
    if prune:
        kwargs["prune"] = True

    if branch:
        result = remote_obj.fetch(branch, **kwargs)
    else:
        result = remote_obj.fetch(**kwargs)

    fetched = [str(info.ref) for info in result]

    return {
        "repo_path": str(repo.working_dir),
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
    origin.pull(main_branch, **_network_timeout_kwargs())

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

    origin.fetch(prune=True, **_network_timeout_kwargs())

    result = {
        "repo_path": str(repo.working_dir),
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
        "repo_path": str(repo.working_dir),
        "origin_url": url,
        "is_github": "github.com" in url
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
