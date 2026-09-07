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


class TestGitCommitReportsWhatWasCommitted:
    """Regression coverage for a defect where git_commit(files=None) ran
    `git add -A` and swept in unrelated concurrent writers' in-flight
    changes under a commit message describing only the caller's own
    narrow change -- with nothing in the response to reveal it happened.
    """

    def test_scoped_commit_reports_exactly_the_requested_files(self, tmp_path, git_repo):
        (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")

        result = _commit(tmp_path, "add a and b", files="a.txt,b.txt")

        assert result.get("success", True), result
        assert set(result["files_committed"]) == {"a.txt", "b.txt"}
        assert "staged_all_changes" not in result

    def test_stage_all_commit_reports_every_file_it_actually_staged(self, tmp_path, git_repo):
        (tmp_path / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        (tmp_path / "intended.txt").write_text("intended\n", encoding="utf-8")

        result = _commit(tmp_path, "intended change only", files=None)

        assert result.get("success", True), result
        assert result["staged_all_changes"] is True
        assert set(result["files_committed"]) == {"unrelated.txt", "intended.txt"}, (
            "files_committed must reflect everything git add -A actually staged, "
            "not just the files the caller meant to change -- this is what makes "
            "scope creep from a git add -A commit visible in the response itself"
        )


class TestGitCommitOnUnbornHead:
    """Regression coverage for a defect where git_commit (and git_status)
    raised gitdb.exc.BadName -- surfaced to callers as "Ref 'HEAD' did not
    resolve to an object" -- on a freshly `git init`-ed repo that has no
    commits yet. `repo.index.diff("HEAD")` requires HEAD to resolve to a
    real commit; on an unborn branch it does not, so the very first commit
    in any repo could never be made through this tool.
    """

    def test_first_commit_ever_succeeds_on_freshly_initialized_repo(self, tmp_path):
        repo = Repo.init(tmp_path)
        with repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Test User")
            cfg.set_value("user", "email", "test@example.com")
        (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

        result = _commit(tmp_path, "initial commit", files=None)

        assert result.get("success", True), result
        assert result.get("message") != "No changes to commit", result
        assert "README.md" in set(result.get("files_committed", [])), result
        assert repo.head.is_valid()

    def test_second_call_on_unborn_head_with_nothing_new_reports_no_changes(self, tmp_path):
        repo = Repo.init(tmp_path)
        with repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Test User")
            cfg.set_value("user", "email", "test@example.com")
        (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
        first = _commit(tmp_path, "initial commit", files=None)
        assert first.get("success", True), first

        second = _commit(tmp_path, "nothing changed", files=None)

        assert second.get("message") == "No changes to commit", second


class TestGitCommitFilesParamIsExclusive:
    """Regression coverage for #11: `files` must be exclusive, not additive.

    If a caller (or an earlier `git add` / `git reset --soft` in the same
    session) has already staged a file that isn't named in `files`, a
    scoped git_commit(files=...) call must not silently absorb it into
    the commit. The old behavior ran a bare `git add <files>` on top of
    whatever the index already held, so an unrelated staged file rode
    along into a commit whose message described only the caller's
    intended, narrower change.
    """

    def test_previously_staged_file_is_excluded_from_a_scoped_commit(self, tmp_path, git_repo):
        (tmp_path / "already_staged.txt").write_text("leftover\n", encoding="utf-8")
        (tmp_path / "intended.txt").write_text("the actual change\n", encoding="utf-8")

        # Simulate a prior `git add` (or a `git reset --soft` that left a
        # previous commit's file staged) before the scoped call under test.
        git_repo.git.add("already_staged.txt")

        result = _commit(tmp_path, "intended change only", files="intended.txt")

        assert result.get("success", True), result
        assert set(result["files_committed"]) == {"intended.txt"}, (
            "a file staged before the scoped git_commit call must not be "
            f"swept into it: {result}"
        )
        tracked_at_head = set(git_repo.git.show("--name-only", "--format=", "HEAD").splitlines())
        assert "already_staged.txt" not in tracked_at_head
        assert "intended.txt" in tracked_at_head
        # The unrelated file's content must survive in the working tree --
        # excluding it from the commit must not discard the caller's other
        # in-progress work, only leave it uncommitted. It ends up unstaged
        # (not staged), the same state `git restore --staged` produces --
        # this is the simplest, most predictable outcome: everything not
        # named in `files` is simply untouched by this commit.
        assert (tmp_path / "already_staged.txt").read_text(encoding="utf-8") == "leftover\n"
        assert "already_staged.txt" in set(git_repo.untracked_files), (
            "was never in HEAD, so unstaging it must return it to untracked, "
            "not silently drop its content"
        )

    def test_files_param_on_unborn_head_still_works(self, tmp_path):
        """The reset-to-HEAD fix must not break the very first commit ever,
        where there is no HEAD to reset to (see TestGitCommitOnUnbornHead).
        """
        repo = Repo.init(tmp_path)
        with repo.config_writer() as cfg:
            cfg.set_value("user", "name", "Test User")
            cfg.set_value("user", "email", "test@example.com")
        (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

        result = _commit(tmp_path, "initial commit", files="README.md")

        assert result.get("success", True), result
        assert set(result.get("files_committed", [])) == {"README.md"}, result
        assert repo.head.is_valid()


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
