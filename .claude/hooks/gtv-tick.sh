#!/usr/bin/env bash
# PostToolUse tick (owner, 2026-10-24: «Ты уже задолбал зависать. Таймер на Google TV каждые 10 минут. Если висим — фикси,
# не жди»): the session cron fires only while idle, so every tool call checks a timestamp; every 10 minutes, while the
# Google TV loop is not finished (data/cache/locks/gtv_loop.active exists), it reminds to check the tab — mid-turn too.
cat > /dev/null
root="${CLAUDE_PROJECT_DIR:-.}"
[ -f "$root/data/cache/locks/gtv_loop.active" ] || exit 0
stamp="$root/data/cache/locks/gtv_tick.last"
now=$(date +%s)
last=$(cat "$stamp" 2>/dev/null || echo 0)
[ $((now - last)) -lt 600 ] && exit 0
echo "$now" > "$stamp"
printf '%s' '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Прошло 10 минут — проверить цикл Google TV СЕЙЧАС (владелец: «если висим — фикси, не жди»): вкладка google.com → __gRun.i / paused / stopped / done, localStorage gtvBeat. Не отвечает / heartbeat > 3 мин / stopped → закрыть вкладку, новая, guard как есть + gtv_loop.js, __gtvLoop(). Закончился → выгрузить результаты, films.gtv_report (отчёт по каждому фильму), удалить data/cache/locks/gtv_loop.active."}}'
exit 0
