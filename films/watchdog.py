"""Watchdog of background work (owner, 2026-10-23: «раз в 30 минут проверять, что агенты не зависли»; 2026-10-24:
«Что с агентом, проверяющим других агентов каждые полчаса, сломан?»).

The session cron fires only while the session is idle, so during long busy turns nobody checked. This module is run
by the PostToolUse hook `.claude/hooks/watchdog-tick.sh` at most every 30 minutes, also mid-turn, and by the cron.
It reports: live project Python processes, the age of every log written in the last 12 h, and «STALLED» for a
process whose log has had no new line for 30+ minutes. In-page Chrome loops (playlists, Google TV) keep their progress
in the page — check them with javascript_tool (window.__plRun / window.__gRun), the report reminds to do it.

Run: python -X utf8 -m films.watchdog
"""
from __future__ import annotations

import re
import subprocess
import time

from . import db

LOGS = db.ROOT / "data" / "cache" / "logs"
STALL_MIN = 30


def processes() -> list[str]:
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'films' } | "
          "ForEach-Object { $_.CommandLine }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [l.strip() for l in out.splitlines() if l.strip() and "films.watchdog" not in l]


def report() -> str:
    now = time.time()
    procs = processes()
    lines = [f"Сторож {time.strftime('%H:%M')}: процессов проекта {len(procs)}"]
    for p in procs:
        lines.append("  ▶ " + p[-110:])
    logs = sorted((f for f in LOGS.glob("*.log") if now - f.stat().st_mtime < 12 * 3600), key=lambda f: -f.stat().st_mtime)
    running = " ".join(procs)
    for f in logs:
        age = (now - f.stat().st_mtime) / 60
        stem = f.stem.split("_2026")[0]
        alive = any(re.search(re.escape(k) + r"(?![\w.])|" + re.escape(k) + r"\.py", running)
                    for k in (stem, stem.replace("_", "."), stem.replace("_", " ")))
        flag = " ⚠ ЗАВИС? (процесс жив, лог молчит ≥ 30 мин)" if alive and age >= STALL_MIN else ""
        if alive or age < 180:   # older finished logs are noise
            lines.append(f"  лог {f.name}: {age:.0f} мин назад{flag}")
    # background agents (owner, 2026-10-25: «каждый агент во время отчёта должен прогнозировать время, когда закончит, и
    # процент выполнения»): the last «PROGRESS N% ETA HH:MM — …» line of every data/logs/agent_*.log written today
    for f in sorted((db.ROOT / "data" / "logs").glob("agent_*.log"), key=lambda f: -f.stat().st_mtime):
        age = (now - f.stat().st_mtime) / 60
        if age > 12 * 60:
            continue
        text = f.read_text(encoding="utf-8", errors="replace").splitlines()
        own = "AGENT_" + f.stem.removeprefix("agent_").upper() + "_OK"   # only its own marker, not another agent's quoted
        last = next((l.strip() for l in reversed(text) if l.strip()), "")
        # done: its own marker, or a marker closing its last line (sub-logs write the parent's), or a final «finished …»
        if (any(own in l for l in text[-5:]) or re.search(r"AGENT_\w+_OK\W*$", last)
                or re.match(r"\S+( \S+)? finished\b", last)):
            if age < 60:
                lines.append(f"  агент {f.stem}: закончил ({age:.0f} мин назад)")
            continue
        prog = next((l for l in reversed(text) if "PROGRESS" in l), None)
        stall = " ⚠ ЗАВИС? (лог молчит ≥ 20 мин)" if age >= AGENT_STALL_MIN else ""
        lines.append(f"  агент {f.stem}: {prog.split('PROGRESS', 1)[1].strip()[:120] if prog else 'нет строки PROGRESS (процент и прогноз не пишет)'}"
                     f"; последняя строка {age:.0f} мин назад{stall}")
    lines.append("  Chrome-циклы (плейлисты __plRun, Google TV __gRun) — проверить в вкладке javascript_tool")
    return "\n".join(lines)


AGENT_STALL_MIN = 20   # agents write PROGRESS at least every 10 minutes


if __name__ == "__main__":
    print(report())
