#!/usr/bin/env bash
# PreToolUse on Chrome JS (owner, 2026-10-26: Jellyfin through the Chrome tab, no API key; the Plex rule applies — never
# re-mark, never un-mark). Any javascript_tool text that touches Jellyfin played state must be exactly
# films/browser/jellyfin_guard.js followed by one `__jfGuard.mark([...], "<user>");` call
# (films.sources.jellyfin_chrome code); un-marking is always denied. Edits to the JS guard or this hook are denied too.
# Tests: tests/test_jellyfin_chrome.py.
input=$(cat)
printf '%s' "$input" | CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}" python -X utf8 -c '
import json, os, re, sys
from pathlib import Path
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
ti = d.get("tool_input") or {}

def deny(why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                      "permissionDecisionReason": why}}, ensure_ascii=False))
    sys.exit(0)

if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
    p = (ti.get("file_path") or "").replace(chr(92), "/")
    if p.endswith("films/browser/jellyfin_guard.js") or p.endswith(".claude/hooks/jellyfin-chrome-guard.sh"):
        deny("Предохранитель Jellyfin (вкладка Chrome) менять — только с прямого разрешения владельца в чате.")
    sys.exit(0)

if not tool.endswith("javascript_tool"):
    sys.exit(0)
text = ti.get("text") or ""
if re.search(r"(?i)markUnplayed|UnplayedItems|PlayedItems[^\n]*DELETE|method\s*:\s*[\"\x27]DELETE", text):
    deny("Снимать отметки «просмотрено» в Jellyfin нельзя (владелец, 2026-10-25).")
if not re.search(r"(?i)markPlayed|PlayedItems|UserData|updateUserItemRating|reportPlayback", text):
    sys.exit(0)
guard = Path(os.environ["CLAUDE_PROJECT_DIR"]) / "films" / "browser" / "jellyfin_guard.js"
src = guard.read_text(encoding="utf-8") if guard.exists() else None
rest = text[len(src):] if src and text.startswith(src) else None
if rest is None or not re.fullmatch(r"\s*__jfGuard\.mark\(\[(\"[0-9a-fA-F-]+\"(,\s*)?)*\],\s*\"[^\"]+\"\);\s*", rest):
    deny("Отметки Jellyfin — только через films/browser/jellyfin_guard.js: текст ровно из "
         "`python -X utf8 -m films.sources.jellyfin_chrome code …` (уже просмотренное не трогает, снимать нельзя).")
sys.exit(0)
'
exit 0
