"""Tests for the repo_path echo added to every tool's response (#9).

#9 reported that batched/parallel tool calls across many repos occasionally
returned a result body that did not correspond to the requested repo_path
(branch names swapped between two repos, results misaligned to the wrong
path). No shared or global state was found in this server's own code (each
call creates a fresh git.Repo via GitRepoClient.for_path with no caching),
so the suspected root cause sits above this server -- in the MCP SDK's
concurrent task dispatch or the client's response correlation, neither of
which this repo controls or can fix directly.

What this repo CAN do, and does here: every tool now echoes back
str(repo.working_dir) -- the resolved, absolute path GitPython computed --
so any caller can immediately self-verify that a response actually
corresponds to the repo it asked about, rather than trusting positional
correlation. This does not prove the underlying race is fixed; it makes
a misaligned result detectable at zero extra cost instead of silently
trusted, which is what #9's own "Impact" section identifies as the actual
harm ("a caller trusts the result without cross-checking").

Windows-safe: ASCII only, no Unicode characters.
"""

import json
import sys
from pathlib import Path

import pytest
from git import Repo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def _call(tool, repo_path, **kwargs):
    """Call a real server tool function and return its parsed JSON payload."""
    raw = tool(repo_path=str(repo_path), **kwargs)
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.fixture
def two_repos(tmp_path):
    """Two independent, distinctly-identifiable git repos."""
    repos = {}
    for name in ("repo_a", "repo_b"):
        repo_dir = tmp_path / name
        repo_dir.mkdir()
        repo = Repo.init(repo_dir)
        with repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Test User")
            cfg.set_value("user", "email", "test@example.com")
        (repo_dir / "README.md").write_text(f"{name}\n", encoding="utf-8")
        repo.index.add(["README.md"])
        repo.index.commit(f"init {name}")
        repos[name] = repo
    return repos


class TestRepoPathEchoIdentifiesTheCorrectRepo:
    def test_git_status_echoes_the_resolved_path_of_the_repo_it_actually_read(
        self, two_repos
    ):
        for name, repo in two_repos.items():
            result = _call(server.git_status, repo.working_dir)
            assert result.get("success", True), result
            assert result["repo_path"] == str(Path(repo.working_dir).resolve()) or \
                Path(result["repo_path"]).resolve() == Path(repo.working_dir).resolve(), (
                f"git_status on {name} echoed a repo_path that does not "
                f"resolve to {name}'s own working_dir: {result}"
            )

    def test_two_repos_queried_in_sequence_never_echo_each_others_path(self, two_repos):
        repo_a, repo_b = two_repos["repo_a"], two_repos["repo_b"]

        result_a = _call(server.git_branch_list, repo_a.working_dir)
        result_b = _call(server.git_branch_list, repo_b.working_dir)

        path_a = Path(result_a["repo_path"]).resolve()
        path_b = Path(result_b["repo_path"]).resolve()

        assert path_a == Path(repo_a.working_dir).resolve(), result_a
        assert path_b == Path(repo_b.working_dir).resolve(), result_b
        assert path_a != path_b, (
            "two distinct repos echoed the same repo_path - "
            f"a={result_a}, b={result_b}"
        )

    def test_git_log_echoes_repo_path_alongside_its_existing_fields(self, two_repos):
        repo = two_repos["repo_a"]
        result = _call(server.git_log, repo.working_dir, count=5)

        assert result.get("success", True), result
        assert Path(result["repo_path"]).resolve() == Path(repo.working_dir).resolve()
        # Pre-existing fields must still be present - this is additive, not a rename.
        assert "commits" in result
        assert "current_branch" in result
