#!/usr/bin/env bash
# PreToolUse (owner, 2026-10-26: «Запиши себе правило которое будет блокировать обращение к гиту вот так параллельно и
# чтобы не было гонок. Тебе надо понять критические области и не пытаться с ними работать параллельно и придумать
# семафоры» — two background pushes raced at 03:00: «cannot lock ref 'refs/heads/main'»). Every git command that writes
# (add, commit, push, merge, rebase, reset, checkout/switch, stash, pull, fetch, tag, cherry-pick, revert, rm, mv, gc)
# must run under the `git` semaphore: `python -X utf8 -m films.locks run git -- git …` (waits for the other one). A bare
# one is denied. Read-only git (status, log, diff, show, ls-files, grep, rev-parse, config --get) stays free.
# Tests: tests/test_git_guard.py.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
cmd = str((d.get("tool_input") or {}).get("command") or "")
# text inside quotes is an argument (grep -n "git push", -m "…", sshCommand="…"), not a git call: blanked out first
cmd = re.sub(r"\"[^\"]*\"|\x27[^\x27]*\x27", "Q", cmd)
WRITE = r"(add|commit|push|merge|rebase|reset|checkout|switch|stash|pull|fetch|tag|cherry-pick|revert|rm|mv|gc|filter-branch|am|apply|restore)"
# each git invocation: «git», optional «-c k=v» / «-C dir» options, then the subcommand
bare = []
for m in re.finditer(r"(?<![\w./-])git(?:\.exe)?((?:\s+-[cC]\s+(?:\"[^\"]*\"|\x27[^\x27]*\x27|\S+))*)\s+" + WRITE + r"\b", cmd):
    before = cmd[:m.start()]
    seg = re.split(r"&&|\|\||;|\n|\|", before)[-1]          # the part of the command line this git belongs to
    if re.search(r"films\.locks\s+run\s+git\b[^\n]*--\s*$", seg.strip() + " ") or re.search(r"films\.locks\s+run\s+git\b.*--", seg):
        continue
    bare.append(m.group(0).strip()[:60])
import os
stage = None
try:
    stage = json.load(open(os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."), "data", "cache", "locks", "stage.json"), encoding="utf-8"))
except (OSError, ValueError):
    pass
if stage and re.search(r"(?<![\w./-])git(?:\.exe)?(?:\s+-[cC]\s+\S+)*\s+(commit|push)\b", cmd):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Открыт этап плана «" + str(stage.get("stage")) + "» (с " + str(stage.get("started")) + "): коммит и пуш — только по "
        "окончании этапа (владелец 2026-10-26: «комит и пуш только на этап плана можно, не чаще»). Закончил этап — "
        "`python -X utf8 -m films.stage done`, потом коммит и пуш под семафором git."}}, ensure_ascii=False))
    sys.exit(0)
if bare:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "git — критическая область: только через семафор `python -X utf8 -m films.locks run git -- git …` "
        "(ждёт, пока закончит другой; владелец 2026-10-26: «чтобы не было гонок»). Пуш с долгой проверкой приватности: "
        "`python -X utf8 -m films.locks run git -- git -c core.sshCommand=\"C:/Windows/System32/OpenSSH/ssh.exe -o ServerAliveInterval=20 -o ServerAliveCountMax=90\" push -q origin main`. "
        "Найдено без семафора: " + "; ".join(bare[:3])}}, ensure_ascii=False))
'
exit 0
