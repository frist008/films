#!/usr/bin/env bash
# PreToolUse guard (owner, 2026-10-25: «Ты же сам важных людей не добавлял? Только я могу решать, кто важный»): a new
# [[person]] in config/people.toml must carry a note with the owner's words («владелец …»). Blocks Edit/Write/Bash that
# add a person without such a note.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
ti = d.get("tool_input") or {}
def deny():
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Важных людей решает только владелец (2026-10-25: «Только я могу решать, кто важный»): "
        "у каждого нового [[person]] в config/people.toml — note с его словами («владелец <дата>: «…»»). Без его слов не добавлять."}},
        ensure_ascii=False))
    sys.exit(0)
if tool in ("Edit", "Write", "MultiEdit"):
    if not (ti.get("file_path") or "").replace("\\", "/").endswith("config/people.toml"):
        sys.exit(0)
    text = ti.get("new_string") if tool == "Edit" else ti.get("content") or ""
    for block in (text or "").split("[[person]]")[1:]:
        if "владелец" not in block:
            deny()
elif tool in ("Bash", "PowerShell"):
    cmd = ti.get("command") or ""
    if "people.toml" in cmd and "[[person]]" in cmd and "владелец" not in cmd:
        deny()
sys.exit(0)
'
exit 0
