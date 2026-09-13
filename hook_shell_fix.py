"""Make GitPython run Windows git hooks safely: Git's bash, no inherited stdin, bounded time.

GitPython cannot rely on a shebang on Windows, so ``git.index.fun.run_commit_hook``
spawns hooks as ``["bash.exe", <hook>]`` through ``safer_popen``. That single spawn
site has three problems when this server runs them, and this module patches all
three.

1. Wrong shell. ``CreateProcess`` searches for a bare executable name in this order --

       application dir -> parent cwd -> System32 -> Windows dir -> PATH

   -- so ``C:\\Windows\\System32\\bash.exe`` is found at step 3, before PATH is ever
   consulted. On a machine with the WSL launcher stub installed but no distro, that
   stub answers every hook invocation with "Windows Subsystem for Linux has no
   installed distributions" and exits 1. Putting Git's ``bin`` directory on PATH does
   not help; the interpreter has to be named by absolute path.

2. Inherited stdin (issue #18). ``run_commit_hook`` pipes stdout and stderr but passes
   no ``stdin``, so the hook -- and everything it starts, such as the pre-commit
   framework, ruff, black and git -- inherits this server's stdin. Under the stdio MCP
   transport that is the live JSON-RPC pipe, which never reaches EOF: any process in
   the hook tree that reads stdin blocks forever, and the whole server blocks with it.
   Hooks now get ``subprocess.DEVNULL`` unless the caller chose a stdin explicitly.

3. No time limit. GitPython waits on the hook with no timeout, and its own
   ``kill_after_timeout`` is POSIX-only (it raises on Windows, see commit 7d16aa0).
   A daemon watchdog now kills the hook's whole process tree with ``taskkill /T /F``
   after ``GIT_OPS_HOOK_TIMEOUT`` seconds (default 600, ``0`` disables it). The killed
   hook exits non-zero, so GitPython raises ``HookExecutionError`` and the tool call
   returns an error instead of hanging.

Fails open. On non-Windows platforms, or when Git's bash cannot be located, the
original GitPython behavior is left untouched.

Usage:
    import hook_shell_fix

    hook_shell_fix.apply()

Windows-safe: ASCII only, no Unicode characters.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_SHELL_NAMES = {"bash.exe", "bash", "sh.exe", "sh"}

_FALLBACK_INSTALL_ROOTS = (
    r"C:\Program Files\Git",
    r"C:\Program Files (x86)\Git",
    r"C:\Git",
)

HOOK_TIMEOUT_ENV_VAR = "GIT_OPS_HOOK_TIMEOUT"
DEFAULT_HOOK_TIMEOUT_SECONDS = 600.0

logger = logging.getLogger(__name__)

_applied = False


def find_git_bash():
    """Locate Git for Windows' own ``bash.exe`` by absolute path.

    Resolution order: the ``bin`` directory next to the ``git.exe`` already on
    PATH, then ``GIT_INSTALL_ROOT``, then the standard install locations. Any
    candidate inside the Windows system directory is rejected -- that is the WSL
    launcher stub this module exists to avoid.

    Returns:
        Path or None: Absolute path to Git's bash, or None when not found.
    """
    candidates = []

    git_exe = shutil.which("git")
    if git_exe:
        git_root = Path(git_exe).resolve().parent.parent
        candidates.append(git_root / "bin" / "bash.exe")

    install_root = os.environ.get("GIT_INSTALL_ROOT", "").strip()
    if install_root:
        candidates.append(Path(install_root) / "bin" / "bash.exe")

    for root in _FALLBACK_INSTALL_ROOTS:
        candidates.append(Path(root) / "bin" / "bash.exe")

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if system_root in resolved.parents:
            continue
        return resolved

    return None


def hook_timeout_seconds():
    """Read the hook time limit from ``GIT_OPS_HOOK_TIMEOUT``.

    An unset or blank variable yields the default. A value that is not a number
    is ignored with a warning rather than failing the commit.

    Returns:
        float: Seconds a hook may run before its process tree is killed; ``0`` or
            a negative value disables the watchdog.
    """
    raw = os.environ.get(HOOK_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_HOOK_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid hook timeout ignored",
            extra={"env_var": HOOK_TIMEOUT_ENV_VAR, "value": raw, "default_seconds": DEFAULT_HOOK_TIMEOUT_SECONDS},
        )
        return DEFAULT_HOOK_TIMEOUT_SECONDS


def kill_if_still_running(process, command):
    """Kill a hook's whole process tree if it has not exited yet.

    ``taskkill /T`` is used instead of ``Popen.kill`` because the hook is a shell
    whose children (pre-commit, linters, git) would otherwise survive and keep the
    output pipes open, which is exactly what leaves GitPython waiting.

    Args:
        process: The ``Popen`` object returned for the hook.
        command: The argument vector the hook was started with, for the log.

    Returns:
        bool: True when the process was still running and a kill was issued.
    """
    if process.poll() is not None:
        return False
    logger.warning(
        "git hook exceeded its time limit; killing its process tree",
        extra={"pid": process.pid, "command": list(command) if isinstance(command, (list, tuple)) else command},
    )
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        logger.warning("taskkill unavailable; killing the hook process only", extra={"pid": process.pid, "error": str(exc)})
        process.kill()
    return True


def start_hook_watchdog(process, command, timeout):
    """Arm a daemon timer that kills a hook still running after ``timeout`` seconds.

    Args:
        process: The ``Popen`` object for the hook, or None when spawning returned
            nothing (as test doubles do).
        command: The hook's argument vector, passed through for logging.
        timeout: Seconds before the kill; ``0`` or less disables the watchdog.

    Returns:
        threading.Timer or None: The armed timer, or None when no watchdog was set.
    """
    if timeout <= 0 or process is None or not hasattr(process, "poll"):
        return None
    timer = threading.Timer(timeout, kill_if_still_running, args=(process, command))
    timer.daemon = True
    timer.start()
    return timer


def apply():
    """Patch GitPython's hook spawn site: absolute shell, DEVNULL stdin, watchdog.

    Idempotent and safe to call more than once. Does nothing on non-Windows
    platforms, when GitPython is not importable, or when Git's bash cannot be
    located.

    Returns:
        str: Absolute path to the bash that will be used, or "" when no patch
            was applied.
    """
    global _applied

    if _applied or sys.platform != "win32":
        return ""

    try:
        import git.index.fun as git_index_fun
    except Exception:
        return ""

    git_bash = find_git_bash()
    if git_bash is None:
        return ""

    original_popen = git_index_fun.safer_popen
    bash_path = str(git_bash)

    def popen_for_hooks(command, *args, **kwargs):
        """Spawn a git hook with Git's bash, no inherited stdin, and a watchdog.

        Args:
            command: Argument vector GitPython wants to spawn.
            *args: Passed through to the wrapped ``safer_popen``.
            **kwargs: Passed through to the wrapped ``safer_popen``; ``stdin``
                defaults to ``subprocess.DEVNULL`` when the caller did not set it.

        Returns:
            The process object returned by the wrapped ``safer_popen``.
        """
        if isinstance(command, (list, tuple)) and command:
            head = str(command[0])
            if head in _SHELL_NAMES:
                command = [bash_path] + list(command[1:])
        kwargs.setdefault("stdin", subprocess.DEVNULL)
        process = original_popen(command, *args, **kwargs)
        start_hook_watchdog(process, command, hook_timeout_seconds())
        return process

    git_index_fun.safer_popen = popen_for_hooks
    _applied = True
    return bash_path
