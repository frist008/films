#!/usr/bin/env bash
# PostToolUse hook: the 30-minute "are agents / background jobs stuck?" check must be armed on EVERY background launch
# (owner, 2026-10-23: CLAUDE.md «Сторож фоновых агентов»; 2026-10-25: «Хук по запуску таймеров на проверку агентов не
# работает? Надо чинить, чтобы он при любом запуске агента опять запускался»).
#
#   CronCreate / CronDelete   → the watchdog cron is remembered / forgotten in data/cache/locks/watchdog_cron.json
#                               (only a job whose prompt starts with «Проверка фоновых агентов»);
#   Agent, or Bash / PowerShell with run_in_background → no remembered cron → BLOCK: arm it now (CronCreate).
# A cron lives only in the session: the marker is ignored when older than 7 days or from another session.
input=$(cat)
printf '%s' "$input" | CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}" python -X utf8 -c '
import json, os, sys, time
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
ti = d.get("tool_input") or {}
tr = d.get("tool_response")
sid = d.get("session_id") or ""
marker = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."), "data", "cache", "locks", "watchdog_cron.json")
PROMPT = "Проверка фоновых агентов"

def armed() -> bool:
    try:
        m = json.load(open(marker, encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return m.get("session") == sid and time.time() - m.get("at", 0) < 7 * 86400

if tool == "CronCreate":
    if (ti.get("prompt") or "").startswith(PROMPT):
        text = tr if isinstance(tr, str) else json.dumps(tr, ensure_ascii=False)
        import re
        m = re.search(r"job ([0-9a-f]{6,})", text or "")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        json.dump({"id": m.group(1) if m else None, "session": sid, "at": time.time()}, open(marker, "w", encoding="utf-8"))
    sys.exit(0)
if tool == "CronDelete":
    try:
        m = json.load(open(marker, encoding="utf-8"))
        if not m.get("id") or m.get("id") == ti.get("id"):
            os.remove(marker)
    except (OSError, ValueError):
        pass
    sys.exit(0)

background = tool == "Agent" or (tool in ("Bash", "PowerShell") and ti.get("run_in_background"))
if background and not armed():
    print(json.dumps({"decision": "block", "reason":
        "Запущен фоновый агент или команда, а таймер сторожа не взведён (правило владельца: при ЛЮБОМ запуске — проверка раз в "
        "30 минут). Сейчас: CronCreate, recurring, cron «13,43 * * * *», prompt начинается с «Проверка фоновых агентов и команд "
        "(правило владельца, раз в 30 мин): хвост data/logs/*.log и tasks/*.output, прогресс с прошлой проверки, зависшее — "
        "перезапуск и запись в PLAN.md; фоновых не осталось — CronDelete этой задачи.»"}, ensure_ascii=False))
sys.exit(0)
'
exit 0
