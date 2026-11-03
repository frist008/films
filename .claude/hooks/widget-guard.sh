#!/usr/bin/env bash
# PreToolUse on the chat widget (owner, 2026-10-26: «хоть проверяй что присылаешь» — the photos of the important-people
# candidates came from image.tmdb.org, which the widget sandbox blocks, so the owner got empty frames). The widget loads
# images / scripts / styles only from its allowlist; anything else (and local file paths) is denied before it is sent.
# Photos for the owner: download them, serve the page on 127.0.0.1 and check every image in the built-in browser
# (naturalWidth > 0) before giving the link — skill owner-survey «Важные люди». Tests: tests/test_widget_guard.py.
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
if not d.get("tool_name", "").endswith("show_widget"):
    sys.exit(0)
code = str((d.get("tool_input") or {}).get("widget_code") or "")
ALLOW = ("cdnjs.cloudflare.com", "esm.sh", "cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com", "fonts.gstatic.com")
bad = []
for m in re.finditer(r"(?:src|href|srcset|poster)\s*=\s*[\"\x27]?([^\"\x27\s>]+)|url\(\s*[\"\x27]?([^\"\x27)\s]+)", code, re.I):
    u = m.group(1) or m.group(2) or ""
    if re.match(r"(data:|#|javascript:|mailto:)", u, re.I):
        continue
    if m.group(0).lower().startswith("href") and re.match(r"https?://", u, re.I) and not re.search(r"\.(css|js)(\?|$)", u, re.I):
        continue                         # an ordinary link opens in the host browser
    host = re.match(r"https?://([^/:]+)", u, re.I)
    if host and host.group(1).lower() in ALLOW:
        continue
    bad.append(u[:80])
if bad:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "Виджет не загрузит: " + ", ".join(bad[:4]) + " — разрешены только " + ", ".join(ALLOW) +
        " и data:. Фото для владельца — страница на 127.0.0.1 с проверкой каждой картинки во встроенном браузере (скил owner-survey, «Важные люди»; владелец 2026-10-26: «хоть проверяй что присылаешь»)."}}, ensure_ascii=False))
'
exit 0
