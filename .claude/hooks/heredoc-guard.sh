#!/usr/bin/env bash
# PreToolUse guard (owner, 2026-10-25: «если забыл — фикси скилы и хуки»): code with a backslash must be written with
# Edit/Write, never generated through a bash/PowerShell heredoc — the tool call's escaping turns \n into a real line
# break and \b into a backspace byte (broke films/sheet.py, rank.py, disk_map.json, survey-guard.sh). Blocks a heredoc
# command that writes a .py/.js/.sh/.toml/.json project file and contains a backslash.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
if d.get("tool_name") not in ("Bash", "PowerShell"):
    sys.exit(0)
cmd = (d.get("tool_input") or {}).get("command") or ""
heredoc = re.search(r"<<-?\s*[\x27\"]?\w+", cmd) or "@\x27" in cmd
writes_code = re.search(r"(open\([^)]*\.(py|js|sh|toml|json)[\x27\"]|write_text|Set-Content|Out-File|>\s*\S+\.(py|js|sh|toml|json))", cmd)
if heredoc and writes_code and "\\" in cmd:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Код с обратной косой (\\n, \\b, \\d, пути Windows) — только инструментом Edit/Write, "
        "не через heredoc в Bash/PowerShell: экранирование превращает \\n в перенос строки, \\b — в управляющий символ "
        "(сломало sheet.py 2026-10-25). Правило — CLAUDE.md «Команды»."}}, ensure_ascii=False))
sys.exit(0)
'
exit 0
