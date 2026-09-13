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


class TestHookStdinIsolation:
    """Hooks must never inherit the server's stdin, which is the MCP protocol pipe (#18)."""

    @pytest.mark.skipif(sys.platform != "win32", reason="patch is a Windows-only no-op elsewhere")
    def test_defaults_stdin_to_devnull(self, monkeypatch):
        import subprocess

        import git.index.fun as git_index_fun

        captured = {}

        def fake_popen(command, *args, **kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr(git_index_fun, "safer_popen", fake_popen)
        hook_shell_fix.apply()
        git_index_fun.safer_popen(["bash.exe", ".git/hooks/pre-commit"])

        assert captured["stdin"] is subprocess.DEVNULL

    @pytest.mark.skipif(sys.platform != "win32", reason="patch is a Windows-only no-op elsewhere")
    def test_respects_an_explicit_stdin(self, monkeypatch):
        import subprocess

        import git.index.fun as git_index_fun

        captured = {}

        def fake_popen(command, *args, **kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr(git_index_fun, "safer_popen", fake_popen)
        hook_shell_fix.apply()
        git_index_fun.safer_popen(["bash.exe", ".git/hooks/pre-commit"], stdin=subprocess.PIPE)

        assert captured["stdin"] is subprocess.PIPE


class _FakeProcess:
    """Minimal Popen stand-in with a controllable exit state."""

    pid = 4242

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = False

    def poll(self):
        """Return the configured exit code, or None while "running"."""
        return self.returncode

    def kill(self):
        """Record a direct kill."""
        self.killed = True


class TestHookWatchdog:
    """A hung hook must be killed so GitPython raises instead of blocking forever."""

    def test_kills_the_tree_of_a_process_that_is_still_running(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hook_shell_fix.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))

        assert hook_shell_fix.kill_if_still_running(_FakeProcess(), ["bash", "hook"]) is True
        assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]]

    def test_leaves_a_finished_process_alone(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hook_shell_fix.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))

        assert hook_shell_fix.kill_if_still_running(_FakeProcess(returncode=0), ["bash", "hook"]) is False
        assert calls == []

    def test_falls_back_to_kill_when_taskkill_is_missing(self, monkeypatch):
        def missing_taskkill(cmd, **kwargs):
            raise OSError("taskkill not found")

        monkeypatch.setattr(hook_shell_fix.subprocess, "run", missing_taskkill)
        process = _FakeProcess()

        assert hook_shell_fix.kill_if_still_running(process, ["bash", "hook"]) is True
        assert process.killed

    def test_timer_fires_after_the_timeout(self, monkeypatch):
        import threading

        fired = threading.Event()
        monkeypatch.setattr(hook_shell_fix, "kill_if_still_running", lambda process, command: fired.set())

        timer = hook_shell_fix.start_hook_watchdog(_FakeProcess(), ["bash", "hook"], 0.05)

        assert timer is not None and timer.daemon
        assert fired.wait(5), "watchdog never fired"

    def test_non_positive_timeout_disables_the_watchdog(self):
        assert hook_shell_fix.start_hook_watchdog(_FakeProcess(), ["bash", "hook"], 0) is None

    def test_no_watchdog_without_a_process(self):
        assert hook_shell_fix.start_hook_watchdog(None, ["bash", "hook"], 5) is None

    def test_timeout_env_parsing(self, monkeypatch):
        monkeypatch.delenv(hook_shell_fix.HOOK_TIMEOUT_ENV_VAR, raising=False)
        assert hook_shell_fix.hook_timeout_seconds() == hook_shell_fix.DEFAULT_HOOK_TIMEOUT_SECONDS

        monkeypatch.setenv(hook_shell_fix.HOOK_TIMEOUT_ENV_VAR, "12")
        assert hook_shell_fix.hook_timeout_seconds() == 12.0

        monkeypatch.setenv(hook_shell_fix.HOOK_TIMEOUT_ENV_VAR, "not-a-number")
        assert hook_shell_fix.hook_timeout_seconds() == hook_shell_fix.DEFAULT_HOOK_TIMEOUT_SECONDS


@pytest.mark.skipif(sys.platform != "win32", reason="reproduces a Windows stdio-transport hang")
class TestCommitWithStdinReadingHook:
    """Regression for #18: a hook that reads stdin must not hang git_commit."""

    def test_commit_completes_while_parent_stdin_stays_open(self, tmp_path):
        import subprocess
        import textwrap
        import time

        repo = tmp_path / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\ncat >/dev/null\nexit 0\n", newline="\n")
        (repo / "a.txt").write_text("x\n")

        server_dir = Path(__file__).resolve().parent.parent
        child = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {server_dir!r})
            import server
            print("RESULT", server.git_commit("regression #18", repo_path={repo!r}), flush=True)
            """
        ).format(server_dir=str(server_dir), repo=str(repo))

        process = subprocess.Popen(
            [sys.executable, "-c", child],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.time() + 90
            while process.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            still_running = process.poll() is None
        finally:
            if process.poll() is None:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
            output, _ = process.communicate(timeout=30)

        assert not still_running, "git_commit blocked on a stdin-reading hook"
        assert process.returncode == 0, output
        assert "commit_hash" in output, output
