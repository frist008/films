"""Cloudflare «я не робот» — сначала подождать, потом спрашивать (владелец, 2026-11-23: «Не нужна, ты врешь. Всегда когда ты
видишь что она нужна ставить таймер на минуту и смотреть не проверилось ли оно само»).

Сборщик пишет «waiting: cloudflare» → Claude открывает во вкладке сборщика страницу этого сайта (навигация: проверка
браузера проходит сама за полминуты; fetch() из старой страницы её не проходит), отмечает это —

    python -X utf8 -m films.cf_recheck mark <сайт>

— ждёт ≥ WAIT секунд и смотрит снова (fetch страницы раздела: 200 — прошла; сборщик перезапустить коротким вызовом).
Вопрос владельцу про Cloudflare хук `.claude/hooks/cf-ask-guard.sh` пропускает, только если такая отметка есть: моложе
FRESH и старше WAIT.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

MARK = Path(__file__).resolve().parent.parent / "data" / "cache" / "locks" / "cf_recheck.json"
WAIT = 60          # «ставить таймер на минуту»
FRESH = 15 * 60
ASK_RE = re.compile(r"cloudflare|не\s+робот|captcha|капч", re.IGNORECASE)


def mark(site: str, path: Path = MARK, now: float | None = None) -> dict:
    data = {"site": site, "at": now if now is not None else time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def ask_problem(texts: list[str], path: Path = MARK, now: float | None = None) -> str | None:
    """Причина отказа вопросу про Cloudflare (None — вопрос можно задать)."""
    if not any(ASK_RE.search(t or "") for t in texts):
        return None
    now = now if now is not None else time.time()
    try:
        age = now - float(json.loads(path.read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        age = None
    why = ("Сначала подождать (владелец, 2026-11-23: «Всегда когда ты видишь что она нужна ставить таймер на минуту и "
           "смотреть не проверилось ли оно само»): открыть во вкладке сборщика страницу сайта (навигация), "
           "`python -X utf8 -m films.cf_recheck mark <сайт>`, подождать ≥ 60 с и проверить снова; спрашивать — только если "
           "и после этого проверка не прошла.")
    if age is None or age > FRESH:
        return "Нет отметки повторной проверки Cloudflare. " + why
    if age < WAIT:
        return f"С повторной проверки прошло {int(age)} с, нужно ≥ {WAIT}. " + why
    return None


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "mark":
        print(json.dumps(mark(argv[1]), ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
