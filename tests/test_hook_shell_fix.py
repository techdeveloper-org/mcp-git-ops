"""Tests for hook_shell_fix - absolute-path shell resolution for git hooks.

Guards a non-obvious Windows invariant: GitPython spawns commit hooks as a bare
``bash.exe``, and ``CreateProcess`` resolves a bare name from System32 before it
ever consults PATH. Without this patch every commit through the server fails
against the WSL launcher stub and the hook body never runs.

Windows-safe: ASCII only, no Unicode characters.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hook_shell_fix  # noqa: E402


@pytest.fixture(autouse=True)
def reset_applied_flag():
    """Reset the module's idempotency flag around each test.

    Yields:
        None
    """
    original = hook_shell_fix._applied
    hook_shell_fix._applied = False
    yield
    hook_shell_fix._applied = original


class TestFindGitBash:
    """find_git_bash() must return Git's own bash, never the WSL stub."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only resolution")
    def test_returns_an_existing_file(self):
        found = hook_shell_fix.find_git_bash()
        assert found is not None, "Git for Windows bash was not found"
        assert found.is_file()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only resolution")
    def test_never_returns_the_system32_wsl_stub(self):
        """The System32 copy is the WSL launcher, not a usable shell."""
        import os

        found = hook_shell_fix.find_git_bash()
        assert found is not None
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
        assert system_root not in found.parents, "resolved to the System32 WSL stub"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only resolution")
    def test_returns_absolute_path(self):
        """A bare name would be re-resolved by CreateProcess, defeating the fix."""
        found = hook_shell_fix.find_git_bash()
        assert found is not None
        assert found.is_absolute()


class TestApply:
    """apply() must rewrite bare shell names and leave everything else alone."""

    @pytest.mark.skipif(sys.platform != "win32", reason="patch is a Windows-only no-op elsewhere")
    def test_rewrites_bare_bash_to_absolute_path(self, monkeypatch):
        import git.index.fun as git_index_fun

        captured = {}

        def fake_popen(command, *args, **kwargs):
            captured["command"] = command
            return None

        monkeypatch.setattr(git_index_fun, "safer_popen", fake_popen)
        bash_path = hook_shell_fix.apply()
        assert bash_path, "apply() reported no patch"

        git_index_fun.safer_popen(["bash.exe", ".git/hooks/pre-commit"])

        assert captured["command"][0] == bash_path
        assert captured["command"][1] == ".git/hooks/pre-commit"

    @pytest.mark.skipif(sys.platform != "win32", reason="patch is a Windows-only no-op elsewhere")
    def test_leaves_other_executables_untouched(self, monkeypatch):
        import git.index.fun as git_index_fun

        captured = {}

        def fake_popen(command, *args, **kwargs):
            captured["command"] = command
            return None

        monkeypatch.setattr(git_index_fun, "safer_popen", fake_popen)
        hook_shell_fix.apply()

        git_index_fun.safer_popen(["python.exe", "-c", "pass"])

        assert captured["command"] == ["python.exe", "-c", "pass"]

    @pytest.mark.skipif(sys.platform != "win32", reason="patch is a Windows-only no-op elsewhere")
    def test_is_idempotent(self, monkeypatch):
        """A second apply() must not wrap the wrapper."""
        import git.index.fun as git_index_fun

        monkeypatch.setattr(git_index_fun, "safer_popen", lambda command, *a, **k: None)
        first = hook_shell_fix.apply()
        after_first = git_index_fun.safer_popen
        second = hook_shell_fix.apply()

        assert first
        assert second == ""
        assert git_index_fun.safer_popen is after_first

    def test_no_op_on_non_windows(self, monkeypatch):
        monkeypatch.setattr(hook_shell_fix.sys, "platform", "linux")
        assert hook_shell_fix.apply() == ""
