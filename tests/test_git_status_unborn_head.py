"""Regression coverage for git_status on an unborn-HEAD repository.

`repo.index.diff("HEAD")` raises gitdb.exc.BadName when HEAD does not yet
resolve to a commit -- a freshly `git init`-ed repo, before any commit
exists. git_status used this call unconditionally to compute the `staged`
list, so calling it on a brand-new repo (the very first thing anyone does
after `git init`, before the first commit) raised instead of returning a
status.

Windows-safe: ASCII only, no Unicode characters.
"""

import json
import sys
from pathlib import Path

from git import Repo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def _status(repo_path):
    raw = server.git_status(repo_path=str(repo_path))
    return json.loads(raw) if isinstance(raw, str) else raw


def test_status_on_freshly_initialized_repo_with_no_commits_does_not_raise(tmp_path):
    Repo.init(tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    result = _status(tmp_path)

    assert "error" not in result, result
    assert "README.md" in result["untracked"]


def test_status_reports_index_entries_as_staged_before_first_commit(tmp_path):
    repo = Repo.init(tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    repo.git.add("README.md")

    result = _status(tmp_path)

    assert "error" not in result, result
    assert "README.md" in result["staged"]
    assert "README.md" not in result["untracked"]
