#!/usr/bin/env bash
# PreToolUse guard (owner, 2026-10-25: «видел» — отмечать и в Jellyfin; the Plex rule applies too — never re-mark,
# never un-mark): the only writer to Jellyfin is films/sources/jellyfin_guard.py (skips already played items).
# Blocks: shell commands that write to the Jellyfin API directly (PlayedItems, POST/DELETE to :8096) and edits to the
# guard or this hook — those need the owner's explicit permission in chat.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
ti = d.get("tool_input") or {}

def deny(why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                      "permissionDecisionReason": why}}, ensure_ascii=False))
    sys.exit(0)

if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
    p = (ti.get("file_path") or "").replace("\\", "/")
    if p.endswith("films/sources/jellyfin_guard.py") or p.endswith(".claude/hooks/jellyfin-guard.sh"):
        deny("Предохранитель Jellyfin менять — только с прямого разрешения владельца в чате (владелец, 2026-10-25).")
    sys.exit(0)

if tool in ("Bash", "PowerShell"):
    cmd = ti.get("command") or ""
    if re.search(r"PlayedItems|PlayingItems", cmd) or (re.search(r"8096", cmd) and re.search(
            r"(?i)-X\s*(POST|DELETE)|--data|-Method\s+(Post|Delete)|requests\.(post|delete)|\.post\(|\.delete\(", cmd)):
        deny("Запись в Jellyfin — только через films/sources/jellyfin_guard.py (watched.mark / mark_episode): "
             "уже просмотренное не отмечается повторно, снятие отметок запрещено (владелец, 2026-10-25).")
sys.exit(0)
'
exit 0
