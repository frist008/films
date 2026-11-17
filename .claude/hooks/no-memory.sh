#!/usr/bin/env bash
# PreToolUse (owner, 2026-10-24: «удостоверься, что всё по этому проекту лежит в проекте и не вылезло в глобальные скилы
# или мемори … точно не в мемори проекта, а в скилы или хуки»; 2026-10-26: «в мемори глобальной ничего не должно быть что
# касается этого проекта! создать хук запрещающий этому проекту создавать глобальные скилы или хуки»): no writes to
# Claude's memory for this project or to any user-level Claude dir — skills, hooks, agents, commands, CLAUDE.md,
# settings.json. Project rules go to .claude/skills/*, .claude/hooks/*, CLAUDE.md (in the repo); personal values to
# config/private.toml. User-level = a .claude dir under the home folder or a profile in AppData (this account's memory
# lives in AppData/Roaming/MultiDeskClaude-Profiles/<profile>/.claude — the 2026-10-24 version missed it); the project's
# own .claude (D:/work/films/.claude or a relative .claude/…) is never matched. Tests: tests/test_hooks_syntax.py.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
ti = d.get("tool_input") or {}
text = " ".join(str(ti.get(k) or "") for k in ("file_path", "command", "notebook_path"))
p = text.replace("\\\\", "/").replace("\\", "/").lower()
sub = r"(?:skills/(?!\.trash)|hooks/|agents/|commands/|claude\.md|settings(?:\.local)?\.json|projects/[^/\s\"]*films[^/\s\"]*/memory)"
user_level = r"(?:~|\$home|\$\{home\}|\$env:userprofile|\$env:appdata|%userprofile%|%appdata%|users/[^/\s\"]+)"
target = None
for m in re.finditer(r"([^\s\"\x27<>|;]*?)\.claude/" + sub, p):
    pre = m.group(1)
    if re.match(r"(?:[a-z]:|/[a-z])/work/films/", pre):
        continue                        # the project itself
    if re.search(user_level, pre) or "appdata" in pre or "multideskclaude" in pre:
        target = m.group(0)
        break
writes = d.get("tool_name") in ("Write", "Edit", "MultiEdit", "NotebookEdit") or re.search(
    r"(>|\btee\b|\bcp\b|\bmv\b|\bln\b|sed -i|\bmkdir\b|set-content|add-content|out-file|copy-item|move-item|new-item|"
    r"rename-item|\.write|json\.dump|writealltext)", p)
if target and writes:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Всё по проекту films — только в проекте: правила → .claude/skills/* / .claude/hooks/* / CLAUDE.md, личные значения → config/private.toml. Глобальные скилы, хуки, агенты, команды, settings и память Claude не использовать (владелец, 2026-10-24 и 2026-10-26). Цель: " + target}}, ensure_ascii=False))
'
exit 0
