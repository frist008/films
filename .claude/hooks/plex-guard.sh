#!/usr/bin/env bash
# PreToolUse guard (owner, 2026-10-23): nothing may write to the owner's Plex watch history past films/sources/plex_guard.py.
# Blocks shell commands with raw scrobble / unscrobble / history-removal calls or approval bypasses, and edits of the guard.
input=$(cat)
tool=$(printf '%s' "$input" | python -X utf8 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))" 2>/dev/null)
if [ "$tool" = "Edit" ] || [ "$tool" = "Write" ] || [ "$tool" = "MultiEdit" ] || [ "$tool" = "NotebookEdit" ]; then
  path=$(printf '%s' "$input" | python -X utf8 -c "import sys,json; d=json.load(sys.stdin); print((d.get('tool_input') or {}).get('file_path',''))" 2>/dev/null)
  case "$path" in
    *plex_guard.py|*plex-guard.sh)
      printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"plex_guard защищает историю просмотров владельца в Plex; менять его — только с прямого разрешения владельца в чате (он правит сам или даёт явное «да» на конкретную правку)."}}'
      exit 0;;
  esac
  exit 0
fi
cmd=$(printf '%s' "$input" | python -X utf8 -c "import sys,json; d=json.load(sys.stdin); print((d.get('tool_input') or {}).get('command',''))" 2>/dev/null)
if printf '%s' "$cmd" | grep -Eqi 'scrobble|removeActivity|removeWatchHistory|owner_approved|GuardedSession|requests\.(put|post|get)\([^)]*(32400|plex\.tv)|curl[^|;]*(32400|plex\.tv)|Invoke-(WebRequest|RestMethod)[^|;]*(32400|plex\.tv)|unmark'; then
  printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Запрет (владелец, 2026-10-23): прямые отметки/снятие/удаление истории в Plex из команды. Отмечать только через films.watched (mark / mark_episode / mark_series) — они идут через plex_guard, который не трогает уже просмотренное. Снятие отметок и удаление записей истории — только по явному «да» владельца на конкретный список, отдельной правкой кода."}}'
  exit 0
fi
exit 0
