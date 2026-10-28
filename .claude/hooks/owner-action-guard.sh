#!/usr/bin/env bash
# PreToolUse (owner, 2026-10-26: «ты пишешь в правила ничего никогда что я что-то делаю, создавай хук, это запрещено писать
# без моего разрешения и то что где-то сейчас есть удалить. потому что это всё твоя выдумка»; «не надо на меня взвешивать
# скилами то что я не разрешаю»): the rules (CLAUDE.md, .claude/skills/**, docs/**) never say that the OWNER does
# something — logs in, presses, pastes, copies, creates, enters, opens, confirms — unless the same text carries his
# permission: «владелец разрешил YYYY-MM-DD: «…»». What Claude does and the owner's own quoted decisions stay allowed.
# Checked: Edit / Write / MultiEdit text, and Bash / PowerShell commands that write those files.
# Tests: tests/test_owner_action_guard.py.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
tool = d.get("tool_name", "")
ti = d.get("tool_input") or {}
RULES = re.compile(r"(^|[/\\])(CLAUDE\.md|\.claude[/\\]skills[/\\].+\.md|docs[/\\].+\.md)$", re.I)
# verbs only (not nouns like «логины владельца»; «владелец сам их просил» — his decision — has no such verb)
VERB = (r"(жм[её]т|нажима[её]т|нажм[её]т|вставля[её]т|встав(?:ит|ил|ляет)|копиру[её]т|скопиру[её]т|открыва[её]т|откро[её]т|"
        r"входит|войд[её]т|логинится|залогинится|созда[её]т|создаст|вводит|введ[её]т|вписыва[её]т|подтвержда[её]т|"
        r"разреша[её]т|регистрируется|запуска[её]т|кликает|проходит|выбира[её]т)")
OWNER = r"(владел\w*|хозяин\w*)"
pat = re.compile(r"(?i)\b" + OWNER + r"\s+(?:сам\s+)?" + VERB + r"|\b" + VERB + r"\s+(?:сам\s+)?" + OWNER +
                 r"|\b(?:делает|делают)\s+" + OWNER + r"|" + OWNER + r"\s+у\s+компьютера|«Allow»\s+" + OWNER +
                 r"|" + OWNER + r"(?:у|ю)\s*(?:—|-|:)?\s*(?:нужно|надо|нажать|вставить|войти|открыть|создать|вписать|ввести)")
ALLOWED = re.compile(r"владелец\s+разрешил\s+\d{4}-\d\d-\d\d", re.I)

def texts():
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        fp = str(ti.get("file_path") or "")
        if not RULES.search(fp.replace("\\", "/")):
            return
        if tool == "Edit":
            yield ti.get("new_string") or ""
        elif tool == "Write":
            yield ti.get("content") or ""
        elif tool == "MultiEdit":
            for e in ti.get("edits") or []:
                yield e.get("new_string") or ""
    elif tool in ("Bash", "PowerShell"):
        cmd = str(ti.get("command") or "")
        if re.search(r"CLAUDE\.md|\.claude[/\\]skills|docs[/\\]", cmd) and re.search(r">|tee|sed -i|write_text|\.write\(|Set-Content|Out-File|Add-Content", cmd):
            yield cmd

for t in texts():
    for line in str(t).splitlines():
        m = pat.search(line)
        if m and not ALLOWED.search(line):
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "В правила (CLAUDE.md, скилы, docs) не писать, что владелец что-то делает, без его разрешения "
                "(владелец 2026-10-26: «ты пишешь в правила ничего никогда что я что-то делаю … это запрещено писать без моего разрешения»). "
                "Пиши, что делает Claude; если владелец сам разрешил — в той же строке «владелец разрешил ГГГГ-ММ-ДД: «его слова»». "
                "Найдено: «" + m.group(0) + "» в строке: " + line.strip()[:120]}}, ensure_ascii=False))
            sys.exit(0)
'
exit 0
