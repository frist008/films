#!/usr/bin/env bash
# PreToolUse guard (owner, 2026-10-24): «не надо, как в Плексе, сбрасывать то, что было добавлено по дате».
# The Google TV watchlist «Хочу посмотреть» button toggles — pressing it on a listed film removes it and loses its date.
# Only films/browser/gtv_watchlist_guard.js (loaded verbatim) may press it, and only via __gtvGuard.add / .check calls.
# Blocks: other browser JS / clicks touching the watchlist button, redefining the guard, editing the guard or this hook.
input=$(cat)
printf '%s' "$input" | CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}" python -X utf8 -c '
import json, os, re, sys
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
ti = d.get("tool_input") or {}
root = os.environ.get("CLAUDE_PROJECT_DIR", ".")
GUARD = os.path.join(root, "films", "browser", "gtv_watchlist_guard.js")

def deny(why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                      "permissionDecisionReason": why}}, ensure_ascii=False))
    sys.exit(0)

if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
    p = (ti.get("file_path") or "").replace("\\", "/")
    if p.endswith("films/browser/gtv_watchlist_guard.js") or p.endswith(".claude/hooks/gtv-guard.sh"):
        deny("Предохранитель списка Google TV менять — только с прямого разрешения владельца в чате (владелец, 2026-10-24).")
    sys.exit(0)

if not (tool.startswith("mcp__claude-in-chrome__") or tool.startswith("mcp__Claude_Browser__")):
    sys.exit(0)

try:
    guard_src = open(GUARD, encoding="utf-8").read().strip()
except OSError:
    guard_src = None

def scripts(t, x):
    """(tool, js text, click summary) for the call and every browser_batch item."""
    if t.endswith("browser_batch"):
        for a in x.get("actions") or []:
            yield from scripts(a.get("name", ""), a.get("input") or {})
        return
    yield t, x.get("text") or "", x.get("action_summary") or "", x.get("action") or ""

TOUCH = re.compile(r"Хочу посмотреть|aria-pressed|cJyZJd|watchlist|список просмотра|Просмотрено|interests/saved|TdUNyc", re.I)
ACT = re.compile(r"\.click\s*\(|dispatchEvent|MouseEvent|PointerEvent|KeyboardEvent|\.submit\s*\(|removeActivity|delete|remove\w*\(", re.I)
for t, text, summary, action in scripts(tool, ti):
    if t.endswith("javascript_tool"):
        if guard_src is not None and text.strip() == guard_src:
            continue                                   # the vetted guard, loaded verbatim
        if re.search(r"__gtvGuard\s*=|__gtvGuard\s*\[|Object\.defineProperty\([^)]*__gtvGuard", text):
            deny("Переопределять __gtvGuard нельзя: загружать только films/browser/gtv_watchlist_guard.js как есть.")
        if TOUCH.search(text) and ACT.search(text):
            deny("Запрет (владелец, 2026-10-24): кнопку списка Google TV трогать только через __gtvGuard.add (нажимает лишь при aria-pressed=false, "
                 "уже добавленное не трогает и не переставляет даты). Удалять из списка нельзя.")
        if "__gtvGuard" in text and ACT.search(re.sub(r"__gtvGuard\.(add|check)\s*\(", "", text)):
            deny("Вызовы __gtvGuard — без собственных кликов/событий/удалений рядом.")
    if action in ("left_click", "right_click", "double_click", "triple_click", "key", "type") and \
            re.search(r"Хочу посмотреть|список просмотра|watchlist|желаем|Просмотрено|Удалить из", summary, re.I):
        deny("Клики по списку Google TV мышью запрещены — только __gtvGuard.add (владелец, 2026-10-24).")
sys.exit(0)
'
exit 0
