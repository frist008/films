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
from pathlib import Path

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
        lines.append("  ▶ " + short_cmd(p))
    logs = sorted((f for f in LOGS.glob("*.log") if now - f.stat().st_mtime < 12 * 3600), key=lambda f: -f.stat().st_mtime)
    running = " ".join(procs)
    for f in logs:
        age = (now - f.stat().st_mtime) / 60
        stem = f.stem.split("_2026")[0]
        alive = any(re.search(re.escape(k) + r"(?![\w.])|" + re.escape(k) + r"\.py", running)
                    for k in (stem, stem.replace("_", "."), stem.replace("_", " ")))
        quiet_ok = stem in QUIET_LOGS   # the bridge writes only when it receives: silence is idling, not a stall
        flag = " ⚠ ЗАВИС? (процесс жив, лог молчит ≥ 30 мин)" if alive and age >= STALL_MIN and not quiet_ok else ""
        if alive or age < 180:   # older finished logs are noise
            lines.append(f"  лог {f.name}: {age:.0f} мин назад{flag}")
    # background agents (owner, 2026-10-25: «каждый агент во время отчёта должен прогнозировать время, когда закончит, и
    # процент выполнения»): the last «PROGRESS N% ETA HH:MM — …» line of every data/logs/agent_*.log written today
    for f in sorted((db.ROOT / "data" / "logs").glob("agent_*.log"), key=lambda f: -f.stat().st_mtime):
        age = (now - f.stat().st_mtime) / 60
        if age > 12 * 60:
            continue
        text = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if finished(text, f.stem):
            if age < 60:
                lines.append(f"  агент {f.stem}: " + ("⚠ УПАЛ (Traceback / FAILED в последнем прогоне) — разобраться, "
                                                      "перезапустить" if crashed(text) else "закончил")
                             + f" ({age:.0f} мин назад)")
            continue
        prog = next((l for l in reversed(text) if "PROGRESS" in l), None)
        if c := counter_after_progress(text):   # a counter line newer than PROGRESS (old code, 2026-10-28)
            prog = f"PROGRESS {100 * c[0] // c[1]}% — {c[0]}/{c[1]} (строка счётчика)"
        stall =" ⚠ ЗАВИС? (лог молчит ≥ 20 мин)" if age >= AGENT_STALL_MIN else ""
        if not stall and not counter_after_progress(text) and (since := no_movement_min(text)) is not None:
            stall = f" ⚠ НЕТ ДВИЖЕНИЯ {since} мин (процент и счётчики те же) — написать агенту сразу"
        lines.append(f"  агент {f.stem}: {prog.split('PROGRESS', 1)[1].strip()[:120] if prog else 'нет строки PROGRESS (процент и прогноз не пишет)'}"
                     f"; последняя строка {age:.0f} мин назад{stall}")
    lines.append("  Chrome-циклы (плейлисты __plRun, Google TV __gRun) — проверить в вкладке javascript_tool")
    return "\n".join(lines)


QUIET_LOGS = {"bridge"}   # review 2026-10-28: «bridge.log … ⚠ ЗАВИС?» to the owner while the bridge only waited


def short_cmd(cmd: str, width: int = 110) -> str:
    """A process line by its start — the module and first arguments (review 2026-10-28: the tail of «… trailers --ids
    41,12846,…» showed only ids)."""
    m = re.search(r"-m\s+(\S+)(.*)", cmd)
    py = re.search(r"""["']?([^\s"']+\.py)["']?(.*)""", cmd)   # a script: its file name and arguments (review 2026-10-28)
    s = (m.group(1) + m.group(2)) if m else (re.split(r"[\\/]", py.group(1))[-1] + py.group(2)) if py else cmd
    s = " ".join(s.split())
    return s if len(s) <= width else s[:width - 1] + "…"


AGENT_STALL_MIN = 20   # agents write PROGRESS at least every 10 minutes
NO_MOVE_MIN = 10       # owner, 2026-10-25: «Пиши сразу, 10 минут и 0 % движения — это уже плохо»


def finished(lines: list[str], stem: str) -> bool:
    """Done: its own marker, or a marker closing its last line (sub-logs write the parent's), or a final «finished …».
    A marker counts only if no PROGRESS line follows it: a log reused by a new run (agent_rutracker.log, 2026-10-26) keeps
    the old run's marker a few lines up, and the new run was shown as «закончил»."""
    own = "AGENT_" + stem.removeprefix("agent_").upper() + "_"   # only its own marker, not another agent's quoted
    last = next((l.strip() for l in reversed(lines) if l.strip()), "")
    if re.search(r"AGENT_\w+_(?:OK|FAILED)\W*$", last) or re.match(r"\S+( \S+)? finished\b", last):
        return True
    for l in reversed(lines[-5:]):
        if re.search(re.escape(own) + r"(?:OK|FAILED)\b", l):
            return True
        if "PROGRESS" in l:
            return False
    return False


def crashed(lines: list[str]) -> bool:
    """The last run (after the previous run's marker) has a Traceback or a _FAILED marker (review 2026-10-28: the
    trailer chain's `originals` step crashed, the chain joined its steps with «;» and still wrote AGENT_TRAILERS2_OK)."""
    # a marker closes its line; «… (AGENT_TRAILERS3_FAILED не ставлю: остановлен намеренно)» is a note, not one (review
    # round 10: the stopped chain was shown as «упал»)
    marker = lambda kinds, l: re.search(rf"AGENT_\w+_(?:{kinds})\W*$", l.strip())
    ends = [i for i, l in enumerate(lines) if marker("OK|FAILED", l)]
    start = ends[-2] + 1 if len(ends) >= 2 else 0
    return any(l.startswith("Traceback") or marker("FAILED", l) for l in lines[start:])


def counter_after_progress(lines: list[str]) -> tuple[int, int, float | None] | None:
    """(N, M, elapsed minutes or None) of a plain counter line («  250/2029 acc 18 … 38 min») newer than the last
    PROGRESS line, else None."""
    for l in reversed(lines):
        if "PROGRESS" in l or re.search(r"AGENT_\w+_OK", l):
            return None
        if m := re.match(r"\s*(?:[^\W\d]+\s*:?\s*)?(\d+)/(\d+)\b", l):   # «  250/2029 …», «  pages 500/1050»
            n, total = int(m.group(1)), int(m.group(2))
            if 0 < n <= total:
                el = re.search(r"\b(\d+(?:\.\d+)?)\s*min\b", l)
                return n, total, float(el.group(1)) if el else None
    return None


def no_movement_min(lines: list[str], now: float | None = None) -> int | None:
    """Minutes the agent's progress has stood still: the percent and every «N/M» counter of the newest PROGRESS line are
    the same as in all PROGRESS lines of the last NO_MOVE_MIN+ minutes (a fresh line with the same numbers is not
    progress). None when it moved, or when there is no PROGRESS line old enough to compare."""
    now = now or time.time()
    progs = []
    for l in lines:
        if re.search(r"AGENT_\w+_OK", l):   # a finished run: a reused log's new run is compared only with itself
            progs = []                       # (2026-10-28: «НЕТ ДВИЖЕНИЯ 154 мин» from the morning run's 100 %)
            continue
        m = re.match(r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)\S*\s+PROGRESS\s+(.*)", l)
        if m:
            try:
                ts = time.mktime(time.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                continue
            text = m.group(2)
            pct = re.findall(r"\d+%", text.split("ETA")[0])        # the percent, not times or other numbers
            progs.append((ts, tuple(pct + re.findall(r"\d+/\d+", text))))
    if not progs:
        return None
    last = progs[-1][1]
    start = progs[-1][0]
    for ts, nums in reversed(progs):
        if nums != last:
            break
        start = ts
    still = (now - start) / 60   # since the numbers last changed (or since the first report, if they never did)
    return int(still) if still >= NO_MOVE_MIN else None


def owner_report() -> str:
    """Short report for the owner himself, shown by the hook as a system message every 10 minutes (owner, 2026-10-25:
    «Не вижу прогресса каждые 10 минут от агента, который должен опрашивать. Опять баги?» — the full report went only into
    Claude's context and was not relayed). Running agents: percent, ETA, current step; stalled ones flagged."""
    full = report().splitlines()
    agents = [l.strip().removeprefix("агент ").removeprefix("agent_") for l in full if l.strip().startswith("агент ")]
    running = [a for a in agents if "закончил" not in a]
    done = [a.split(":")[0] for a in agents if "закончил" in a]
    procs = sum(1 for l in full if l.strip().startswith("▶"))
    head = f"Сторож {time.strftime('%H:%M')} (каждые 10 мин): "
    if not running:
        return head + f"фоновых агентов нет; процессов проекта {procs}" + (f"; недавно закончили: {', '.join(done)}" if done else "")
    lines = [head + f"работают {len(running)}, процессов {procs}"]
    lines += ["• " + a for a in running]
    if done:
        lines.append("закончили: " + ", ".join(done))
    return "\n".join(lines)


def owner_table(now: float | None = None, logs: Path | None = None) -> str:
    """Markdown table for the owner's message (owner, 2026-10-26: «ни таймеров которые проверяжют бекграунд агентов и
    сообщающих таблицей что они делают и сколько всего осталось»): one row per agent log of the last 12 h —
    who, what it is doing, done, left, finishes, state (работает / закончил / ЗАВИС? / НЕТ ДВИЖЕНИЯ N мин)."""
    now = now or time.time()
    rows = []
    for f in sorted((logs or db.ROOT / "data" / "logs").glob("agent_*.log"), key=lambda f: -f.stat().st_mtime):
        age = (now - f.stat().st_mtime) / 60
        if age > 12 * 60:
            continue
        text = f.read_text(encoding="utf-8", errors="replace").splitlines()
        who = f.stem.removeprefix("agent_")
        prog = next((l for l in reversed(text) if "PROGRESS" in l), "")
        m = re.search(r"PROGRESS\s+(\d+)%\s*(?:ETA\s+(\S+))?\s*[—-]?\s*(.*)", prog)
        pct, eta, step = (m.group(1), m.group(2) or "?", m.group(3).strip()) if m else ("?", "?", "нет строки PROGRESS")
        if c := counter_after_progress(text):
            # 2026-10-28: the trailer search on old code wrote only «  250/2029 acc … 38 min» — the owner saw «0 %, ?»
            pct, step = str(100 * c[0] // c[1]), f"{c[0]}/{c[1]} (строка счётчика, без PROGRESS)"
            if len(c) > 2 and c[2]:          # the elapsed minutes of the counter line → a forecast
                eta = time.strftime("%H:%M", time.localtime(f.stat().st_mtime + c[2] / c[0] * (c[1] - c[0]) * 60))
        cnt = re.search(r"(\d+)/(\d+)", step)
        left = f"{int(cnt.group(2)) - int(cnt.group(1))} из {cnt.group(2)}" if cnt else (f"{100 - int(pct)}%" if pct != "?" else "?")
        if finished(text, f.stem):
            if age >= 60:
                continue
            state, left, eta = ("⚠ упал (Traceback)" if crashed(text) else "закончил"), "0", f"{age:.0f} мин назад"
        elif age >= AGENT_STALL_MIN:
            state = f"⚠ ЗАВИС? (лог молчит {age:.0f} мин)"
        elif not c and (since := no_movement_min(text, now)) is not None:
            state = f"⚠ НЕТ ДВИЖЕНИЯ {since} мин"
        else:
            state = "работает"
        rows.append(f"| {who} | {step[:70].replace('|', '/')} | {pct}% | {left} | {eta} | {state} |")
    if not rows:
        return f"Сторож {time.strftime('%H:%M')}: фоновых агентов нет."
    return "\n".join([f"Сторож {time.strftime('%H:%M')} (проверка каждые 30 минут):", "",
                      "| Кто | Что делает | Готово | Осталось | Закончит | Состояние |", "|---|---|---|---|---|---|", *rows])


def wait_finished(stem: str, poll_s: float = 120, logs: Path | None = None, beat: str | None = None) -> None:
    """Block until data/logs/<stem>.log is finished() — its LAST run's marker (2026-10-28: a chain waiting with
    `grep -q AGENT_TRAILERS_OK` saw the morning run's marker in the reused log and started two hours early). beat: the
    waiting chain's own log gets a PROGRESS line every poll (a silent wait was a «ЗАВИС?» for the owner)."""
    d = logs or db.ROOT / "data" / "logs"
    f = d / f"{stem}.log"
    while True:
        lines = []
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            if finished(lines, stem):
                return
        except OSError:
            pass
        if beat:   # the awaited job's counter in the line: it moves, so no «НЕТ ДВИЖЕНИЯ» while the other one works
            c = counter_after_progress(lines)
            with open(d / f"{beat}.log", "a", encoding="utf-8") as b:
                b.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" PROGRESS 0% ETA ? — ждёт конца {stem}"
                        + (f" ({c[0]}/{c[1]})" if c else "") + "\n")
        time.sleep(poll_s)


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--wait"]:   # python -X utf8 -m films.watchdog --wait agent_trailers [agent_trailers2]
        wait_finished(sys.argv[2], beat=sys.argv[3] if len(sys.argv) > 3 else None)
        sys.exit(0)
    print(owner_table() if "--table" in sys.argv else owner_report() if "--owner" in sys.argv else report())
