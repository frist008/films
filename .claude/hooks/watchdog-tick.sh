#!/usr/bin/env bash
# PostToolUse tick (owner, 2026-10-24: «Что с агентом, проверяющим других агентов каждые полчаса, сломан?»): the cron
# watchdog fires only while the session is idle, so long busy turns skipped it. Every tool call checks a timestamp;
# once 10 minutes have passed, films.watchdog runs: the full report goes into Claude's context, and a short one
# (agents: percent, ETA, step, stalls) is shown to the OWNER as a system message (owner, 2026-10-25: «Не вижу прогресса
# каждые 10 минут от агента, который должен опрашивать. Опять баги?» — before, only Claude saw it and did not relay it).
cat > /dev/null
root="${CLAUDE_PROJECT_DIR:-.}"
stamp="$root/data/cache/locks/watchdog.last"
now=$(date +%s)
last=$(cat "$stamp" 2>/dev/null || echo 0)
[ $((now - last)) -lt 1800 ] && exit 0   # every 30 minutes (owner, 2026-10-26: «можешь таймер начинать делать раз в пол часа опять раз всё ок»; was 10, 2026-10-25)
echo "$now" > "$stamp"
rep=$(cd "$root" && python -X utf8 -m films.watchdog 2>&1 | head -40)
own=$(cd "$root" && python -X utf8 -m films.watchdog --table 2>&1 | head -15)  # table: who, what, done, left, finishes, state (2026-10-26)
printf '%s\n' "$own" >> "$root/data/logs/watchdog_owner.log"
python -X utf8 -c 'import json,sys; print(json.dumps({"systemMessage": sys.argv[2], "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Проверка сторожа (раз в 10 мин, правило владельца; у агентов — процент и прогноз окончания): " + sys.argv[1] + "\nВладельцу короткий отчёт показан системным сообщением; в следующем своём сообщении повтори его одной-двумя строками. Зависшее — SendMessage/TaskStop/перезапуск, запись в PLAN.md."}}, ensure_ascii=False))' "$rep" "$own"
exit 0
