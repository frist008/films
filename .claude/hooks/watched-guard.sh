#!/usr/bin/env bash
# PreToolUse guard on Bash/PowerShell (owner, 2026-10-25: «Почему без отметки в Plex?», «Баг скила, чтобы ты не забыл про
# Plex», «И про Google TV и фильмы же ещё нельзя забывать»): the owner's «видел» never goes into the DB by hand-written SQL.
# It goes through films.watched (mark / mark_series): DB + Plex (plex_guard: never re-marks) + Google TV queue for films.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
cmd = (d.get("tool_input") or {}).get("command") or ""
if re.search(r"insert\s+(or\s+\w+\s+)?into\s+watched\b", cmd, re.I) and "manual_partial" not in cmd:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "«Видел» владельца — только через films.watched: `python -X utf8 -m films watched mark <title_id>` "
        "(фильм) или `watched.mark_series(conn, tmdb_tv, seasons)` (сезоны) — это база на компьютере + Plex (предохранитель не "
        "отмечает повторно) + фильм в очередь «Хочу посмотреть» Google TV (скил mark-watched). Не SQL вручную (владелец, 2026-10-25)."}},
        ensure_ascii=False))
sys.exit(0)
'
exit 0
