#!/usr/bin/env bash
# PreToolUse on mcp__ccd_turn__wrap_up (owner, 2026-10-28: «шо? опять баг скилов что на англ начал общатся? фикси баг и
# спрашивай нормально» — the next-step box went out in English: recap, question and buttons). Everything the owner reads
# is Russian: recap, ask and every button label must contain Cyrillic. Tests: tests/test_russian_guard.py.
input=$(cat)
# Every tool (owner, 2026-10-28: «опять на англ заговорил? исправляй баг»): the note written before this call must be Russian.
printf '%s' "$input" | CLAUDE_HOOKS="$(dirname "$0")" python -X utf8 -c '
import json, os, re, sys
sys.path.insert(0, os.environ["CLAUDE_HOOKS"])
from lang_check import last_text, not_russian
d = json.load(sys.stdin)
prev = last_text(d.get("transcript_path") or "")
if not_russian(prev):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Владелец читает только по-русски (2026-10-28: «опять на англ заговорил? исправляй баг»): "
        "последний текст ему был не по-русски — «" + prev[:80] + "». Напиши его заново по-русски, потом повтори вызов."}},
        ensure_ascii=False))
    sys.exit(0)
if not str(d.get("tool_name", "")).endswith("wrap_up"):
    sys.exit(0)
ti = d.get("tool_input") or {}
acts = ti.get("actions") or []
if isinstance(acts, str):
    try:
        acts = json.loads(acts)
    except ValueError:
        acts = []
fields = [("recap", ti.get("recap")), ("ask", ti.get("ask"))]
fields += [("кнопка", a.get("label")) for a in acts if isinstance(a, dict) and a.get("label")]
bad = [name for name, text in fields if text and not re.search(r"[А-Яа-яЁё]", str(text))]
if bad:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Владелец читает только по-русски (2026-10-28: «опять баг скилов что на англ начал общатся? "
        "фикси баг и спрашивай нормально»): итог, вопрос и кнопки — на русском. Не по-русски: " + ", ".join(bad) + "."}},
        ensure_ascii=False))
sys.exit(0)
'
exit 0
