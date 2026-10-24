#!/usr/bin/env bash
# PreToolUse (owner, 2026-10-24: «удостоверься, что всё по этому проекту лежит в проекте и не вылезло в глобальные скилы
# или мемори … точно не в мемори проекта, а в скилы или хуки»): no writes to Claude's memory for this project or to the
# user-level skills / CLAUDE.md / agents — project rules go to .claude/skills/*, .claude/hooks/*, CLAUDE.md (in the repo);
# personal values to config/private.toml. Only the user-level ~/.claude dir is matched, never the project's .claude.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
ti = d.get("tool_input") or {}
text = " ".join(str(ti.get(k) or "") for k in ("file_path", "command", "notebook_path"))
p = text.replace("\\", "/").lower()
home = r"(?:users/[^/ ]+|~|\$home|\$env:userprofile)/\.claude/"
target = re.search(home + r"(?:projects/[^/ ]*films[^/ ]*/memory|skills/(?!\.trash)|claude\.md|agents/)", p)
writes = d.get("tool_name") in ("Write", "Edit", "MultiEdit", "NotebookEdit") or \
    re.search(r"(>|\btee\b|\bcp\b|\bmv\b|sed -i|set-content|out-file|copy-item|move-item)", p)
if target and writes:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Всё по проекту films — только в проекте: правила → .claude/skills/owner-rules или другой скил / хук / CLAUDE.md, личные значения → config/private.toml. Память Claude и глобальные скилы не использовать (владелец, 2026-10-24)."}}, ensure_ascii=False))
'
exit 0
