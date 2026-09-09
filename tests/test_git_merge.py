"""Tests for git_merge (server.py).

Added 2026-09-09: github_merge_pr (mcp-github-api) has no local fallback
when GitHub's own mergeability computation stalls (observed live -- a PR
sat at mergeable: null for several minutes while two sibling PRs against
the same repo merged cleanly seconds apart). This tool merges an
already-reviewed branch locally instead of reaching for raw git via a
shell, matching this project's MCP-first convention.

Windows-safe: ASCII only, no Unicode characters.
"""

import json
import sys
from pathlib import Path

import pytest
from git import GitCommandError, Repo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo, main branch with one commit, no remote."""
    repo = Repo.init(tmp_path, initial_branch="main")
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test User")
        cfg.set_value("user", "email", "test@example.com")
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    return repo


def _merge(repo_path, source, message=None, squash=True):
    """Call the real git_merge tool and return its parsed JSON payload."""
    raw = server.git_merge(source=source, message=message, squash=squash, repo_path=str(repo_path))
    return json.loads(raw) if isinstance(raw, str) else raw


class TestGitMergeSquash:
    def test_squash_merge_produces_one_commit_on_target(self, tmp_path, git_repo):
        git_repo.git.checkout("-b", "feature")
        (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
        git_repo.index.add(["a.txt"])
        git_repo.index.commit("feature commit 1")
        (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
        git_repo.index.add(["b.txt"])
        git_repo.index.commit("feature commit 2")
        git_repo.git.checkout("main")

        result = _merge(tmp_path, "feature", message="squash feature into main")

        assert result.get("success", True), result
        assert result["squash"] is True
        assert result["target"] == "main"
        assert (tmp_path / "a.txt").exists()
        assert (tmp_path / "b.txt").exists()
        # Squash merge must not carry the source branch's own commit
        # history onto main -- exactly one new commit, not two.
        log = git_repo.git.log("--oneline", "main").splitlines()
        assert len(log) == 2, f"expected init + one squash commit, got: {log}"

    def test_squash_merge_without_message_raises(self, tmp_path, git_repo):
        git_repo.git.checkout("-b", "feature")
        (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
        git_repo.index.add(["a.txt"])
        git_repo.index.commit("feature commit")
        git_repo.git.checkout("main")

        with pytest.raises(ValueError, match="message is required"):
            server.git_merge.__wrapped__(source="feature", message=None, squash=True, repo_path=str(tmp_path))


class TestGitMergeNoFastForward:
    def test_no_ff_merge_preserves_source_history(self, tmp_path, git_repo):
        git_repo.git.checkout("-b", "feature")
        (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
        git_repo.index.add(["a.txt"])
        git_repo.index.commit("feature commit 1")
        (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
        git_repo.index.add(["b.txt"])
        git_repo.index.commit("feature commit 2")
        git_repo.git.checkout("main")

        result = _merge(tmp_path, "feature", message="merge feature", squash=False)

        assert result.get("success", True), result
        assert result["squash"] is False
        log = git_repo.git.log("--oneline", "main").splitlines()
        # init + 2 feature commits + 1 merge commit = 4
        assert len(log) == 4, f"expected source history preserved via --no-ff, got: {log}"


class TestGitMergeConflict:
    def test_conflicting_merge_raises_and_leaves_conflict_markers(self, tmp_path, git_repo):
        (tmp_path / "shared.txt").write_text("original\n", encoding="utf-8")
        git_repo.index.add(["shared.txt"])
        git_repo.index.commit("add shared")

        git_repo.git.checkout("-b", "feature")
        (tmp_path / "shared.txt").write_text("feature version\n", encoding="utf-8")
        git_repo.index.add(["shared.txt"])
        git_repo.index.commit("feature edits shared")

        git_repo.git.checkout("main")
        (tmp_path / "shared.txt").write_text("main version\n", encoding="utf-8")
        git_repo.index.add(["shared.txt"])
        git_repo.index.commit("main edits shared")

        with pytest.raises(GitCommandError):
            server.git_merge.__wrapped__(source="feature", message="conflict", squash=True, repo_path=str(tmp_path))

        # This tool must not auto-resolve or abort -- the conflicted state
        # is left for the caller to inspect and resolve deliberately.
        status = git_repo.git.status("--porcelain")
        assert "UU shared.txt" in status or "shared.txt" in status


class TestGitMergeRefSafety:
    def test_source_beginning_with_dash_is_rejected(self, tmp_path, git_repo):
        with pytest.raises(ValueError, match="must not begin with"):
            server.git_merge.__wrapped__(source="--force", message="m", squash=True, repo_path=str(tmp_path))
