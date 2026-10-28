#!/usr/bin/env bash
# PreToolUse on Claude in Chrome tabs_close_mcp (owner, 2026-10-27: «Тебе надо запретить так закрывать вкладки чтобы к другим
# терять контекст» — closing the finished rutracker tab dissolved Claude's tab group, and the kinozal collector tab and
# the nnmclub keep-alive tab dropped out of it: the extension can no longer stop, check or restart them). While any
# collector is still running (films.gate.open_collectors — a recent agent_src_* / agent_rutracker log without its OK
# marker) no tab is closed; close all of them together at the end of the stage. A Cloudflare helper page is opened by
# navigating an existing tab of the group, not in a new tab that is then closed. Tests: tests/test_tab_close_guard.py.
input=$(cat)
printf '%s' "$input" | CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}" python -X utf8 -c '
import json, os, sys
d = json.load(sys.stdin)
if not str(d.get("tool_name", "")).endswith("tabs_close_mcp"):
    sys.exit(0)
sys.path.insert(0, os.environ.get("CLAUDE_PROJECT_DIR", "."))
try:
    from films import gate
    running = gate.open_collectors()
except Exception as e:      # cannot tell → do not close
    running = ["unknown (" + str(e)[:60] + ")"]
if running:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Пока идёт сбор (" + ", ".join(running) + "), вкладки Chrome не закрывать: закрытие вкладки "
        "может распустить группу Claude, и вкладки со сборщиками выпадут из неё (2026-10-27, владелец: «запретить так закрывать "
        "вкладки»). Закрыть все вместе в конце этапа; для Cloudflare — перейти по ссылке в уже открытой вкладке группы."}},
        ensure_ascii=False))
'
exit 0
