#!/usr/bin/env bash
# PostToolUse tick (owner, 2026-10-24: «Что с агентом, проверяющим других агентов каждые полчаса, сломан?»): the cron
# watchdog fires only while the session is idle, so long busy turns skipped it. Every tool call checks a timestamp;
# once 10 minutes have passed, films.watchdog runs and its report is injected as context — mid-turn too.
cat > /dev/null
root="${CLAUDE_PROJECT_DIR:-.}"
stamp="$root/data/cache/locks/watchdog.last"
now=$(date +%s)
last=$(cat "$stamp" 2>/dev/null || echo 0)
[ $((now - last)) -lt 600 ] && exit 0   # every 10 minutes (owner, 2026-10-25)
echo "$now" > "$stamp"
rep=$(cd "$root" && python -X utf8 -m films.watchdog 2>&1 | head -40)
python -X utf8 -c 'import json,sys; print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Проверка сторожа (раз в 10 мин, правило владельца; у агентов — процент и прогноз окончания): " + sys.argv[1] + "\nЗависшее — SendMessage/TaskStop/перезапуск, запись в PLAN.md, коротко сказать владельцу."}}, ensure_ascii=False))' "$rep"
exit 0
