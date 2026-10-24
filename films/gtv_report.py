"""Report of the Google TV «Хочу посмотреть» operation (R94 / R143): one line per film, no exceptions (owner, 2026-10-24:
«у операции должен быть отчёт по каждому фильму, без исключений»).

Input: the newest data/cache/ingest/gtv-*.txt — the page posts {"items": [[id, orig, ru, year], …], "res": {id: result}}
(localStorage gtvItems / gtvRun) to the bridge (python -X utf8 -m films.bridge). Writes data/cache/gtv_report.md and loads
verified results into store_items (store_watch.ingest_gtv). Statuses: «добавлен (проверено)», «добавлен, не подтвердился»,
«уже был в списке», «нет карточки Google», «ошибка», «ещё не обработан».

Run: python -X utf8 -m films.gtv_report [--no-db]
"""
from __future__ import annotations

import json
import sys

from . import db

INGEST = db.ROOT / "data" / "cache" / "ingest"
REPORT = db.ROOT / "data" / "cache" / "gtv_report.md"


def status(r: dict | None) -> str:
    if not r:
        return "ещё не обработан"
    if r.get("action") == "added":
        return "добавлен (проверено)" if r.get("verified") else f"добавлен, не подтвердился (после: {r.get('after')})"
    if r.get("state") == "in":
        return "уже был в списке"
    if r.get("state") == "nopanel":
        return "нет карточки Google"
    if r.get("state") == "error":
        return f"ошибка: {r.get('err')}"
    return f"{r.get('action') or '?'} / {r.get('state') or '?'}" + (f" ({r['why']})" if r.get("why") else "")


def build(load_db: bool = True) -> dict:
    files = sorted(INGEST.glob("gtv-*.txt"))
    if files:
        d = json.loads(files[-1].read_text(encoding="utf-8"))
        items, res = d["items"], {str(k): v for k, v in d["res"].items()}
    else:
        # a google.com page may not reach the local bridge (Chrome local-network access) → compact codes read out of
        # the tab in chunks: data/cache/stores/gtv_codes.txt «id:A id:I …» (A added+verified, U added not verified,
        # I already in the list, N no Google card, E error) + the item list data/cache/gtv_items_run.json
        items = json.loads((db.ROOT / "data" / "cache" / "gtv_items_run.json").read_text(encoding="utf-8"))
        code = {"A": {"action": "added", "verified": True, "state": "out"}, "U": {"action": "added", "verified": False, "state": "out"},
                "I": {"action": "skip", "state": "in"}, "N": {"action": "skip", "state": "nopanel"}, "E": {"state": "error", "err": "см. вкладку"}}
        res = {}
        for tok in (db.ROOT / "data" / "cache" / "stores" / "gtv_codes.txt").read_text(encoding="utf-8").split():
            k, c = tok.split(":")
            res[k] = dict(code.get(c, {"state": c}), id=int(k))
        files = [db.ROOT / "data" / "cache" / "stores" / "gtv_codes.txt"]
    counts, lines = {}, []
    for n, (tid, orig, ru, year) in enumerate(items, 1):
        r = res.get(str(tid))
        st = status(r)
        counts[st.split(" (")[0].split(":")[0]] = counts.get(st.split(" (")[0].split(":")[0], 0) + 1
        card = f" — карточка: «{r.get('title')}», {r.get('sub')}" if r and r.get("title") else ""
        lines.append(f"| {n} | {ru or orig} | {orig if ru and orig != ru else ''} | {year} | {st}{card} |")
    head = [f"# Google TV «Хочу посмотреть»: отчёт по каждому фильму ({len(items)})", "",
            f"Источник: `{files[-1].name}`. Итог: " + ", ".join(f"{k} — {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])),
            "", "| № | Название | Оригинал | Год | Результат |", "|---|---|---|---|---|"]
    REPORT.write_text("\n".join(head + lines) + "\n", encoding="utf-8")
    out = {"films": len(items), "counts": counts, "report": str(REPORT)}
    if load_db:
        from .store_watch import ingest_gtv
        out["db"] = ingest_gtv([{**v, "id": int(k)} for k, v in res.items() if v])
    return out


if __name__ == "__main__":
    print(json.dumps(build(load_db="--no-db" not in sys.argv), ensure_ascii=False, indent=1))
