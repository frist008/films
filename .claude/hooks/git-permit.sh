#!/usr/bin/env bash
# Commit and push only with the owner's explicit permission (owner, 2026-11-03: «давай создадим хук, что комит и пуш можно
# только с моего явного разрешения»; «Спрашивай каждый раз» — one answer = one commit with its push).
#   pre  (PreToolUse, Bash / PowerShell): `git commit` / `git commit-tree` needs data/cache/locks/git_permit.json with
#        granted = true, younger than 2 h and not used by a commit yet (the commit uses it); `git push` needs a granted
#        permission younger than 2 h. Otherwise deny: ask with AskUserQuestion first.
#   post (PostToolUse, AskUserQuestion): a question that names both «коммит» and «пуш» → its answer is written to
#        git_permit.json (granted = the answer starts with «Да»).
# Tests: tests/test_git_permit.py.
mode="${1:-pre}"
input=$(cat)
printf '%s' "$input" | MODE="$mode" CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}" python -X utf8 -c '
import json, os, re, sys, time
d = json.load(sys.stdin)
mode = os.environ["MODE"]
locks = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."), "data", "cache", "locks")
flag = os.path.join(locks, "git_permit.json")
TTL = 2 * 3600

def load():
    try:
        with open(flag, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def save(obj):
    os.makedirs(locks, exist_ok=True)
    with open(flag, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)

if mode == "post":
    if d.get("tool_name") != "AskUserQuestion":
        sys.exit(0)
    qs = [q.get("question", "") for q in (d.get("tool_input") or {}).get("questions") or []]
    gq = [q for q in qs if re.search(r"коммит|commit", q, re.I) and re.search(r"пуш|push", q, re.I)]
    if not gq:
        sys.exit(0)
    resp = d.get("tool_response")
    answers = {}
    if isinstance(resp, dict) and isinstance(resp.get("answers"), dict):
        answers = resp["answers"]
    else:
        s = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        for q in gq:
            m = re.search(re.escape(q) + r"\"\s*=\s*\"([^\"]*)", s)
            if m:
                answers[q] = m.group(1)
    ans = next((answers.get(q) for q in gq if answers.get(q)), None)
    if ans is None:
        sys.exit(0)
    # «да» anywhere as a word, unless the answer starts with «нет» (2026-11-03: «старая история тоже не нужна на компе, да
    # комит и пуш» — a yes not at the start; owner: «правь хук!»)
    yes = bool(re.search(r"(?<![а-яё])да(?![а-яё])", ans, re.I)) and not re.match(r"\s*(нет|не\s)", ans, re.I)
    save({"granted": yes, "answer": ans[:120], "at": time.time(), "commit_used": False})
    sys.exit(0)

if d.get("tool_name") not in ("Bash", "PowerShell"):
    sys.exit(0)
cmd = str((d.get("tool_input") or {}).get("command") or "")
cmd = re.sub(r"\"[^\"]*\"|\x27[^\x27]*\x27", "Q", cmd)   # quoted text is an argument (grep "git push", -m "…")
GIT = r"(?<![\w./-])git(?:\.exe)?(?:\s+-[cC]\s+\S+)*\s+"
commit = re.search(GIT + r"commit(?:-tree)?\b", cmd)
push = re.search(GIT + r"push\b", cmd)
if not (commit or push):
    sys.exit(0)
st = load()
fresh = st.get("granted") and time.time() - float(st.get("at") or 0) < TTL
why = None
if commit and not (fresh and not st.get("commit_used")):
    why = "коммит"
elif push and not fresh:
    why = "пуш"
if why:
    msg = ("Коммит и пуш — только с явного разрешения владельца (2026-11-03: «давай создадим хук, что комит и пуш можно только "
           "с моего явного разрешения», «Спрашивай каждый раз»). Сначала AskUserQuestion: в вопросе слова «коммит» и «пуш», "
           "что войдёт в коммит; ответ «Да, коммить и пушить» / «Нет». Один ответ «да» — один коммит с пушем, 2 часа.")
    if st:
        msg += " Последний ответ: «" + str(st.get("answer")) + "», " + str(int((time.time() - float(st.get("at") or 0)) // 60)) + " мин назад" + (", коммит по нему уже сделан." if st.get("commit_used") else ".")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": msg}}, ensure_ascii=False))
    sys.exit(0)
if commit:
    st["commit_used"] = True
    save(st)
sys.exit(0)
'
exit 0
