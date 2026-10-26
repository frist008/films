"""Cross-process semaphores for the critical sections (owner, 2026-10-24: «где есть критические секции — создай семафоры»).

Background jobs of this project run in parallel (marks, trailer searches, rutor, enrichment). Some resources must not
be hit by two of them at once:

  plex_write   — writes to Plex (library + Discover): parallel runs got 429 / 503 and lost marks;
  youtube_http — YouTube search / watch pages: two trailer runs double the request rate (→ 429 / «sorry»);
  google_tv    — the Google TV watchlist (a second loop + a double press = the film removed);
  sheet        — rebuilding the Google Sheet (two writers mix tabs);
  chrome       — Claude in Chrome: one agent at a time (the tab group is shared);
  git          — the repository: index, commits, push (owner, 2026-10-26: «правило которое будет блокировать обращение к
                 гиту вот так параллельно и чтобы не было гонок» — two background pushes raced: «cannot lock ref
                 'refs/heads/main'»). Every git write goes through `python -m films.locks run git -- git …` (hook
                 .claude/hooks/git-guard.sh blocks a bare one);
  match        — `films match` rewrites release → title links (two at once fight over the same rows);
  rutracker_run — `rutracker queries / resume` rewrite queries_run.json / run.js that the bridge serves;
  backup       — `films.backup export` rewrites backup/ (read by the commit that follows: take it inside git).

CLI: `python -X utf8 -m films.locks run NAME [--timeout S] -- CMD …` runs CMD holding NAME (waits for it, exit code
passed through); `python -X utf8 -m films.locks status` — which locks are held, by whom.

`lock(name)` — exclusive section (a file created with O_EXCL; a lock left by a dead process is taken over);
`pace(name, seconds)` — a shared minimum interval between calls of all processes (a timestamp file under a lock).
Files: data/cache/locks/<name>.lock / <name>.last
"""
from __future__ import annotations

import contextlib
import json
import os
import time

from . import db

DIR = db.ROOT / "data" / "cache" / "locks"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def lock(name: str, timeout: float = 3600, poll: float = 0.5):
    DIR.mkdir(parents=True, exist_ok=True)
    path = DIR / f"{name}.lock"
    start = time.time()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time()}).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                holder = json.loads(path.read_text() or "{}")
            except (OSError, ValueError):
                holder = {}
            if int(holder.get("pid") or 0) == os.getpid():   # re-entered by the same process: already ours
                yield
                return
            if not _alive(int(holder.get("pid") or 0)):
                with contextlib.suppress(OSError):
                    path.unlink()          # a dead process left it
                continue
            if time.time() - start > timeout:
                raise TimeoutError(f"lock {name} held by pid {holder.get('pid')}")
            time.sleep(poll)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def held() -> list[dict]:
    """Locks held right now by a live process."""
    out = []
    for p in sorted(DIR.glob("*.lock")) if DIR.exists() else []:
        try:
            h = json.loads(p.read_text() or "{}")
        except (OSError, ValueError):
            h = {}
        if _alive(int(h.get("pid") or 0)):
            out.append({"name": p.stem, "pid": h.get("pid"), "since": time.strftime("%H:%M:%S", time.localtime(h.get("at", 0))),
                        "cmd": h.get("cmd")})
    return out


def run(name: str, cmd: list[str], timeout: float = 3600) -> int:
    """Run `cmd` holding `name` (the holder file records the command, for `status`)."""
    import subprocess
    with lock(name, timeout=timeout):
        path = DIR / f"{name}.lock"
        with contextlib.suppress(OSError, ValueError):
            h = json.loads(path.read_text() or "{}")
            path.write_text(json.dumps(dict(h, cmd=" ".join(cmd)[:200])))
        return subprocess.run(cmd).returncode


def pace(name: str, seconds: float) -> None:
    """Wait so that calls of `name` from all processes are at least `seconds` apart."""
    DIR.mkdir(parents=True, exist_ok=True)
    last = DIR / f"{name}.last"
    with lock(name + "_pace", timeout=600, poll=0.05):
        try:
            prev = float(last.read_text() or 0)
        except (OSError, ValueError):
            prev = 0.0
        wait = prev + seconds - time.time()
        if wait > 0:
            time.sleep(wait)
        last.write_text(str(time.time()))


if __name__ == "__main__":
    import sys
    a = sys.argv[1:]
    if a[:1] == ["run"] and "--" in a:
        sep = a.index("--")
        name = a[1]
        t = float(a[a.index("--timeout") + 1]) if "--timeout" in a[:sep] else 3600
        sys.exit(run(name, a[sep + 1:], timeout=t))
    if a[:1] == ["status"]:
        print(json.dumps(held(), ensure_ascii=False, indent=1))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
