"""Stage 6: best release + variant rows, filters (hide, never delete), upgrades, Dima, sort keys.

compute(conn) returns the rows the sheet shows: one dict per (title, variant), already sorted, each with
`hidden_reasons` (empty = shown on «Главная»). Nothing here writes to the DB except `responsibility`
(Dima auto rules) — the sheet and reports read from these rows.
"""
import json
import re
import sqlite3
import tomllib
from datetime import date, timedelta

from . import db

HDR_ORDER = ["DV", "HDR10+", "HDR10", "HLG", "SDR"]


def settings() -> dict:
    with open(db.ROOT / "config" / "settings.toml", "rb") as f:
        return tomllib.load(f)


def franchise_cfg() -> list[dict]:
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        return tomllib.load(f).get("franchise", [])


def people_cfg() -> list[dict]:
    with open(db.ROOT / "config" / "people.toml", "rb") as f:
        return tomllib.load(f).get("person", [])


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------------------------- quality

def hdr_class(hdr: list[str], dv_static: int | None = None) -> str:
    """Best HDR class of a release. Re-built DV with static metadata ranks just below real DV's peers."""
    for h in HDR_ORDER[:-1]:
        if h in hdr:
            if h == "DV" and dv_static:
                return "DV*"  # rebuilt from HDR10 (static metadata) — R31
            return h
    return "SDR"


def hdr_rank(cls: str) -> int:
    order = ["DV", "HDR10+", "DV*", "HDR10", "HLG", "SDR"]
    return order.index(cls) if cls in order else len(order)


def release_key(r: dict, cfg: dict) -> tuple:
    """Smaller is better. Voice first — «дубляж важнее качества всегда» (owner, 2026-10-24): dub > MVO > … > none,
    then HDR, 4K, source, bitrate."""
    voice_rank = cfg["sort"]["voice_rank"]
    source_rank = cfg["sort"]["source_rank"]
    v = r.get("best_voice") or "none"
    return (voice_rank.index(v) if v in voice_rank else len(voice_rank),
            hdr_rank(hdr_class(_j(r.get("hdr"), []), r.get("dv_static_md"))),
            0 if (r.get("resolution") or 0) >= 2160 else 1,
            source_rank.index(r["source_type"]) if r.get("source_type") in source_rank else len(source_rank),
            -(r.get("h264_equiv_kbps") or 0),
            -(r.get("seeders") or 0))


def describe(r: dict) -> str:
    hdr = hdr_class(_j(r.get("hdr"), []), r.get("dv_static_md"))
    parts = [str(r.get("resolution") or "SD"), r.get("source_type") or "?"]
    if hdr != "SDR":
        parts.append(hdr + (f" P{r['dv_profile']}" if hdr.startswith("DV") and r.get("dv_profile") else ""))
    if r.get("best_voice"):
        parts.append(r["best_voice"])
    return " ".join(parts)


def _qbt_release(q: dict) -> dict:
    """A queued qBittorrent torrent shown as the title's row when no collected release matches it."""
    n = q.get("name") or ""
    hdr = [x for x, rx in (("DV", r"DV|DoVi|Dolby.?Vision"), ("HDR10+", r"HDR10\+|HDR10P"), ("HDR10", r"HDR"))
           if re.search(rx, n, re.I)]
    src = next((s for s, rx in (("BDRemux", r"remux"), ("BDRip", r"bdrip|blu-?ray"), ("WEB-DL", r"web-?dl|webrip"))
                if re.search(rx, n, re.I)), None)
    res = 2160 if re.search(r"2160p|4k|uhd", n, re.I) else 1080 if re.search(r"1080p", n, re.I) else None
    return {"id": None, "source": "qbittorrent", "url": q.get("topic_url"), "name": n, "size_bytes": q.get("size_bytes"),
            "resolution": res, "hdr": json.dumps(hdr), "source_type": src, "best_voice": None, "edition": "[]"}


def plex_quality(p: dict) -> dict:
    audio = [a for a in _j(p.get("audio"), []) if a.get("stream") == "audio"]
    langs = {a.get("lang") for a in audio}
    kinds = {a.get("kind") for a in audio if a.get("lang") in ("ru", "uk")}
    return {"resolution": p.get("resolution"), "hdr": _j(p.get("hdr"), []), "dv_profile": p.get("dv_profile"),
            "source_type": p.get("source_type"), "has_ru_uk": bool(langs & {"ru", "uk"}),
            "voice": next((k for k in ("dub", "mvo", "dvo", "svo", "avo", "amateur") if k in kinds), None)}


def upgrade_reasons(have: dict, best: dict, cfg: dict) -> list[str]:
    """Why the best release is better than the downloaded file (empty = no upgrade)."""
    why = []
    if (best.get("resolution") or 0) > (have.get("resolution") or 0):
        why.append(f"разрешение {have.get('resolution')}→{best.get('resolution')}")
    hb, hh = hdr_class(_j(best.get("hdr"), [])), hdr_class(have["hdr"])
    if hdr_rank(hb) < hdr_rank(hh):
        why.append(f"{hh}→{hb}")
    if have.get("dv_profile") == 5 and hb != "SDR" and best.get("dv_profile") != 5:
        why.append("у меня DV P5 (не поддерживается)")
    bv = best.get("best_voice")
    if bv in ("dub", "mvo", "dvo", "svo", "avo") and have["has_ru_uk"] is False:   # None = unknown (a file Plex lacks)
        why.append(f"вышла озвучка: {bv}")
    elif bv == "dub" and have["voice"] and have["voice"] != "dub":
        why.append(f"вышел дубляж (у меня {have['voice']})")
    src = cfg["sort"]["source_rank"]
    # by source only a lossless disc counts (owner's example: BDRemux vs anything else); a BDRip is a
    # re-encode and is not automatically better than an untouched WEB-DL
    if (best.get("source_type") in ("BDRemux", "Blu-ray") and have.get("source_type") in src
            and src.index(best["source_type"]) < src.index(have["source_type"])
            and (best.get("resolution") or 0) >= (have.get("resolution") or 0)):
        why.append(f"{have['source_type']}→{best['source_type']}")
    return why


# ---------------------------------------------------------------------------------------------- Dima

def excluded_for(conn, d: dict) -> set[int]:
    """Titles a responsible person must not get: members of `exclude_franchises` (franchise_members) plus titles whose
    RU / original name matches `exclude_title_patterns` — franchise_members misses many (code review 2026-10-24: «Лего
    Звёздные войны: Мандалорец», «Бэтмен: Падение рыцаря» sat in Яна's playlist)."""
    out = set()
    if d.get("exclude_franchises"):
        q = ",".join("?" * len(d["exclude_franchises"]))
        out |= {r[0] for r in conn.execute(
            f"""SELECT t.id FROM titles t JOIN franchise_members m ON m.imdb_id = coalesce(t.imdb_id, t.series_imdb_id)
                WHERE m.franchise_key IN ({q}) AND m.confidence != 'review'""", d["exclude_franchises"])}
    if d.get("exclude_title_patterns"):
        rx = re.compile("|".join(d["exclude_title_patterns"]), re.I)
        out |= {tid for tid, ru, orig in conn.execute("SELECT id, title_ru, title_original FROM titles")
                if rx.search(f"{ru or ''} | {orig or ''}")}
    return out


def apply_responsible(conn, cfg: dict) -> dict:
    """Automatic responsibility from [responsible.*] in settings: Дима — «комедия» among the first two genres, Яна — feature
    animation. Manual assignments (owner's lists, sheet edits) are never overwritten; for a title that fits
    both rules the later entry (Яна) wins. An excluded or short title loses an older automatic guess."""
    from . import fresh
    first_seen = fresh.first_seen(conn)
    stats = {}
    with conn:
        for key, d in cfg["responsible"].items():
            st = stats.setdefault(key, {"auto": 0, "suggested": 0, "removed": 0})
            if d.get("animation"):
                rows = conn.execute("SELECT id, year FROM titles WHERE is_animation = 1 AND kind = 'movie'").fetchall()
            else:   # Дима: comedy as the 1st or 2nd genre (owner, 2026-10-24; was the 1st only) — `genre_rank` in settings
                top = d.get("genre_rank", 1)
                rows = [(tid, y) for tid, y, g in conn.execute(   # films only: nobody is responsible for series (2026-10-24)
                    "SELECT id, year, genres FROM titles WHERE kind = 'movie' AND genres IS NOT NULL AND genres != '[]'")
                        if set(json.loads(g)[:top]) & set(d["genres"])]
            skip = excluded_for(conn, d)
            short = {r[0] for r in conn.execute("SELECT id FROM titles WHERE runtime_min IS NOT NULL AND runtime_min < ?",
                                                (d.get("min_runtime", 0),))}
            # excluded / short: no automatic responsibility, an older guess goes away too (feature films only — a game
            # trailer and a 23-minute adult special were «cartoons» on TMDB)
            for tid in skip | short:
                st["removed"] += conn.execute("DELETE FROM responsibility WHERE title_id = ? AND person = ? AND source != 'manual'",
                                              (tid, d["name"])).rowcount
            for tid, year in rows:
                if tid in skip or tid in short:
                    continue
                cur = conn.execute("SELECT source FROM responsibility WHERE title_id = ?", (tid,)).fetchone()
                if cur and cur[0] == "manual":
                    continue
                # R99 (owner, 2026-10-24): «new» = first rutor release since [fresh] since (any film year) → confirmed; the
                # owner's manual lists covered everything before, so older ones stay suggestions by year
                if fresh.is_new(first_seen, tid):
                    src = "auto"
                else:
                    src = "auto_suggested" if year in d["suggest_years"] else None
                if src:
                    conn.execute("INSERT OR REPLACE INTO responsibility (title_id, person, source) VALUES (?, ?, ?)",
                                 (tid, d["name"], src))
                    st["auto" if src == "auto" else "suggested"] += 1
    return stats


apply_dima = apply_responsible  # old name, kept for callers


# ---------------------------------------------------------------------------------------------- main

def compute(conn) -> list[dict]:
    cfg = settings()
    years = set(db.years())
    flt = cfg["filters"]
    today = date.today()
    apply_responsible(conn, cfg)
    # owner's «не интересно» from the franchise survey (series of series_only franchises, old films / cartoons): applied on
    # every build — it was run by hand once and the config edits after it never reached the sheet (review 2026-10-24)
    from .franchise_seen import hide_excluded_series
    hide_excluded_series(conn)
    people_name = {}
    people = {}  # credits come from TMDB without IMDb ids, so a person matches by IMDb id or by name
    for p in people_cfg():
        for k in (p.get("imdb_id"), p["name"], p.get("tmdb_id") and f"tmdb:{p['tmdb_id']}"):
            if k:
                people[k] = p.get("weight", 1)
                people_name[k] = p["name"]  # hits are shown and weighted by the configured name
    # collections of franchises the owner wants collected beyond the configured years (R34)
    franchise_years, selected_cols, ignored_cols, selected_keys = {}, set(), set(), set()
    for f in franchise_cfg():
        if f.get("scope") == "all":
            selected_keys.add(f["key"])
            for cid in f.get("tmdb_collections", []):
                selected_cols.add(cid)
                franchise_years[cid] = f.get("from_year") or 0
        elif f.get("scope") == "none":
            ignored_cols.update(f.get("tmdb_collections", []))
    # universe members resolved from Wikidata / Wikipedia / fandom (films.franchise) — spin-offs and series too
    member_imdb = {r[0] for r in conn.execute(
        "SELECT imdb_id FROM franchise_members WHERE confidence != 'review' AND franchise_key IN (%s)" % ",".join("?" * len(selected_keys)),
        sorted(selected_keys))} if selected_keys else set()

    rel_by_title: dict[int, list[dict]] = {}
    for r in conn.execute(
            """SELECT rt.title_id, r.* FROM release_titles rt JOIN releases r ON r.id = rt.release_id"""):
        rel_by_title.setdefault(r["title_id"], []).append(dict(r))
    plex_by_title: dict[int, list[dict]] = {}
    for p in conn.execute("SELECT * FROM plex_items WHERE title_id IS NOT NULL AND present = 1"):
        plex_by_title.setdefault(p["title_id"], []).append(dict(p))
    from . import disks
    disk_by_title = disks.files_by_title(conn)   # every video file on the local drives (R85): downloaded = not seen
    watched = {r[0]: r[1] for r in conn.execute("SELECT title_id, group_concat(source) FROM watched GROUP BY 1")}
    marks = {r["title_id"]: dict(r) for r in conn.execute("SELECT * FROM user_marks")}
    resp = {r["title_id"]: dict(r) for r in conn.execute("SELECT * FROM responsibility")}
    wishlist = {r[0] for r in conn.execute("SELECT title_id FROM store_items WHERE in_wishlist = 1 AND title_id IS NOT NULL")}
    owned = {r[0] for r in conn.execute("SELECT title_id FROM store_items WHERE owned = 1 AND title_id IS NOT NULL")}
    # franchise: a TMDB collection where the owner watched another part
    watched_cols = {r[0] for r in conn.execute(
        """SELECT DISTINCT t.collection_id FROM titles t JOIN watched w ON w.title_id = t.id
           WHERE t.collection_id IS NOT NULL AND substr(w.source, -8) != '_partial'""")}
    credit_people: dict[int, set] = {}
    if people:
        for tid, name, imdb, tm in conn.execute(
                "SELECT c.title_id, p.name, p.imdb_id, p.tmdb_id FROM credits c JOIN people p ON p.id = c.person_id"):
            key = next((k for k in (f"tmdb:{tm}" if tm else None, imdb, name) if k and k in people), None)
            if key:
                credit_people.setdefault(tid, set()).add(people_name[key])
        # `crew = true` people also count as creator / showrunner / executive producer … (R75, films.crew)
        crew_keys = {f"tmdb:{p['tmdb_id']}" for p in people_cfg() if p.get("crew") and p.get("tmdb_id")}
        if crew_keys:
            try:
                for tid, tm in conn.execute("SELECT DISTINCT title_id, person_tmdb_id FROM person_crew"):
                    if f"tmdb:{tm}" in crew_keys:
                        credit_people.setdefault(tid, set()).add(people_name[f"tmdb:{tm}"])
            except sqlite3.OperationalError:  # table not migrated yet
                pass

    from .sources import qbittorrent
    try:
        queued = qbittorrent.queued_titles(conn)   # put on download in qBittorrent, not complete (R47)
    except Exception:  # noqa: BLE001 — table not migrated yet
        queued = {}
    from . import watchlist, kostya_list, responsible
    wanted = watchlist.title_ids()                 # owner's «хочу» list, high priority (R48)
    list_marker = kostya_list.markers_by_title()   # owner's 2021–2025 list: ❤️ / 💛 / 🔵 / 🧞‍♀ / 🧸 (R54)
    top_list = list(cfg.get("top", {}).get("imdb") or [])   # «в топ над всем» (owner): above even the urgent rows,
    top_imdb = set(top_list)                                # in the order the owner named them
    qwatch = set(cfg.get("quality_watch", {}).get("imdb") or [])   # owner's rutor list: own them, want a better copy
    urgent = responsible.urgent_titles()           # 🟢 / 🧞‍♀ / unticked ❤️ → the very top (R54, R55)
    from . import fresh
    first_seen = fresh.first_seen(conn)
    auto_mark = fresh.auto_marks(conn, first_seen)  # R99: new since 2026 with RT ≥ 90 → ❤️, 80–89 → 💛 (same symbols)
    wts = cfg["sort"].get("weights", {})

    pats = cfg.get("not_interested", {}).get("title_patterns") or []
    ni_rx = re.compile("|".join(pats), re.I) if pats else None   # «вообще не интересно» by name ([not_interested])
    apats = cfg.get("not_interested", {}).get("animation_patterns") or []
    ni_anim_rx = re.compile("|".join(apats), re.I) if apats else None   # same, cartoons only (live action stays)

    rows = []
    for t in conn.execute("SELECT * FROM titles"):
        t = dict(t)
        rels = rel_by_title.get(t["id"], [])
        if not rels and t["id"] in queued:
            rels = [_qbt_release(queued[t["id"]])]   # queued from another tracker: its torrent is the row
        if not rels:
            continue  # watched-only / store-only titles appear on «Просмотренные», not here
        rels.sort(key=lambda r: release_key(r, cfg))
        best = rels[0]
        variants = [("Лучшее", best)]
        for label, pred in (("Open Matte", lambda r: "open_matte" in _j(r.get("edition"), [])),
                            ("Расширенная", lambda r: set(_j(r.get("edition"), [])) & {"extended", "directors_cut", "uncut", "unrated"})):
            cand = [r for r in rels if pred(r)]
            if cand and cand[0] is not best:
                variants.append((label, cand[0]))
        studio_upscale = t.get("upscale_verdict") == "upscaled" or (t.get("di_resolution") in ("2K", "HD", "SD"))
        if studio_upscale and (best.get("resolution") or 0) >= 2160:
            orig = [r for r in rels if (r.get("resolution") or 0) < 2160 and not r.get("is_ai_upscale")]
            if orig:
                variants.append(("Оригинал без апскейла", orig[0]))
        ai = [r for r in rels if r.get("is_ai_upscale")]
        if ai and ai[0] is not best:
            variants.append(("AI-апскейл", ai[0]))

        hidden = []
        w = watched.get(t["id"]) or ""
        if any(s in w.split(",") for s in ("plex", "manual", "jellyfin")):
            hidden.append("видел")
        mark = marks.get(t["id"], {})
        if mark.get("mark") == "not_interested":
            hidden.append("не интересен")
        elif ni_rx and ni_rx.search(f"{t.get('title_ru') or ''} | {t.get('title_original') or ''}"):
            hidden.append("не интересен (правило)")
        elif ni_anim_rx and t.get("is_animation") and ni_anim_rx.search(f"{t.get('title_ru') or ''} | {t.get('title_original') or ''}"):
            hidden.append("не интересен (правило)")
        cid = t.get("collection_id")
        is_member = (t.get("imdb_id") or t.get("series_imdb_id")) in member_imdb
        in_franchise_scope = (cid in franchise_years and (t.get("year") or 0) >= franchise_years[cid]) or is_member
        owner_pick = (t.get("imdb_id") or "") in top_imdb or t["id"] in wanted or t["id"] in queued or t["id"] in list_marker             or t["id"] in urgent             or (resp.get(t["id"]) or {}).get("source") == "manual" and (resp.get(t["id"]) or {}).get("person") not in (None, "—")
        # ↑ the owner asked for it (his lists, Дима's / Яна's manual lists): no year / cartoon-series rule
        if t.get("year") not in years and not in_franchise_scope and not owner_pick:
            hidden.append(f"год {t.get('year')}")
        if t.get("tracker_category") in ("animation", "anime") and t["kind"] == "series" and not owner_pick                 and not is_member:
            hidden.append("мультсериал")
        rd = t.get("release_date")
        old = not rd or date.fromisoformat(rd[:10]) < today - timedelta(days=flt["fresh_days"])
        if t.get("imdb_rating") is not None and t["imdb_rating"] < flt["min_imdb"] and old:
            hidden.append(f"IMDb {t['imdb_rating']}")
        if flt["hide_without_voice"] and not t.get("no_dub_needed") and t["id"] not in queued and all(
                (r.get("best_voice") or "none") == "none" for r in rels):
            hidden.append("нет озвучки")
        upgrade = []
        have = None
        if plex_by_title.get(t["id"]):
            have = max((plex_quality(p) for p in plex_by_title[t["id"]]),
                       key=lambda q: ((q["resolution"] or 0), -hdr_rank(hdr_class(q["hdr"]))))
            upgrade = upgrade_reasons(have, best, cfg)
            if flt["hide_downloaded"] and not upgrade:
                hidden.append("скачано")
        elif disk_by_title.get(t["id"]):   # on disk, not in Plex (Jellyfin only / no media server): quality from the name
            have = max((disks.name_quality(f["path"]) for f in disk_by_title[t["id"]]),
                       key=lambda q: ((q["resolution"] or 0), -hdr_rank(hdr_class(q["hdr"]))))
            upgrade = upgrade_reasons(have, best, cfg) if have["resolution"] else []   # unknown resolution: no claim
            if flt["hide_downloaded"] and not upgrade:
                hidden.append("скачано")

        franchise = ((cid in selected_cols or cid in watched_cols or is_member) and cid not in ignored_cols
                     and "видел" not in hidden)
        person_hits = credit_people.get(t["id"], set())
        hdr_best = hdr_class(_j(best.get("hdr"), []), best.get("dv_static_md"))
        sort_vals = {
            "upgrade": 0 if upgrade else 1,
            "franchise": 0 if franchise else 1,
            "imax": 0 if t.get("is_imax") else 1,
            "people": -sum(people.get(p, 1) for p in person_hits),
            "hdr": hdr_rank(hdr_best),
            "uhd": 0 if (best.get("resolution") or 0) >= 2160 else 1,
            "voice": cfg["sort"]["voice_rank"].index(best.get("best_voice") or "none")
                     if (best.get("best_voice") or "none") in cfg["sort"]["voice_rank"] else 99,
            "wishlist": 0 if t["id"] in wishlist else 1,
            "imdb": -(t.get("imdb_rating") or 0),
        }
        # points (owner, 2026-10-24: «баллы за каждый признак»): every feature adds its weight, the sum decides
        lm, am = list_marker.get(t["id"]), auto_mark.get(t["id"])
        parts = {
            "твой список": {"want": wts.get("owner_want", 0), "maybe": wts.get("owner_maybe", 0),
                            "possible": wts.get("owner_possible", 0)}.get(lm, 0),
            "хочу (сериалы)": wts.get("watchlist", 0) if t["id"] in wanted else 0,
            "апгрейд": wts.get("upgrade", 0) if upgrade else 0,
            "франшиза": wts.get("franchise", 0) if franchise else 0,
            "IMAX": wts.get("imax", 0) if t.get("is_imax") else 0,
            "новинка RT": ({"want": wts.get("auto_want", 0), "maybe": wts.get("auto_maybe", 0)}.get(am, 0) if not lm else 0),
            "новинка 2026": wts.get("new_since", 0) if fresh.is_new(first_seen, t["id"]) else 0,
            "люди": min(wts.get("people_max", 0), wts.get("people_per_point", 0) * sum(people.get(p, 1) for p in person_hits)),
            "HDR": (wts.get("hdr") or {}).get(hdr_best, 0),
            "4K": wts.get("uhd", 0) if (best.get("resolution") or 0) >= 2160 else 0,
            "озвучка": (wts.get("voice") or {}).get(best.get("best_voice") or "none", 0),
            "желаемое": wts.get("wishlist", 0) if t["id"] in wishlist else 0,
            "IMDb": round(max(0, (t.get("imdb_rating") or 0) - 5) * wts.get("imdb_per_point_over_5", 0), 1),
            "RT": round(max(0, (fresh.rt_score(t) or 0) - 50) * wts.get("rt_per_point_over_50", 0), 1),
        }
        score = round(sum(parts.values()), 1)
        is_top = (t.get("imdb_id") or "") in top_imdb
        key = ((0, top_list.index(t["imdb_id"])) if is_top else (1 if t["id"] in urgent else 2, -score)) \
            + tuple(sort_vals[k] for k in cfg["sort"]["order"])
        for vi, (label, r) in enumerate(variants):
            rows.append({"title": t, "variant": label, "variant_index": vi, "release": r,
                         "quality": describe(r), "hidden_reasons": hidden, "upgrade": upgrade,
                         "have": have, "queued": queued.get(t["id"]), "wanted": t["id"] in wanted,
                         "list_marker": list_marker.get(t["id"]) or auto_mark.get(t["id"]), "auto_mark": auto_mark.get(t["id"]),
                         "urgent": t["id"] in urgent, "top": is_top, "quality_watch": (t.get("imdb_id") or "") in qwatch, "score": score, "score_parts": {k: v for k, v in parts.items() if v},
                         "first_seen": first_seen.get(t["id"]),
                         "franchise": franchise,
                         "people_hits": sorted(person_hits),
                         "in_wishlist": t["id"] in wishlist, "owned": t["id"] in owned,
                         "mark": mark, "responsible": resp.get(t["id"]), "watched": w,
                         "sort_key": key})
    rows.sort(key=lambda x: (x["sort_key"], x["title"]["id"], x["variant_index"]))
    return rows


def preview(conn, top: int = 40) -> None:
    rows = compute(conn)
    shown = [r for r in rows if not r["hidden_reasons"]]
    from collections import Counter
    reasons = Counter(x for r in rows if r["variant_index"] == 0 for x in r["hidden_reasons"])
    print(f"rows {len(rows)}, shown {len(shown)} ({sum(1 for r in shown if r['variant_index'] == 0)} titles)")
    print("hidden (main rows) by reason:", dict(reasons.most_common()))
    for r in shown[:top]:
        t = r["title"]
        flags = "".join(["⬆" if r["upgrade"] else "", "F" if r["franchise"] else "", "I" if t.get("is_imax") else ""])
        print(f"{flags:3} {r['variant'][:12]:12} {t['title_ru'] or t['title_original']}"
              f"{' S' + str(t['season_number']) if t['season_number'] else ''} ({t['year']}) | {r['quality']} | "
              f"IMDb {t['imdb_rating']} RT {t['rt_critics']}/{t['rt_audience']}"
              + (f" | апгрейд: {', '.join(r['upgrade'])}" if r["upgrade"] else ""))
