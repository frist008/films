#!/usr/bin/env bash
# YouTube is the owner's too (owner, 2026-10-26: «ютуб пока занят … если нужен будет ютуб, то спроси свободен ли он, запиши
# в скил и хук»). Browser work on youtube.com (playlists, purchases, store languages) only after the owner answered «free».
#   pre  (PreToolUse, Chrome / built-in browser): a navigate to youtube.com, JS in a tab last navigated to YouTube, or
#        YouTube/playlist code (ytcfg, innertube, __want, __plRun, ACTION_ADD_VIDEO) → needs data/cache/locks/youtube_free.json
#        younger than 2 h with free = true; otherwise deny: ask with AskUserQuestion first.
#   post (PostToolUse, AskUserQuestion): a question about YouTube being free («YouTube … свобод…») → its answer is written
#        to youtube_free.json (free = answer starts with «Да» / says «свободен», not «занят»/«нет»).
# yt-dlp / HTTP search of trailers does not use the owner's YouTube — not covered. Tests: tests/test_youtube_guard.py.
mode="${1:-pre}"
input=$(cat)
printf '%s' "$input" | MODE="$mode" CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-.}" python -X utf8 -c '
import json, os, re, sys, time
d = json.load(sys.stdin)
mode = os.environ["MODE"]
locks = os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."), "data", "cache", "locks")
flag = os.path.join(locks, "youtube_free.json")
tabs_f = os.path.join(locks, "youtube_tabs.json")
TTL = 2 * 3600
YT_URL = re.compile(r"(^|[/.])(youtube\.com|youtu\.be|youtube-nocookie\.com)([/:?#]|$)", re.I)
YT_CODE = re.compile(r"ytcfg|innertube|youtubei/|__want\b|__plRun|ACTION_ADD_VIDEO|playlist_sync|youtube\.com", re.I)

def load(p, dflt):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return dflt

def save(p, obj):
    os.makedirs(locks, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)

if mode == "post":
    if d.get("tool_name") != "AskUserQuestion":
        sys.exit(0)
    qs = [q.get("question", "") for q in (d.get("tool_input") or {}).get("questions") or []]
    yq = [q for q in qs if re.search(r"youtube|ютуб", q, re.I) and re.search(r"свобод", q, re.I)]
    if not yq:
        sys.exit(0)
    resp = d.get("tool_response")
    answers = {}
    if isinstance(resp, dict) and isinstance(resp.get("answers"), dict):
        answers = resp["answers"]
    else:
        s = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        for q in yq:
            m = re.search(re.escape(q) + r"\"\s*=\s*\"([^\"]*)", s)
            if m:
                answers[q] = m.group(1)
    for q in yq:
        a = (answers.get(q) or "").strip()
        if not a:
            continue
        busy = re.search(r"занят|\bнет\b|не свобод", a, re.I)
        free = not busy and re.match(r"(да|свобод)", a, re.I)
        save(flag, {"at": time.time(), "free": bool(free), "answer": a})
    sys.exit(0)

tool = d.get("tool_name", "")
if not (tool.startswith("mcp__claude-in-chrome__") or tool.startswith("mcp__Claude_Browser__")):
    sys.exit(0)
tabs = load(tabs_f, {})

def actions(t, x):
    if t.endswith("browser_batch"):
        for a in x.get("actions") or []:
            yield from actions(a.get("name", ""), a.get("input") or {})
    else:
        yield t, x

touches = False
for t, x in actions(tool, d.get("tool_input") or {}):
    url = str(x.get("url") or "")
    tab = str(x.get("tabId") or "")
    if url and url not in ("back", "forward"):
        if tab:
            tabs[tab] = bool(YT_URL.search(url))
        if YT_URL.search(url):
            touches = True
    if tab and tabs.get(tab) and not url:
        touches = True
    if t.endswith("javascript_tool") and YT_CODE.search(str(x.get("text") or "")):
        touches = True
save(tabs_f, tabs)
if touches:
    st = load(flag, {})
    if not (st.get("free") and time.time() - float(st.get("at") or 0) < TTL):
        why = ("YouTube — и владельца: прежде чем работать в нём (плейлисты, покупки, языки магазина), спроси через AskUserQuestion, "
               "свободен ли YouTube (в вопросе слова «YouTube» и «свободен», ответ «Да, свободен» / «Нет, занят»). "
               "Ответ «да» действует 2 часа (владелец, 2026-10-26: «если нужен будет ютуб, то спроси свободен ли он»).")
        if st:
            why += " Последний ответ: «" + str(st.get("answer")) + "», " + str(int((time.time() - float(st.get("at") or 0)) // 60)) + " мин назад."
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": why}}, ensure_ascii=False))
sys.exit(0)
'
exit 0
