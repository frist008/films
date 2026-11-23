"""The bash the hooks run in: Git's bash, as Claude Code runs them (2026-11-17, Python 3.15).

subprocess.run(["bash", …]) on Windows searches System32 before PATH and starts WSL's bash.exe — there is no `python`
in WSL, the hook's check cannot start, and a hook that must deny lets everything through: 14 hook tests failed after
the owner moved to Python 3.15 («был обновил питон до 3.15»), while the hooks themselves, run by Claude Code through
Git's bash, kept working."""
import os
import shutil
from pathlib import Path


def _git_bash() -> str:
    if os.name != "nt":
        return "bash"
    cands = [os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    git = shutil.which("git")
    if git:
        g = Path(git).resolve()
        for root in (g.parents[1], g.parents[2] if len(g.parents) > 2 else g.parents[1]):   # cmd\git.exe, mingw64\bin\git.exe
            cands += [str(root / "bin" / "bash.exe"), str(root / "usr" / "bin" / "bash.exe")]
    cands.append(r"C:\Program Files\Git\bin\bash.exe")
    for c in cands:
        if c and Path(c).is_file():
            return c
    raise RuntimeError("Git's bash not found: hook tests would run WSL's bash.exe instead")


BASH = _git_bash()
