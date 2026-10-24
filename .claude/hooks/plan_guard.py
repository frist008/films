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


# scheduled prompts (cron watchdog, /loop) are not the owner's words
NOT_OWNER = re.compile(r"^\s*(Проверка фоновых агентов|<<autonomous-loop)", re.I)


def _key(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()[:8]


def _seen() -> set[str]:
    try:
        return set(json.loads(SEEN.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _add(items: list[tuple[str, str]]) -> list[str]:
    """items: (iso time, text) → new inbox lines appended under HEAD (top of the plan)."""
    seen = _seen()
    new = [(ts, t) for ts, t in items if _key(t) not in seen and t.strip() and not NOT_OWNER.search(t)]
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
    SEEN.write_text(json.dumps(sorted(seen | {_key(t) for _, t in new})), encoding="utf-8")
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
        if ts < CUTOFF:
            continue
        a = d.get("attachment") or {}
        if d.get("type") == "attachment" and a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human":
            out.append((ts, a.get("prompt") or ""))
        elif d.get("type") == "user" and not d.get("isMeta") and not d.get("isSidechain"):
            c = (d.get("message") or {}).get("content")
            if isinstance(c, str) and not c.lstrip().startswith("<"):
                out.append((ts, c))
    return out


def main(mode: str) -> None:
    d = json.load(sys.stdin)
    if mode == "prompt":
        p = d.get("prompt") or ""
        if p.strip() and not p.lstrip().startswith("<") and not NOT_OWNER.search(p):
            _add([(datetime.now().isoformat(timespec="seconds"), p)])
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                  "Сообщение владельца записано в PLAN.md → «Входящие». Первым делом: оформить R-пункт (или сделать), "
                  "по выполнении — проверить и перенести в PLAN-done.md."}}, ensure_ascii=False))
        return
    new = _add(_human_messages(d.get("transcript_path") or ""))
    plan = PLAN.read_text(encoding="utf-8")
    inbox = re.findall(r"^- \[ \] ВХ-\w+.*$", plan, re.M)
    done = re.findall(r"^- \[x\].*$", plan, re.M)
    why = []
    # every finished stage goes to GitHub (owner, 2026-10-24: «на гитхаб каждый этап — пуш в мастер»)
    import subprocess
    git = lambda *a: subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout
    dirty = [l for l in git("status", "--porcelain").splitlines() if l[3:] and not l[3:].startswith("backup/")]
    ahead = git("rev-list", "--count", "@{u}..HEAD").strip()
    if dirty or (ahead and ahead != "0"):
        why.append(f"GitHub: {len(dirty)} незакоммиченных файлов, {ahead or '?'} незапушенных коммитов — `python -X utf8 -m "
                   "films.backup export` → `git add -A` → коммит → `git push origin main`.")
    if inbox:
        why.append(f"В PLAN.md «Входящие» {len(inbox)} неразобранных сообщений владельца (первый приоритет): "
                   + " | ".join(x[:90] for x in inbox[:6]))
    if done:
        why.append(f"В PLAN.md {len(done)} выполненных «- [x]»: проверить, что сделано, и перенести в PLAN-done.md.")
    if why and not d.get("stop_hook_active"):
        print(json.dumps({"decision": "block", "reason": " ".join(why)}, ensure_ascii=False))
    elif why:
        print(json.dumps({"systemMessage": "План: " + " ".join(why)[:300]}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
