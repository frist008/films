#!/usr/bin/env bash
# PreToolUse guard (owner, 2026-10-24): YouTube trailer playlists get only what `python -m films.playlist_notes desired`
# built and checked (no series, nothing watched / downloaded / queued, only what the sheet shows, no rejected videos).
# - loading the target list into the page (`window.__want = …`) must be exactly data/cache/playlists_desired.js, < 2 h old;
# - code that adds videos (ACTION_ADD_VIDEO, playlist/create) must not carry literal video ids — only __want.
input=$(cat)
printf '%s' "$input" | CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}" python -X utf8 -c '
import json, os, re, sys, time
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
if not (tool.startswith("mcp__claude-in-chrome__") or tool.startswith("mcp__Claude_Browser__")):
    sys.exit(0)
def texts(t, x):
    if t.endswith("browser_batch"):
        for a in x.get("actions") or []:
            yield from texts(a.get("name", ""), a.get("input") or {})
        return
    if t.endswith("javascript_tool"):
        yield x.get("text") or ""
def deny(why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": why}}, ensure_ascii=False))
    sys.exit(0)
f = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."), "data", "cache", "playlists_desired.js")
for text in texts(tool, d.get("tool_input") or {}):
    if re.search(r"window\.__want\s*=(?!=)|__want\.\w+\s*=(?!=)|__want\[[^\]]*\]\s*=(?!=)|__want\.\w+\.(push|splice|unshift)", text):
        try:
            ok = open(f, encoding="utf-8").read().strip() == text.strip() and time.time() - os.path.getmtime(f) < 7200
        except OSError:
            ok = False
        if not ok:
            deny("Состав плейлистов — только из `python -X utf8 -m films.playlist_notes desired` (проверены правила владельца: без сериалов, "
                 "просмотренного, скачанного/в очереди, скрытого фильтром таблицы; франшизы рядом). Загрузить ровно data/cache/playlists_desired.js (свежий, < 2 ч).")
    if re.search(r"ACTION_ADD_VIDEO|playlist/create|addedVideoId", text) and re.search(r"[\"\x27][\w-]{11}[\"\x27]", text):
        deny("Добавлять ролики в плейлисты можно только из __want (проверенный список), без вписанных вручную id.")
sys.exit(0)
'
exit 0
