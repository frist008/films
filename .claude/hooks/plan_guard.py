"""Every message of the owner goes into PLAN.md first; done items are checked and moved out (owner, 2026-10-24:
«каждое моё сообщение … обязательно должно попадать в план — первый приоритет», «ты не забываешь из плана удалять то, что
сделал? и проверять, что оно сделано? … тут тоже нужен хук»).

  prompt  (UserPromptSubmit) — the message is appended to «## Входящие» of PLAN.md at once.
  stop    (Stop)             — owner messages that came in mid-turn (transcript: queued_command, origin human) are appended
                               too; the turn may not end while «Входящие» has entries (each becomes an R-item or is done)
                               or PLAN.md still holds done «- [x]» items (check, then move to PLAN-done.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
PLAN = ROOT / "PLAN.md"
SEEN = ROOT / "data" / "cache" / "plan_inbox_seen.json"
CUTOFF = "2026-10-24T06:45:00"   # messages before the hook existed were handled by hand
HEAD = "## Входящие (каждое сообщение владельца — сюда первым делом; разобрать в R-пункт или выполнить)"


# scheduled prompts (cron watchdog, /loop) and compaction summaries are not the owner's words
NOT_OWNER = re.compile(r"^\s*(Проверка фоновых агентов|Проверка цикла Google TV|Google TV, хвост из 50|<<autonomous-loop|This session is being continued"
                       r"|I hit my usage limit while you were working)", re.I)   # the app's «limit reset» note (2026-10-26)


def _text(c) -> str:
    """Message content → its text (a message with a screenshot is a list [image, text]: code review 2026-10-24)."""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(_text(b.get("text", "")) if isinstance(b, dict) and b.get("type") == "text" else
                        (b if isinstance(b, str) else "") for b in c)
    return ""


def _key(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()[:8]


def _seen() -> dict[str, int]:
    """text hash → how many times it is already in the plan (two identical «да» are two messages)."""
    try:
        v = json.loads(SEEN.read_text(encoding="utf-8"))
        return {k: 1 for k in v} if isinstance(v, list) else v
    except (OSError, ValueError):
        return {}


def _add(items: list[tuple[str, str]], occurrences: bool = False) -> list[str]:
    """items: (iso time, text) → new inbox lines under HEAD (top of the plan). occurrences=True (the transcript): add as
    many copies as the transcript has beyond what is recorded; otherwise (the prompt hook) add one."""
    seen = _seen()
    items = [(ts, t) for ts, t in items if t.strip() and not t.lstrip().startswith("<") and not NOT_OWNER.search(t)]
    new, count = [], {}
    for ts, t in items:
        k = _key(t)
        count[k] = count.get(k, 0) + 1
        if (count[k] if occurrences else seen.get(k, 0) + 1) > seen.get(k, 0):
            new.append((ts, t))
            seen[k] = count[k] if occurrences else seen.get(k, 0) + 1
    if not new:
        return []
    plan = PLAN.read_text(encoding="utf-8")
    lines = [f"- [ ] ВХ-{_key(t)} ({ts[:16].replace('T', ' ')}) «{re.sub(r'\s+', ' ', t).strip()}»" for ts, t in new]
    if HEAD not in plan:
        first = plan.index("\n## ")
        plan = plan[:first] + f"\n\n{HEAD}\n" + plan[first:]
    i = plan.index(HEAD) + len(HEAD) + 1
    plan = plan[:i] + "\n".join(lines) + "\n" + plan[i:]
    PLAN.write_text(plan, encoding="utf-8")
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen), encoding="utf-8")
    return lines


def _human_messages(transcript: str) -> list[tuple[str, str]]:
    out = []
    try:
        f = open(transcript, encoding="utf-8")
    except OSError:
        return out
    for line in f:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = d.get("timestamp") or ""
        if ts < CUTOFF or d.get("isCompactSummary") or d.get("isMeta") or d.get("isSidechain"):
            continue
        a = d.get("attachment") or {}
        if d.get("type") == "attachment" and a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human":
            out.append((ts, _text(a.get("prompt"))))
        elif d.get("type") == "user" and (d.get("origin") or {}).get("kind", "human") == "human":
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                continue   # tool results (answers to questions are recorded with the question's item)
            t = _text(c)
            if t and not t.lstrip().startswith("<"):
                out.append((ts, t))
    return out


WATCH_TABLE = "| Кто |"


def watchdog_table_missing(transcript: str) -> bool:
    """A watchdog check (the cron prompt «Проверка фоновых агентов…» or `films.watchdog` run in this turn) ends with its
    table — who, %, ETA (owner, 2026-10-28: «не вижу от сторожа таблицы с окончанием времени работы»)."""
    try:
        with open(transcript, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return False
    start = None
    for i, d in enumerate(rows):
        c = (d.get("message") or {}).get("content")
        if d.get("type") == "user" and not d.get("isMeta") and not (
                isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)):
            start = i
    if start is None:
        return False
    asked = bool(re.match(r"\s*Проверка фоновых агентов", _text((rows[start].get("message") or {}).get("content"))))
    ran, last = False, ""
    for d in rows[start + 1:]:
        if d.get("type") != "assistant":
            continue
        for b in (d.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and "films.watchdog" in json.dumps(b.get("input"), ensure_ascii=False):
                ran = True
            elif b.get("type") == "text" and b.get("text", "").strip():
                last = b["text"]
    return (asked or ran) and WATCH_TABLE not in last


ASK_LATER = re.compile(r"(?i)(задам|спрошу|вопрос\w*)[^.\n]{0,40}(следующ\w* (сообщени|ход)|позже|потом)")


sys.path.insert(0, str(Path(__file__).resolve().parent))
from lang_check import last_text, not_russian  # noqa: E402 — the reply the owner reads, read from the transcript's end


DEIXIS = re.compile(r"(?<![\w-])(тут|здесь|там|сюда|туда|на нём|на нем|на этом|этот сайт|этом сайте)(?![\w-])", re.I)
URL = re.compile(r"https?://[^\s<>\"«»]+")


def deixis_hint(text: str) -> str:
    """«как тут зарегистрироватся. Потенциально ещё один источник https://rustorka.com/…» — «тут» was read as the sites of
    the previous answer (owner, 2026-10-26: «ты вопрос не читал что ли?»). When a message has «тут / здесь / там …» and a
    link, the hint names the link it most likely points to."""
    urls = URL.findall(text or "")
    if not urls or not DEIXIS.search(text):
        return ""
    return (" ВНИМАНИЕ: в сообщении «" + DEIXIS.search(text).group(1) + "» и ссылка " + ", ".join(urls[:3]) +
            " — «тут/здесь» скорее всего про эту ссылку из того же сообщения, а не про то, о чём шла речь раньше; "
            "не уверен — спроси через AskUserQuestion.")


def main(mode: str) -> None:
    d = json.load(sys.stdin)
    if mode == "prompt":
        p = d.get("prompt") or ""
        if p.strip() and not p.lstrip().startswith("<") and not NOT_OWNER.search(p):
            _add([(datetime.now().isoformat(timespec="seconds"), _text(p))])
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                  "Сообщение владельца записано в PLAN.md → «Входящие». Первым делом: оформить R-пункт (или сделать), "
                  "по выполнении — проверить и перенести в PLAN-done.md." + deixis_hint(_text(p))}}, ensure_ascii=False))
        return
    new = _add(_human_messages(d.get("transcript_path") or ""), occurrences=True)
    plan = PLAN.read_text(encoding="utf-8")
    inbox = re.findall(r"^- \[ \] ВХ-\w+.*$", plan, re.M)
    done = re.findall(r"^- \[x\].*$", plan, re.M)
    why, net_note = [], ""
    # every finished stage goes to GitHub (owner, 2026-10-24: «на гитхаб каждый этап — пуш в мастер»)
    import subprocess
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout
    # files a running background agent is still editing (one path per line) are not a finished stage — commit them
    # after checking its result, not half-written
    try:
        wip = {x.strip() for x in (ROOT / "data" / "cache" / "locks" / "wip.txt").read_text(encoding="utf-8").splitlines() if x.strip()}
    except OSError:
        wip = set()
    dirty = [l for l in git("status", "--porcelain").splitlines() if l[3:] and not l[3:].startswith("backup/") and l[3:] not in wip]
    ahead = git("rev-list", "--count", "@{u}..HEAD").strip()
    if not dirty and ahead and ahead != "0":
        # unpushed only: if GitHub is unreachable (network outage) a push is impossible — remind, do not block
        try:
            reach = subprocess.run(["git", "ls-remote", "--heads", "origin"], cwd=ROOT, capture_output=True, timeout=20).returncode == 0
        except subprocess.TimeoutExpired:
            reach = False
        if not reach:
            net_note = f"GitHub недоступен (сеть): {ahead} незапушенных коммитов — push, когда сеть вернётся."
            ahead = "0"
    # commit + push once per plan stage (owner, 2026-10-26: «комит и пуш только на этап плана можно, не чаще»): while
    # a stage is open (data/cache/locks/stage.json, films/stage.py) the work is not asked to be committed
    try:
        open_stage = json.loads((ROOT / "data" / "cache" / "locks" / "stage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        open_stage = None
    if open_stage:
        dirty, ahead = [], "0"
    # notes in the plan between stages (the inbox this hook fills itself, the answers) go with the next stage's commit
    # (owner, 2026-10-29: «ты часто комитишь и пушишь? … более большими этапами а не по чуть чуть», «исправляй да»)
    if dirty and all(l[3:].strip('"') in ("PLAN.md", "PLAN-done.md") for l in dirty):
        dirty = []
    if dirty or (ahead and ahead != "0"):
        why.append(f"GitHub: {len(dirty)} незакоммиченных файлов, {ahead or '?'} незапушенных коммитов — `python -X utf8 -m "
                   "films.backup export` → `git add -A` → коммит → `git push origin main`.")
    if inbox:
        why.append(f"В PLAN.md «Входящие» {len(inbox)} неразобранных сообщений владельца (первый приоритет): "
                   + " | ".join(x[:90] for x in inbox[:6]))
    if done:
        why.append(f"В PLAN.md {len(done)} выполненных «- [x]»: проверить, что сделано, и перенести в PLAN-done.md.")
    # a promise in the plan without the action (owner, 2026-10-25: «всё сделал, выходит?» — «полный прогон (агент, 19:30)»
    # was written but never launched): every «лог data/logs/X.log» named in an open item must exist
    missing = sorted({m for m in re.findall(r"data/logs/([\w.-]+\.log)", "\n".join(
        l for l in plan.splitlines() if l.startswith("- [ ]"))) if not (ROOT / "data" / "logs" / m).exists()})
    if missing:
        why.append("В PLAN.md открытые пункты ссылаются на логи, которых нет (обещан запуск, а его не было): "
                   + ", ".join(missing[:6]) + " — запустить или исправить запись в плане.")
    # bug journal (owner, 2026-10-25: «Баги настолько часто, что исправлять надо кардинально»): every bug the owner
    # caught has a code guard (hook / test / module) in docs/bugs.md — the guard paths must exist, a row without one fails
    bugs = ROOT / "docs" / "bugs.md"
    if bugs.exists():
        bad = []
        for line in bugs.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 4 or not re.match(r"\d{4}-\d\d-\d\d", cells[0]):
                continue
            paths = re.findall(r"`([\w./-]+\.(?:sh|py|js|toml))", cells[3])
            if not paths or any(not (ROOT / p).exists() for p in paths):
                bad.append(cells[1][:50])
        if bad:
            why.append("docs/bugs.md: у бага нет стража (хук / тест / модуль) или путь не существует: " + " | ".join(bad[:5])
                       + " — баг не считается исправленным, пока его повтор не ловит код.")
    if watchdog_table_missing(d.get("transcript_path") or ""):
        why.append("Проверка сторожа: в последнем сообщении владельцу нет таблицы `films.watchdog --table` (кто, готово, "
                   "осталось, закончит, состояние) — вставить её целиком, потом короткий итог.")
    # questions are asked with AskUserQuestion in this turn, not promised for later (owner, 2026-10-28: «я не вижу вопросов
    # вообще-то. у тебя для вопросов есть ask question»)
    final = last_text(d.get("transcript_path") or "")
    if not_russian(final):   # owner, 2026-10-28: «опять на англ заговорил? исправляй баг»
        why.append("Последнее сообщение владельцу — не по-русски: «" + final[:80] + "» — написать его по-русски.")
    m = ASK_LATER.search(final)
    if m:
        why.append(f"В ответе «{m.group(0)}» — вопросы владельцу задаются сразу через AskUserQuestion (по 4 за вызов), "
                   "а не обещанием на следующий ход; ссылки — в итоговом сообщении после вопросов.")
    if not why and net_note:
        print(json.dumps({"systemMessage": net_note}, ensure_ascii=False))
        return
    if net_note:
        why.append(net_note)
    if why and not d.get("stop_hook_active"):
        print(json.dumps({"decision": "block", "reason": " ".join(why)}, ensure_ascii=False))
    elif why:
        print(json.dumps({"systemMessage": "План: " + " ".join(why)[:300]}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
