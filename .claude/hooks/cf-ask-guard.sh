#!/usr/bin/env bash
# PreToolUse on AskUserQuestion: a question about Cloudflare only after the wait-and-recheck (films/cf_recheck.py).
cd "$(dirname "$0")/../.." || exit 0
python -X utf8 -c '
import json, sys
from films import cf_recheck
d = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")
if d.get("tool_name") != "AskUserQuestion":
    sys.exit(0)
texts = []
for q in (d.get("tool_input") or {}).get("questions") or []:
    texts.append(q.get("question", ""))
    texts += [o.get("label", "") + " " + o.get("description", "") for o in q.get("options") or []]
why = cf_recheck.ask_problem(texts)
if why:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": why}}, ensure_ascii=False))
'
exit 0
