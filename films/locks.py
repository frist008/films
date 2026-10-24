"""Cross-process semaphores for the critical sections (owner, 2026-10-24: «где есть критические секции — создай семафоры»).

Background jobs of this project run in parallel (marks, trailer searches, rutor, enrichment). Some resources must not
be hit by two of them at once:

  plex_write   — writes to Plex (library + Discover): parallel runs got 429 / 503 and lost marks;
  youtube_http — YouTube search / watch pages: two trailer runs double the request rate (→ 429 / «sorry»);
  google_tv    — the Google TV watchlist (a second loop + a double press = the film removed);
  sheet        — rebuilding the Google Sheet (two writers mix tabs);
  chrome       — Claude in Chrome: one agent at a time (the tab group is shared).

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
