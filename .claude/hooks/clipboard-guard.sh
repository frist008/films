#!/usr/bin/env bash
# PreToolUse (owner, 2026-10-26: «не пиши в буфер обмена! запомни»): the clipboard is the owner's — no writes to it from
# any command (Set-Clipboard, clip.exe, pbcopy, xclip/xsel, pyperclip, .NET Clipboard, navigator.clipboard in a page).
# Text the owner has to paste goes to a file he opens himself (e.g. data/cache/rutracker/run.js). Reading is allowed.
# Tests: tests/test_clipboard_guard.py.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
ti = d.get("tool_input") or {}
def texts(t, x):
    if t.endswith("browser_batch"):
        for a in x.get("actions") or []:
            yield from texts(a.get("name", ""), a.get("input") or {})
        return
    yield " ".join(str(x.get(k) or "") for k in ("command", "text"))
pat = re.compile(r"set-clipboard|\bclip(?:\.exe)?\b(?!board)|\bpbcopy\b|\bxclip\b|\bxsel\b|pyperclip\.copy|"
                 r"clipboard\]::set|clipboard\.set(?:text|data)|setclipboard|navigator\.clipboard\.write|"
                 r"execcommand\(\s*[\"\x27]copy", re.I)
for s in texts(d.get("tool_name", ""), ti):
    if pat.search(s):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "Буфер обмена — владельца, в него не писать (владелец, 2026-10-26: «не пиши в буфер обмена! запомни»). Текст для вставки — в файл, владелец откроет его сам (путь — в сообщении)."}}, ensure_ascii=False))
        break
'
exit 0
