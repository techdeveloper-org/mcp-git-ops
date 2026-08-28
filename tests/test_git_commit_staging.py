"""Tests for git_commit's staging behavior (server.py).

Regression coverage for a defect found while using this server to commit a
real project: passing a directory path in ``files`` staged gitignored
build artifacts (IndexFile.add() ignores .gitignore), and passing a path
whose file had already been deleted from disk raised FileNotFoundError
instead of staging the deletion (IndexFile.add() requires the path to
exist). Both are fixed by staging through ``repo.git.add`` (the porcelain
``git add`` command) instead of the lower-level ``repo.index.add`` API.

Windows-safe: ASCII only, no Unicode characters.
"""

import json
import sys
from pathlib import Path

import pytest
from git import Repo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with a commit identity configured, no remote."""
    repo = Repo.init(tmp_path)
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test User")
        cfg.set_value("user", "email", "test@example.com")
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    return repo


def _commit(repo_path, message, files=None):
    """Call the real git_commit tool and return its parsed JSON payload."""
    raw = server.git_commit(message=message, files=files, repo_path=str(repo_path))
    return json.loads(raw) if isinstance(raw, str) else raw


class TestGitCommitRespectsGitignore:
    def test_directory_path_staging_skips_gitignored_files(self, tmp_path, git_repo):
        (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        git_repo.index.add([".gitignore"])
        git_repo.index.commit("add gitignore")

        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "module.py").write_text("x = 1\n", encoding="utf-8")
        cache_dir = pkg / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "module.cpython-313.pyc").write_bytes(b"\x00\x01")

        result = _commit(tmp_path, "add pkg", files="pkg/")

        assert result.get("success", True), result
        tracked = set(git_repo.git.ls_files().splitlines())
        assert "pkg/module.py" in tracked
        assert not any("__pycache__" in path or path.endswith(".pyc") for path in tracked), (
            f"gitignored files were staged: {tracked}"
        )

    def test_explicit_pyc_path_is_still_staged_if_not_gitignored(self, tmp_path, git_repo):
        """Sanity check the fix does not over-broadly exclude non-ignored files --
        only .gitignore-matched paths should be skipped, not anything with 'pyc' in the name.
        """
        (tmp_path / "notes.pyc.txt").write_text("not actually bytecode\n", encoding="utf-8")

        result = _commit(tmp_path, "add notes", files="notes.pyc.txt")

        assert result.get("success", True), result
        tracked = set(git_repo.git.ls_files().splitlines())
        assert "notes.pyc.txt" in tracked


class TestGitCommitStagesDeletions:
    def test_deleted_file_is_committed_as_a_removal(self, tmp_path, git_repo):
        target = tmp_path / "scratch.txt"
        target.write_text("temporary\n", encoding="utf-8")
        first = _commit(tmp_path, "add scratch", files="scratch.txt")
        assert first.get("success", True), first
        assert "scratch.txt" in set(git_repo.git.ls_files().splitlines())

        target.unlink()

        second = _commit(tmp_path, "remove scratch", files="scratch.txt")

        assert second.get("success", True), (
            f"deleting an already-removed-from-disk path must stage the removal, not error: {second}"
        )
        assert "scratch.txt" not in set(git_repo.git.ls_files().splitlines())

    def test_mixed_batch_of_deleted_and_present_paths_both_stage_correctly(self, tmp_path, git_repo):
        (tmp_path / "keep.txt").write_text("stays\n", encoding="utf-8")
        (tmp_path / "gone.txt").write_text("leaves\n", encoding="utf-8")
        first = _commit(tmp_path, "add both", files="keep.txt,gone.txt")
        assert first.get("success", True), first

        (tmp_path / "gone.txt").unlink()
        (tmp_path / "keep.txt").write_text("stays, modified\n", encoding="utf-8")

        second = _commit(tmp_path, "update keep, remove gone", files="keep.txt,gone.txt")

        assert second.get("success", True), second
        tracked = set(git_repo.git.ls_files().splitlines())
        assert "keep.txt" in tracked
        assert "gone.txt" not in tracked
        assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "stays, modified\n"
