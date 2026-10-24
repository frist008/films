"""blu-ray.com: «Native 4K» / «Upscaled 4K» of 4K Blu-ray discs → titles.upscale_verdict (R32).

The IMDb Digital Intermediate (films.sources.imdb_specs) decides first; this fills only titles whose verdict is still
unknown (no DI / no resolution / conflicting entries) and that have a 4K release — that is where the
«Оригинал без апскейла» row depends on it.

Access (checked 2026-10-24): plain HTTP, no WAF / Cloudflare. But the site rate-limits guests hard: two requests in
a row → a 7-byte body «Error42» for a minute or so. Hence one request per `PACE_S` seconds for all processes
(films.locks.pace('bluray_com')) and a long back-off on «Error42».
  search:  POST https://www.blu-ray.com/search/quicksearch.php  userid=-1&country=all&section=4kbluraymovies&keyword=…
           → HTML list + `var urls = new Array('https://www.blu-ray.com/movies/…-4K-Blu-ray/{id}/', …)`
  disc:    GET that url → «Resolution: Native 4K (2160p)» / «Upscaled 4K (2160p)» / «4K (2160p)», and an
           imdb.com/title/tt… link — a disc counts only when its IMDb id is the title's.
Raw responses are cached in data/cache/bluray_com/ (gzip), so nothing is fetched twice.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import time

import requests

from .. import db, locks

BASE = "https://www.blu-ray.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PACE_S = 7.0
CACHE = db.ROOT / "data" / "cache" / "bluray_com"
MAX_DISCS = 2          # matching 4K disc pages read per title (search order: US first, then other countries)
MAX_PAGES = 4          # disc pages opened per title at most (other films' discs in the search results included)

_s = requests.Session()
_s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Referer": BASE + "/"})


class RateLimited(RuntimeError):
    pass


def _cache_path(key: str):
    return CACHE / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".html.gz")


def _fetch(key: str, do) -> str:
    p = _cache_path(key)
    if p.exists():
        return gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
    for attempt in range(6):
        locks.pace("bluray_com", PACE_S)
        try:
            r = do()
            body = r.text
        except requests.RequestException as e:
            body, r = f"EXC {e}", None
        if r is not None and r.status_code == 200 and not body.strip().startswith("Error"):
            CACHE.mkdir(parents=True, exist_ok=True)
            p.write_bytes(gzip.compress(body.encode("utf-8")))
            return body
        wait = 60 * (attempt + 1)
        print(f"  blu-ray.com {key[:60]}: {body.strip()[:20]!r} (HTTP {getattr(r, 'status_code', '-')}), wait {wait}s",
              file=sys.stderr, flush=True)
        time.sleep(wait)
    raise RateLimited(key)


def search_4k(keyword: str) -> list[str]:
    body = _fetch("qs4k:" + keyword.lower(), lambda: _s.post(
        BASE + "/search/quicksearch.php", timeout=40,
        data={"userid": "-1", "country": "all", "section": "4kbluraymovies", "keyword": keyword},
        headers={"X-Requested-With": "XMLHttpRequest"}))
    m = re.search(r"var\s+urls\s*=\s*new\s+Array\((.*?)\);", body, re.S)
    urls = re.findall(r"'([^']+)'", m.group(1)) if m else []
    return [u for u in urls if "-4K-Blu-ray/" in u]


def disc(url: str) -> dict:
    body = _fetch("disc:" + url, lambda: _s.get(url, timeout=40))
    imdb = re.findall(r"imdb\.com/title/(tt\d{7,9})", body)
    m = re.search(r"Resolution:\s*([^<]+)", body)
    res = m.group(1).strip() if m else None
    verdict = None
    if res and re.search(r"native\s*4k", res, re.I):
        verdict = "native"
    elif res and re.search(r"upscal", res, re.I):
        verdict = "upscaled"
    title = re.search(r"<title>([^<]*)", body)
    return {"url": url, "imdb": sorted(set(imdb)), "resolution": res, "verdict": verdict,
            "title": title.group(1).strip() if title else None}


def verdict_for(tconst: str, names: list[str]) -> dict:
    """Search by each name until a 4K disc of this IMDb id is found; check up to MAX_DISCS of its 4K discs."""
    out = {"tconst": tconst, "discs": [], "verdict": None, "pages": 0}
    seen = set()
    for name in names:
        if not name:
            continue
        for url in search_4k(name):
            if url in seen or len(out["discs"]) >= MAX_DISCS or out["pages"] >= MAX_PAGES:
                continue
            seen.add(url)
            out["pages"] += 1
            d = disc(url)
            if tconst in d["imdb"]:
                out["discs"].append(d)
        if out["discs"]:
            break
    vs = {d["verdict"] for d in out["discs"] if d["verdict"]}
    out["verdict"] = vs.pop() if len(vs) == 1 else ("conflict" if vs else None)
    return out


def targets(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT DISTINCT t.id, t.imdb_id, t.title_original, t.year FROM titles t
           JOIN release_titles rt ON rt.title_id = t.id JOIN releases r ON r.id = rt.release_id
           WHERE t.kind = 'movie' AND t.imdb_id IS NOT NULL AND r.resolution >= 2160
             AND coalesce(t.upscale_verdict, 'unknown') = 'unknown'
             AND coalesce(t.upscale_source, 'imdb_di') IN ('imdb_di', 'bluray_com')
           ORDER BY t.year DESC, t.id""")]


def run(conn, limit: int | None = None) -> dict:
    todo = targets(conn)[:limit] if limit else targets(conn)
    st = {"titles": len(todo), "native": 0, "upscaled": 0, "conflict": 0, "no_disc": 0, "disc_no_verdict": 0,
          "rate_limited": 0}
    report = []
    for i, t in enumerate(todo, 1):
        try:
            v = verdict_for(t["imdb_id"], [t["title_original"]])
        except RateLimited:
            st["rate_limited"] += 1
            print(f"  rate limited at {t['imdb_id']} — stop", file=sys.stderr, flush=True)
            break
        report.append({"title_id": t["id"], **v})
        if v["verdict"] in ("native", "upscaled"):
            st[v["verdict"]] += 1
            with conn:
                conn.execute("""UPDATE titles SET upscale_verdict = ?, upscale_source = 'bluray_com'
                                WHERE id = ? AND coalesce(upscale_verdict, 'unknown') = 'unknown'""", (v["verdict"], t["id"]))
        elif v["verdict"] == "conflict":
            st["conflict"] += 1
        elif v["discs"]:
            st["disc_no_verdict"] += 1
        else:
            st["no_disc"] += 1
        if i % 20 == 0:
            print(f"  {i}/{len(todo)} {json.dumps(st)}", file=sys.stderr, flush=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "last_run.json").write_text(json.dumps({"stats": st, "titles": report}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    return st


if __name__ == "__main__":
    conn = db.connect()
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if (sys.argv[1] if len(sys.argv) > 1 else "run") == "run":
        print(json.dumps(run(conn, lim), ensure_ascii=False, indent=1))
