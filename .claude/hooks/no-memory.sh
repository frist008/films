#!/usr/bin/env bash
# PreToolUse (owner, 2026-10-24: «удостоверься, что всё по этому проекту лежит в проекте и не вылезло в глобальные скилы
# или мемори … точно не в мемори проекта, а в скилы или хуки»): no writes to Claude's memory for this project or to global
# skills / CLAUDE.md — project rules go to .claude/skills/*, .claude/hooks/*, CLAUDE.md; personal values to config/private.toml.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
ti = d.get("tool_input") or {}
text = " ".join(str(ti.get(k) or "") for k in ("file_path", "command", "notebook_path"))
p = text.replace("\\", "/").lower()
if re.search(r"\.claude/projects/[^/ ]*films[^/ ]*/memory|\.claude/skills/(?!\.trash)|/\.claude/claude\.md|\.claude/agents/", p) \
        and not re.search(r"work/films/\.claude/", p) \
        and (d.get("tool_name") in ("Write", "Edit", "MultiEdit", "NotebookEdit") or re.search(r"(>|tee|cp |mv |sed -i|Set-Content|Out-File)", text)):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Всё по проекту films — только в проекте: правила → .claude/skills/owner-rules или другой скил / хук / CLAUDE.md, личные значения → config/private.toml. Память Claude и глобальные скилы не использовать (владелец, 2026-10-24)."}}, ensure_ascii=False))
'
exit 0
