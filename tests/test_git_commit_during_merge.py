"""Tests for git_commit while a merge is in progress (issue #20).

Found in real use, not by inspection: a merge of 18 files landed as a
single-parent commit containing 1 file, with the other 17 left as working-tree
modifications and no error raised anywhere. Every test in the consuming repo
still passed, because the working tree held the merged code -- so the damage
only surfaced when a separate push gate refused a dirty tree.

Two distinct defects are covered here. `files=` triggered an index reset that
threw the merge away, which git itself refuses; and repo.index.commit() never
consults MERGE_HEAD, so even a full commit recorded one parent.

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
def conflicting_repo(tmp_path):
    """A repo whose `feature` branch conflicts with `main` in one file.

    Two files diverge: `shared.txt` conflicts, `only_main.txt` does not, so a
    test can tell the difference between "the conflicted file was committed"
    and "the whole merge was committed".
    """
    repo = Repo.init(tmp_path, initial_branch="main")
    with repo.config_writer() as cfg:
        cfg.set_value("user", "name", "Test User")
        cfg.set_value("user", "email", "test@example.com")

    (tmp_path / "shared.txt").write_text("base\n", encoding="utf-8")
    repo.index.add(["shared.txt"])
    repo.index.commit("init")

    repo.git.checkout("-b", "feature")
    (tmp_path / "shared.txt").write_text("feature side\n", encoding="utf-8")
    (tmp_path / "only_feature.txt").write_text("feature only\n", encoding="utf-8")
    repo.index.add(["shared.txt", "only_feature.txt"])
    repo.index.commit("feature work")

    repo.git.checkout("main")
    (tmp_path / "shared.txt").write_text("main side\n", encoding="utf-8")
    (tmp_path / "only_main.txt").write_text("main only\n", encoding="utf-8")
    repo.index.add(["shared.txt", "only_main.txt"])
    repo.index.commit("main work")

    return repo


def _start_merge(repo):
    """Begin merging feature into main, leaving one file conflicted."""
    try:
        repo.git.merge("feature", "--no-ff")
    except Exception:
        pass
    assert (Path(repo.git_dir) / "MERGE_HEAD").is_file(), "fixture did not start a merge"


def _resolve(repo):
    """Resolve the conflict on disk and stage it, as a caller would."""
    root = Path(repo.working_dir)
    (root / "shared.txt").write_text("resolved\n", encoding="utf-8")
    repo.git.add("shared.txt")


def _commit(repo_path, message, files=None):
    """Call the real git_commit tool and return its parsed JSON payload."""
    raw = server.git_commit(message=message, files=files, repo_path=str(repo_path))
    return json.loads(raw) if isinstance(raw, str) else raw


class TestPartialCommitIsRefused:
    def test_files_during_a_merge_raises(self, conflicting_repo):
        """git's own words: "cannot do a partial commit during a merge"."""
        _start_merge(conflicting_repo)
        _resolve(conflicting_repo)

        result = _commit(conflicting_repo.working_dir, "merge", files="shared.txt")

        assert result.get("success") is False, result
        assert "partial commit during a merge" in json.dumps(result)

    def test_the_refusal_leaves_the_merge_intact(self, conflicting_repo):
        """A refusal that had already reset the index would be worse than the
        bug it replaces, so this asserts the merge is still resumable."""
        _start_merge(conflicting_repo)
        _resolve(conflicting_repo)

        _commit(conflicting_repo.working_dir, "merge", files="shared.txt")

        assert (Path(conflicting_repo.git_dir) / "MERGE_HEAD").is_file()
        assert "only_feature.txt" in conflicting_repo.git.diff("--cached", "--name-only")

    def test_the_refusal_names_what_to_do_instead(self, conflicting_repo):
        _start_merge(conflicting_repo)
        _resolve(conflicting_repo)

        result = _commit(conflicting_repo.working_dir, "merge", files="shared.txt")

        assert "without `files`" in json.dumps(result)


class TestAFullCommitCompletesTheMerge:
    def test_it_records_both_parents(self, conflicting_repo):
        """The defect that made a merge indistinguishable from an ordinary
        commit: repo.index.commit() never consults MERGE_HEAD."""
        _start_merge(conflicting_repo)
        _resolve(conflicting_repo)

        result = _commit(conflicting_repo.working_dir, "merge feature into main")

        assert result["merge_commit"] is True
        assert len(result["parents"]) == 2
        assert len(conflicting_repo.head.commit.parents) == 2

    def test_it_carries_every_merged_file_not_just_the_conflicted_one(self, conflicting_repo):
        _start_merge(conflicting_repo)
        _resolve(conflicting_repo)

        _commit(conflicting_repo.working_dir, "merge feature into main")

        tree = conflicting_repo.head.commit.tree
        assert "only_feature.txt" in [blob.name for blob in tree.blobs]
        assert "only_main.txt" in [blob.name for blob in tree.blobs]

    def test_it_clears_the_merge_state(self, conflicting_repo):
        """Left behind, MERGE_HEAD makes the repo believe it is still merging."""
        _start_merge(conflicting_repo)
        _resolve(conflicting_repo)

        _commit(conflicting_repo.working_dir, "merge feature into main")

        assert not (Path(conflicting_repo.git_dir) / "MERGE_HEAD").exists()
        assert not conflicting_repo.is_dirty()

    def test_the_message_is_the_caller_s_own(self, conflicting_repo):
        """git would otherwise substitute MERGE_MSG's generated text."""
        _start_merge(conflicting_repo)
        _resolve(conflicting_repo)

        _commit(conflicting_repo.working_dir, "a deliberate multi-line\n\nmerge message")

        assert conflicting_repo.head.commit.message.startswith("a deliberate multi-line")


class TestOrdinaryCommitsAreUnaffected:
    def test_a_scoped_commit_outside_a_merge_still_works(self, conflicting_repo):
        """The #11 behaviour this fix must not regress: `files` is exclusive."""
        root = Path(conflicting_repo.working_dir)
        (root / "a.txt").write_text("a\n", encoding="utf-8")
        (root / "b.txt").write_text("b\n", encoding="utf-8")

        result = _commit(conflicting_repo.working_dir, "only a", files="a.txt")

        assert result["files_committed"] == ["a.txt"]
        assert "merge_commit" not in result

    def test_a_full_commit_outside_a_merge_has_one_parent(self, conflicting_repo):
        root = Path(conflicting_repo.working_dir)
        (root / "c.txt").write_text("c\n", encoding="utf-8")

        result = _commit(conflicting_repo.working_dir, "everything")

        assert result["staged_all_changes"] is True
        assert len(conflicting_repo.head.commit.parents) == 1
