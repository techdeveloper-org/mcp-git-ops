"""Regression coverage for git_diff's --stat-only limitation.

git_diff hardcoded "--stat" on every code path, so a caller could get a
one-line summary of what changed but never the actual patch content, and
had no way to view a single commit's own changes (the `git show <sha>`
use case) -- only a working-tree/staged/from_ref comparison against HEAD.
Both gaps forced callers to fall back to a raw `git show`/`git diff`
subprocess instead of the MCP tool, which is exactly what this server
exists to replace.

Windows-safe: ASCII only, no Unicode characters.
"""

import json
import sys
from pathlib import Path

from git import Repo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def _diff(repo_path, **kwargs):
    raw = server.git_diff(repo_path=str(repo_path), **kwargs)
    return json.loads(raw) if isinstance(raw, str) else raw


def _commit(repo, path, content, message):
    path.write_text(content, encoding="utf-8")
    repo.git.add(str(path.name))
    repo.git.commit("-m", message)
    return repo.head.commit.hexsha


def test_full_false_still_returns_only_stat_summary(tmp_path):
    """Backward compatibility: default behavior is unchanged."""
    repo = Repo.init(tmp_path)
    repo.git.config("user.email", "test@example.com")
    repo.git.config("user.name", "Test")
    _commit(repo, tmp_path / "a.txt", "one\n", "first")
    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")

    result = _diff(tmp_path)

    assert "error" not in result, result
    assert "diff" not in result
    assert "a.txt" in result["diff_summary"]
    assert result["commit"] is None


def test_full_true_returns_actual_patch_content(tmp_path):
    """The bug: full=True must return real +/- patch lines, not a summary."""
    repo = Repo.init(tmp_path)
    repo.git.config("user.email", "test@example.com")
    repo.git.config("user.name", "Test")
    _commit(repo, tmp_path / "a.txt", "one\n", "first")
    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")

    result = _diff(tmp_path, full=True)

    assert "error" not in result, result
    assert "diff" in result
    assert "-one" in result["diff"]
    assert "+two" in result["diff"]


def test_commit_shows_that_commits_own_change_against_its_parent(tmp_path):
    """The other gap: view one commit's diff, like `git show <sha>`."""
    repo = Repo.init(tmp_path)
    repo.git.config("user.email", "test@example.com")
    repo.git.config("user.name", "Test")
    _commit(repo, tmp_path / "a.txt", "one\n", "first")
    second_sha = _commit(repo, tmp_path / "a.txt", "two\n", "second")
    _commit(repo, tmp_path / "b.txt", "unrelated\n", "third")

    result = _diff(tmp_path, commit=second_sha, full=True)

    assert "error" not in result, result
    assert result["commit"] == second_sha
    assert "-one" in result["diff"]
    assert "+two" in result["diff"]
    assert "b.txt" not in result["diff"]


def test_commit_rejects_argument_injection_via_safe_ref(tmp_path):
    repo = Repo.init(tmp_path)
    repo.git.config("user.email", "test@example.com")
    repo.git.config("user.name", "Test")
    _commit(repo, tmp_path / "a.txt", "one\n", "first")

    result = _diff(tmp_path, commit="--output=/tmp/pwned")

    assert result.get("success") is False
    assert "error" in result
