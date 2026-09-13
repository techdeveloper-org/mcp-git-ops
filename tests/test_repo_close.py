"""Tests that every Repo a tool opens is closed when the tool returns (#19).

GitPython keeps ``git cat-file`` helper processes and open ``.git`` handles
alive until ``Repo.close()`` is called. In this long-lived MCP server on
Windows an unclosed Repo locks the repository directory, so a checkout the
server has touched cannot be deleted until the server exits.

Windows-safe: ASCII only, no Unicode characters.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class _FakeRepo:
    """Stand-in Repo that records whether it was closed."""

    def __init__(self):
        """Start unclosed."""
        self.closed = False

    def close(self):
        """Record the close."""
        self.closed = True


@pytest.fixture
def fake_repos(monkeypatch):
    """Make GitRepoClient.for_path hand out recording fakes.

    Yields:
        list: Every fake Repo created during the test, in creation order.
    """
    created = []

    def fake_for_path(repo_path="."):
        """Return a new recording fake for any path."""
        repo = _FakeRepo()
        created.append(repo)
        return repo

    monkeypatch.setattr(server.GitRepoClient, "for_path", staticmethod(fake_for_path))
    yield created


class TestOpenedReposAreClosed:
    """The wrapper closes what the tool opened, on success and on failure."""

    def test_every_repo_opened_during_a_call_is_closed_on_return(self, fake_repos):
        def tool_body(path):
            server._open_repo(path)
            server._open_repo(path)
            return "done"

        assert server._closes_opened_repos(tool_body)("x") == "done"
        assert len(fake_repos) == 2
        assert all(repo.closed for repo in fake_repos)

    def test_repos_are_closed_when_the_tool_raises(self, fake_repos):
        def failing_tool(path):
            server._open_repo(path)
            raise RuntimeError("tool failed")

        with pytest.raises(RuntimeError):
            server._closes_opened_repos(failing_tool)("x")
        assert fake_repos[0].closed

    def test_registry_is_reset_after_the_call(self, fake_repos):
        server._closes_opened_repos(lambda path: server._open_repo(path))("x")
        assert server._OPENED_REPOS.get() is None

    def test_open_repo_outside_a_tool_call_is_not_tracked_or_closed(self, fake_repos):
        repo = server._open_repo("x")
        assert repo.closed is False
        assert server._OPENED_REPOS.get() is None

    def test_registered_tools_carry_the_cleanup_wrapper(self):
        """git_status is registered through _tool, so its outermost layer is the cleanup wrapper."""
        assert server.git_status.__code__ is server._closes_opened_repos(lambda: None).__code__

    def test_wrapped_still_exposes_the_undecorated_body(self):
        """``tool.__wrapped__`` must stay the raw body so callers can observe raw exceptions."""
        raw = server.git_status.__wrapped__
        assert not hasattr(raw, "__wrapped__")
        assert raw.__name__ == "git_status"


def _clear_readonly_and_retry(func, path, *_exc):
    """Clear the read-only bit git sets on object files, then retry the removal."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_tree(path):
    """Delete a directory tree, handling git's read-only object files."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, onerror=_clear_readonly_and_retry)


class TestRealRepositoryIsReleased:
    """End to end: after real tool calls the checkout can be deleted at once."""

    def test_repository_is_deletable_right_after_tool_calls(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True)
        (repo_dir / "a.txt").write_text("x\n")
        subprocess.run(["git", "-C", str(repo_dir), "add", "a.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True, capture_output=True)

        server.git_status(repo_path=str(repo_dir))
        server.git_log(count=1, repo_path=str(repo_dir))

        _remove_tree(repo_dir)
        assert not repo_dir.exists()
