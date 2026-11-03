#!/usr/bin/env bash
# PreToolUse guard for SendUserFile / AskUserQuestion (owner, 2026-11-02: «4 картинки, 4 вопроса, зачем ты мне накидал сразу
# всё? у меня не такой высокий монитор. запомни. Это баг который нельзя повторять» — 15 pictures were sent at once before
# 4 questions). Pictures go in batches for the question call that follows: at most 4 files between two AskUserQuestion
# calls; AskUserQuestion resets the count. State: data/cache/locks/pictures_since_question.json. Tests:
# tests/test_picture_guard.py.
input=$(cat)
printf '%s' "$input" | CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}" python -X utf8 -c '
import json, os, sys
from pathlib import Path
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
state = Path(os.environ["CLAUDE_PROJECT_DIR"]) / "data" / "cache" / "locks" / "pictures_since_question.json"
LIMIT = 4
if tool == "AskUserQuestion":
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"sent": 0}), encoding="utf-8")
    sys.exit(0)
if tool != "SendUserFile":
    sys.exit(0)
files = (d.get("tool_input") or {}).get("files") or []
try:
    sent = json.loads(state.read_text(encoding="utf-8")).get("sent", 0)
except (OSError, ValueError):
    sent = 0
if sent + len(files) > LIMIT:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"Не больше {LIMIT} картинок до следующего вопроса (владелец, 2026-11-02: «4 картинки, "
        f"4 вопроса, зачем ты мне накидал сразу всё? у меня не такой высокий монитор»): уже отправлено {sent}, сейчас "
        f"{len(files)}. Картинки — только к ближайшему вызову AskUserQuestion, по одной на вопрос."}}, ensure_ascii=False))
    sys.exit(0)
state.parent.mkdir(parents=True, exist_ok=True)
state.write_text(json.dumps({"sent": sent + len(files)}), encoding="utf-8")
'
exit 0
