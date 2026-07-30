"""Make GitPython run Windows git hooks with Git's bash, not the WSL stub.

GitPython cannot rely on a shebang on Windows, so ``git.index.fun.run_commit_hook``
spawns hooks as ``["bash.exe", <hook>]`` and leaves the executable lookup to
Windows. That lookup is the problem: ``CreateProcess`` searches for a bare
executable name in this order --

    application dir -> parent cwd -> System32 -> Windows dir -> PATH

-- so ``C:\\Windows\\System32\\bash.exe`` is found at step 3, before PATH is ever
consulted. On a machine with the WSL launcher stub installed but no distro, that
stub answers every hook invocation with "Windows Subsystem for Linux has no
installed distributions" and exits 1, which GitPython reports as a
``HookExecutionError``. Every commit through this server fails, and the hook body
never runs at all.

Putting Git's ``bin`` directory on PATH does not help, because PATH is consulted
only after System32. The only reliable fix is to name the interpreter by absolute
path, which is what this module does: it patches the single ``safer_popen`` call
site inside ``git.index.fun`` so a bare ``bash.exe`` / ``sh.exe`` argv[0] is
rewritten to Git's own copy.

Fails open. On non-Windows platforms, or when Git's bash cannot be located, the
original GitPython behavior is left untouched.

Usage:
    import hook_shell_fix

    hook_shell_fix.apply()

Windows-safe: ASCII only, no Unicode characters.
"""

import os
import shutil
import sys
from pathlib import Path

_SHELL_NAMES = {"bash.exe", "bash", "sh.exe", "sh"}

_FALLBACK_INSTALL_ROOTS = (
    r"C:\Program Files\Git",
    r"C:\Program Files (x86)\Git",
    r"C:\Git",
)

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


def apply():
    """Patch GitPython so hook spawns name their shell by absolute path.

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

    def popen_with_absolute_shell(command, *args, **kwargs):
        """Rewrite a bare shell name in argv[0] to Git's absolute bash path.

        Args:
            command: Argument vector GitPython wants to spawn.
            *args: Passed through to the wrapped ``safer_popen``.
            **kwargs: Passed through to the wrapped ``safer_popen``.

        Returns:
            The process object returned by the wrapped ``safer_popen``.
        """
        if isinstance(command, (list, tuple)) and command:
            head = str(command[0])
            if head in _SHELL_NAMES:
                command = [bash_path] + list(command[1:])
        return original_popen(command, *args, **kwargs)

    git_index_fun.safer_popen = popen_with_absolute_shell
    _applied = True
    return bash_path
