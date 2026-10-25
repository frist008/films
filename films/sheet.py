"""Google Sheets output (stage 7).

Access model (owner, 2026-10-23): the spreadsheet belongs to <google.main> and only the owner edits it;
everyone with the link can view. The script acts AS the owner through OAuth (desktop client from GCP
project films-db on <google.cloud>); no service accounts, no other editors.
Scope is drive.file: the app sees only files it created itself (the films spreadsheet).

rebuild(): read the owner's marks back into the DB first (never lose them), then rewrite every sheet.
"""
import json
import re
from datetime import date, timedelta
import tomllib

from . import db, rank

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CLIENT_FILE = db.ROOT / "secrets" / "google_oauth_client.json"
TOKEN_FILE = db.ROOT / "secrets" / "google_token.json"
TITLE = "Фильмы с 2024"   # used only when the spreadsheet is first created

MARKS = {"🔴 Очень хочу": "want", "🟡 Глянуть": "maybe", "⚪ Нейтрально": "neutral",
         "🟣 Заказ": "order", "⛔ Не интересен": "not_interested"}
MARK_LABEL = {v: k for k, v in MARKS.items()}
VOICE_RU = {"dub": "Дубляж", "mvo": "Многоголосый", "dvo": "Двухголосый", "svo": "Одноголосый",
            "avo": "Авторский", "amateur": "Любительский", "original": "Оригинал", "none": "нет"}
# row colours: blue hyperlinks must stay readable on them (owner 2026-10-25: «гиперссылки на зелёном не видно»):
# link-on-background contrast ≥ 4.3 for every row colour; three greens kept apart (queued < wanted < upgrade)
COLORS = {"queued": (0.70, 0.87, 0.62), "wanted": (0.82, 0.93, 0.76), "want": (0.957, 0.8, 0.8), "maybe": (1.0, 0.949, 0.8), "order": (0.851, 0.824, 0.914),
          "upgrade": (0.91, 0.97, 0.87), "dima": (0.812, 0.886, 0.953),
          "resp_dima": (0.706, 0.655, 0.839), "resp_yana": (0.957, 0.761, 0.851),  # name cell: Дима фиолетовый, Яна розовый
          # «Бюджет / сборы» by payback (box office ÷ 2.5 × budget): greens — paid off, yellow — break-even, reds — loss
          "roi_big": (0.42, 0.73, 0.45), "roi_ok": (0.72, 0.88, 0.72), "roi_even": (1.0, 0.92, 0.55),
          "roi_low": (0.98, 0.80, 0.78), "roi_bad": (0.94, 0.58, 0.56), "roi_flop": (0.80, 0.33, 0.33)}

# (header, width px) — the order is the sheet's column order; editable owner columns come first
# owner 2026-10-25: no «Метка» / «Заказчик» / «Заметка» columns («Я такое не просил»; «кто заказчик, не надо знать —
# достаточно знать через смайлик, что это заказ»); the list emoji (❤️ 💛 🔵 🧞‍♀ 🧸) is the start of the title itself,
# «так же, как я кидал»; the hidden «list» column keeps its key for the row colours
MAIN_COLUMNS = [
    ("Название", 300),   # always the leftmost column, frozen (owner 2026-10-25)
    ("Ответственный", 95), ("Баллы", 60), ("Год", 50), ("Качество", 190),
    ("Озвучка", 230), ("Форс. суб.", 70), ("Битрейт H.264-экв., Мбит/с", 90), ("Size, GB", 70),
    ("IMDb", 50), ("RT критики", 70), ("RT зрители", 70), ("Жанры", 150), ("Длительность", 70),
    ("Страны", 120), ("Режиссёр", 150), ("Сценарист", 150),
    ("Оператор", 130), ("Продюсеры", 170),   # survey 2026-10-25: DOP and producers «в таблицу»
    ("В ролях", 260),
    # owner 2026-10-25: «Студия и дистрибьютор в одну колонку, просто перечисление — не важно, кто есть кто»;
    # «Ключевые слова не нужны» (they stay in the DB)
    ("Студии", 170),
    ("Бюджет / сборы", 120), ("Сезон: серии, статус", 140), ("Ссылки", 300),
    ("Апгрейд", 200),   # «У меня (Plex)» — only on «Обновить качество» (owner 2026-10-25)
    # owner 2026-10-25, review of columns I had added on my own: «Вариант», «Тип», «IMAX», «Магазин», «Франшиза»,
    # «Важные люди», «Почему скрыт» — «ничего не надо»; only «Апгрейд» stays. The variant name goes into «Качество».
    ("title_id", 60), ("variant", 40), ("list", 40), ("state", 40), ("roi", 40),
    *((f"f:{k}", 30) for k, _, _ in [  # hidden coefficients of the «Баллы» constants (see CONSTS)
        ("owner_want", 0, 0), ("owner_maybe", 0, 0), ("owner_possible", 0, 0), ("watchlist", 0, 0), ("upgrade", 0, 0),
        ("franchise", 0, 0), ("imax", 0, 0), ("auto_want", 0, 0), ("auto_maybe", 0, 0), ("new_since", 0, 0),
        ("people_per_point", 0, 0), ("hdr.DV", 0, 0), ("hdr.HDR10+", 0, 0), ("hdr.DV*", 0, 0), ("hdr.HDR10", 0, 0),
        ("hdr.HLG", 0, 0), ("uhd", 0, 0), ("voice.dub", 0, 0), ("voice.mvo", 0, 0), ("voice.dvo", 0, 0),
        ("voice.svo", 0, 0), ("voice.avo", 0, 0), ("voice.amateur", 0, 0), ("wishlist", 0, 0),
        ("imdb_per_point_over_5", 0, 0), ("rt_per_point_over_50", 0, 0)]),
]
NL = "\n"
LIST_EMOJI = {"want": "❤️", "maybe": "💛", "possible": "🔵", "order": "🧞‍♀", "yana": "🧸"}


def _duration(minutes) -> str | None:
    """«1ч 41мин» — owner 2026-10-25: «длительность минут надо превратить в Xч Yyмин» (minutes two digits)."""
    if not minutes:
        return None
    h, m = divmod(int(minutes), 60)
    return f"{h}ч {m:02d}мин" if h else f"{m}мин"


def _rt(score, fresh: str, rotten: str) -> str | None:
    """«93% 🍅» — RT's own icons after the score, % at the end (owner 2026-10-25). Tomatometer: 🍅 fresh (≥ 60) /
    🤢 rotten (RT's green splat); Popcornmeter: 🍿 full bucket (≥ 60) / 🫗 tipped-over bucket."""
    if score is None:
        return None
    return f"{int(score)}% {fresh if score >= 60 else rotten}"


def _names(ru: str | None, orig: str | None, prefix: str = "") -> str:
    """Title cell: Russian name, the original on the next line (owner 2026-10-25: «Оригинальное название должно быть там
    же, где название, просто с новой строки»)."""
    ru = ru or orig or ""
    return prefix + ru + (f"\n{orig}" if orig and orig.strip().lower() != ru.strip().lower() else "")


def private_sheet_cfg() -> dict:
    return db.private().get("sheet", {})


def client():
    """Authorized gspread client. First run opens the browser for the owner's consent; while the GCP app is
    in 'Testing' Google expires the refresh token after 7 days, and the browser consent repeats."""
    import gspread
    if not CLIENT_FILE.exists():
        raise RuntimeError("secrets/google_oauth_client.json is missing — see docs/google-setup.md")
    return gspread.oauth(scopes=SCOPES, credentials_filename=str(CLIENT_FILE),
                         authorized_user_filename=str(TOKEN_FILE))


def whoami() -> str:
    gc = client()
    about = gc.http_client.request("get", "https://www.googleapis.com/drive/v3/about",
                                   params={"fields": "user(emailAddress,displayName)"}).json()
    return about["user"]["emailAddress"]


# ---------------------------------------------------------------------------------------------- spreadsheet

def _save_sheet_id(sid: str):
    path = db.ROOT / "config" / "private.toml"
    text = path.read_text(encoding="utf-8")
    if "[sheet]" in text:
        import re
        text = re.sub(r'(?m)^id = ".*"$', f'id = "{sid}"', text)
    else:
        text += f'\n[sheet]\nid = "{sid}"   # <sheet.id>: таблица фильмов (владелец — [google] main)\n'
    path.write_text(text, encoding="utf-8")


def open_spreadsheet(gc):
    sid = private_sheet_cfg().get("id")
    if sid:
        return gc.open_by_key(sid), False
    sh = gc.create(TITLE)
    _save_sheet_id(sh.id)
    # view for anyone with the link; the owner (file owner) is the only editor — R35
    gc.http_client.request("post", f"https://www.googleapis.com/drive/v3/files/{sh.id}/permissions",
                           json={"type": "anyone", "role": "reader", "allowFileDiscovery": False})
    return sh, True


# a renamed tab is renamed in place, never re-created (owner 2026-10-25: «я не просил создавать новые листы, только
# переименовать»): old title → new title
RENAMED_TABS = {"Главная": "Фильмы"}


def _ws(sh, title: str, rows: int, cols: int):
    try:
        ws = sh.worksheet(title)
    except Exception:  # gspread.WorksheetNotFound
        old = next((o for o, n in RENAMED_TABS.items() if n == title), None)
        try:
            ws = sh.worksheet(old) if old else None
        except Exception:
            ws = None
        if ws is None:
            return sh.add_worksheet(title=title, rows=max(rows, 2), cols=cols)
        ws.update_title(title)
    ws.resize(rows=max(rows, 2), cols=cols)
    return ws


# ---------------------------------------------------------------------------------------------- read back

def read_back(conn, sh) -> dict:
    """Owner's marks from the title tabs (Главная, Сериалы, …) and «Все» → user_marks / responsibility. Rows matched by title_id."""
    stats = {"marks": 0, "responsible": 0}
    seen = set()
    for title in (*MARK_TABS, "Главная", "Все"):   # «Главная» — the old name of «Фильмы» (read once after the rename)
        try:
            values = sh.worksheet(title).get_all_values()
        except Exception:
            continue
        if not values:
            continue
        head = values[0]
        idx = {h: i for i, h in enumerate(head)}
        if "title_id" not in idx:
            continue
        computed = {r["title_id"]: r for r in conn.execute("SELECT * FROM responsibility")}
        # what the script itself wrote last time: only a difference from THAT is an owner edit (a stale sheet must
        # never undo assignments made since — the 2026-10-23 bug: 224 «—» and lost manual lists)
        written = json.loads(WRITTEN.read_text(encoding="utf-8")) if WRITTEN.exists() else {}
        with conn:
            for row in values[1:]:
                get = lambda h: row[idx[h]].strip() if idx.get(h) is not None and idx[h] < len(row) else ""  # noqa: E731
                if not get("title_id").isdigit() or get("variant") not in ("0", ""):
                    continue
                tid = int(get("title_id"))
                if tid in seen:
                    continue
                seen.add(tid)
                cur = conn.execute("SELECT mark, ordered_by, note FROM user_marks WHERE title_id = ?", (tid,)).fetchone()
                # columns removed 2026-10-25: a missing column keeps what the DB has (never wipes «не интересен»)
                mark = MARKS.get(get("Метка")) if "Метка" in idx else (cur["mark"] if cur else None)
                ordered_by = (get("Заказчик") or None) if "Заказчик" in idx else (cur["ordered_by"] if cur else None)
                note = (get("Заметка") or None) if "Заметка" in idx else (cur["note"] if cur else None)
                if (mark, ordered_by, note) != (tuple(cur) if cur else (None, None, None)):
                    if mark or ordered_by or note:
                        conn.execute("INSERT OR REPLACE INTO user_marks (title_id, mark, ordered_by, note, updated_at) "
                                     "VALUES (?, ?, ?, ?, datetime('now'))", (tid, mark, ordered_by, note))
                    else:
                        conn.execute("UPDATE user_marks SET mark = NULL, ordered_by = NULL, note = NULL, "
                                     "updated_at = datetime('now') WHERE title_id = ?", (tid,))
                    stats["marks"] += 1
                resp = get("Ответственный")
                if str(tid) not in written:
                    continue          # no record of what was written → cannot tell an edit from a stale value
                shown = written[str(tid)]
                if resp != shown:  # owner changed it → manual decision
                    person = resp.rstrip("?") or "—"
                    conn.execute("INSERT OR REPLACE INTO responsibility (title_id, person, source) VALUES (?, ?, 'manual')",
                                 (tid, person))
                    stats["responsible"] += 1
    return stats


# ---------------------------------------------------------------------------------------------- cells

def _cell(v, link: str | None = None, bold: bool = False):
    if v is None or v == "":
        return {}
    if isinstance(v, bool):
        v = "✓" if v else ""
    c = {"userEnteredValue": {"numberValue": v} if isinstance(v, (int, float)) else {"stringValue": str(v)}}
    if link:
        c["textFormatRuns"] = [{"startIndex": 0, "format": {"link": {"uri": link}}}]
    if bold:
        c["userEnteredFormat"] = {"textFormat": {"bold": True}}
    return c


def _links_cell(links: list[tuple[str, str]], note: str | None = None):
    """«Трейлер · IMDb · RT · Картинка …» as one cell of links; `note` — plain text before them (a tracker-only title:
    «нет в TMDB/IMDb — данные только с трекера», owner 2026-10-25)."""
    links = [(t, u) for t, u in links if u]
    if not links:
        return _cell(note) if note else {}
    text, runs, pos = "", [], 0
    if note:
        text, pos = note, len(note)
        runs.append({"startIndex": 0, "format": {}})
        links = [("", None)] + links
    for i, (t, u) in enumerate(links):
        if u is None:
            continue   # the note's placeholder
        if i:
            runs.append({"startIndex": pos, "format": {}})
            text += " · "
            pos += 3
        runs.append({"startIndex": pos, "format": {"link": {"uri": u}}})
        text += t
        pos += len(t)
    return {"userEnteredValue": {"stringValue": text}, "textFormatRuns": runs}


def _j(v, d):
    try:
        return json.loads(v) if v else d
    except (TypeError, ValueError):
        return d


def _LANG_LABEL(code: str) -> str:
    """Language code as the sheet shows it — Ukrainian is «UA», not «UK» (owner 2026-10-25: «для укр озвучки пиши ua»)."""
    return "UA" if (code or "").lower() in ("uk", "ua", "ukr") else (code or "").upper()


def _voice_text(release: dict) -> str:
    parts = []
    for a in _j(release.get("audio"), []):
        if a.get("lang") not in ("ru", "uk"):
            continue
        label = VOICE_RU.get(a.get("kind") or "", a.get("kind") or "?")
        s = f"{_LANG_LABEL(a['lang'])}: {label}" + (f" ({a['studio']})" if a.get("studio") else "")
        if s not in parts:
            parts.append(s)
    return "; ".join(parts) or VOICE_RU.get(release.get("best_voice") or "none", "")


def _money(v):
    return None if not v else (f"${v / 1e9:.2f}B" if v >= 1e9 else f"${v / 1e6:.0f}M")


def _people(conn) -> dict:
    """title_id -> {role: [names]} as shown: Russian spelling, else the original (survey 2026-10-25: «русское в таблицу»;
    TMDB person translations, films.title_meta people). Limits = films.title_meta.SHOWN_LIMITS (names fetched for those)."""
    from .title_meta import SHOWN_LIMITS
    out: dict[int, dict] = {}
    for tid, role, name, job, ord_ in conn.execute(
            """SELECT c.title_id, c.role, COALESCE(p.name_ru, p.name), c.job, c.ord FROM credits c
               JOIN people p ON p.id = c.person_id
               ORDER BY c.title_id, c.role, COALESCE(c.ord, 999), p.id"""):
        d = out.setdefault(tid, {"director": [], "writer": [], "actor": [], "dop": [], "producer": []})
        if role in d and len(d[role]) < SHOWN_LIMITS[role]:
            d[role].append(name)
    return out


_COUNTRY_LABEL = {"UA": "Украина", "XWW": "мир", "US": "США"}


def _distributor(t: dict) -> str | None:
    """«Warner Bros. (США)» — the country tells whose distributor it is (imdb_specs.pick_distributor)."""
    if not t.get("distributor"):
        return None
    c = t.get("distributor_country")
    return t["distributor"] + (f" ({_COUNTRY_LABEL.get(c, c)})" if c else "")


def _group_keys(conn, rows: list[dict]) -> dict[int, str]:
    """title id → group: the franchise it belongs to (the most specific followed one — fewest members), else its TMDB
    collection, else its series (all seasons), else itself. Owner 2026-10-25: «сезоны одного сериала — рядом, только по
    увеличению года»; «фильмы одной франшизы или сериалы одной франшизы — рядом, приоритет в списке по верхнему»."""
    followed = {f["key"] for f in rank.franchise_cfg() if f.get("scope") != "none"}
    size = {k: n for k, n in conn.execute("SELECT franchise_key, count(*) FROM franchise_members GROUP BY 1")}
    fr: dict[str, str] = {}
    for imdb, key in conn.execute("SELECT imdb_id, franchise_key FROM franchise_members "
                                  "WHERE confidence != 'review' AND imdb_id IS NOT NULL"):
        if key in followed and (imdb not in fr or size.get(key, 0) < size.get(fr[imdb], 0)):
            fr[imdb] = key
    # one TMDB collection never splits between groups (R180: the earlier parts of a sequel, added for any year, are often
    # not franchise members while the sequel is): the whole collection joins the most specific franchise of any member
    col_fr: dict[int, str] = {}
    for r in rows:
        t = r["title"]
        key = fr.get(t.get("series_imdb_id") or t.get("imdb_id") or "")
        cid = t.get("collection_id")
        if key and cid and (cid not in col_fr or size.get(key, 0) < size.get(col_fr[cid], 0)):
            col_fr[cid] = key
    out = {}
    for r in rows:
        t = r["title"]
        key = col_fr.get(t.get("collection_id")) or fr.get(t.get("series_imdb_id") or t.get("imdb_id") or "")
        out[t["id"]] = (f"fr:{key}" if key else f"col:{t['collection_id']}" if t.get("collection_id")
                        else f"tv:{t['series_tmdb_id'] or t.get('series_key')}" if t["kind"] == "series"
                        else f"t:{t['id']}")
    return out


def _grouped(rs: list[dict], groups: dict[int, str]) -> list[dict]:
    """Rows in rank order, but each group (franchise / collection / series) together at the place of its top row;
    inside a group — by year (then release date), then season, keeping each title's variant rows right after it."""
    first: dict[str, int] = {}
    start: dict[str, tuple] = {}   # a series inside a franchise: all its seasons at the year of its first season
    sub = lambda t: f"tv:{t['series_tmdb_id'] or t.get('series_key')}" if t["kind"] == "series" else f"t:{t['id']}"  # noqa: E731
    for i, r in enumerate(rs):
        t = r["title"]
        first.setdefault(groups.get(t["id"], f"t:{t['id']}"), i)
        # by year, then release date (parts 1 and 2 of one year: «Кризис на бесконечных землях», R180)
        start[sub(t)] = min(start.get(sub(t), (9999, "9999")), (t.get("year") or 9999, (t.get("release_date") or "9999")[:10]))
    def key(r):
        t = r["title"]
        g = groups.get(t["id"], f"t:{t['id']}")
        return (first[g], start[sub(t)], sub(t), t.get("season_number") or 0, t.get("year") or 9999, t["id"],
                r["variant_index"])
    return sorted(rs, key=key)


def _blocks(ordered: list[dict], groups: dict[int, str]) -> list[tuple[int, int, str]]:
    """Outlines (owner, 2026-10-25: «Когда что-то одна франшиза — жирнющая обводка границ без внутренностей; когда 2–3
    строки об одном фильме просто разного качества — пунктирная обводка»): (first row, last row, 'franchise' | 'variants')
    as 0-based indexes into `ordered`. A franchise / collection / series block = consecutive rows of one group with ≥ 2
    titles; a variants block = consecutive rows of one title (best + Open Matte / original / AI …)."""
    out = []
    i = 0
    while i < len(ordered):
        g = groups.get(ordered[i]["title"]["id"])
        j = i
        while g and j + 1 < len(ordered) and groups.get(ordered[j + 1]["title"]["id"]) == g:
            j += 1
        if g and len({r["title"]["id"] for r in ordered[i:j + 1]}) > 1:
            out.append((i, j, "franchise"))
        i = j + 1
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1]["title"]["id"] == ordered[i]["title"]["id"]:
            j += 1
        if j > i:
            out.append((i, j, "variants"))
        i = j + 1
    # dashed first, thick after: where the two meet on an edge the franchise outline wins
    return sorted(out, key=lambda b: b[2] == "franchise")


def _roi(t: dict) -> int | None:
    """Payback in % for the «Бюджет / сборы» colour: box office ÷ (2.5 × production budget). Rule of thumb of the industry
    (owner 2026-10-25: «у окупаемости для фильмов есть что-то вроде формулы, там не простая математика»): cinemas keep
    about half of the gross and marketing costs roughly as much as production, so a film breaks even near 2.5 × budget.
    Films only, both numbers known, released ≥ 120 days ago (a film still in cinemas would look like a flop)."""
    b, g = t.get("budget_usd"), t.get("box_office_usd")
    if t.get("kind") != "movie" or not b or not g:
        return None
    rd = str(t.get("release_date") or "")[:10]
    if rd and rd > (date.today() - timedelta(days=120)).isoformat():
        return None
    return round(g * 100 / (2.5 * b))


def _companies(t: dict) -> list[str]:
    """Studios (first 2) and the distributor, one list, no labels — «не важно, кто есть кто» (owner 2026-10-25)."""
    out = list(_j(t.get("studios"), [])[:2])
    if t.get("distributor") and t["distributor"].lower() not in {x.lower() for x in out}:
        out.append(t["distributor"])
    return out


def _keywords(t: dict, n: int = 6) -> str | None:
    """First n TMDB keywords as TMDB gives them (English only: TMDB has no keyword translations)."""
    kw = _j(t.get("keywords"), [])
    return ", ".join(kw[:n]) + (" …" if len(kw) > n else "") if kw else None


def _wiki(t: dict) -> tuple[str, str | None]:
    """Wikipedia link for the «Ссылки» cell: Ukrainian, else English (survey 2026-10-25: «Википедию (uk/en)»)."""
    w = _j(t.get("wiki_links"), {})
    return ("Вики", w["uk"]) if w.get("uk") else ("Вики (en)", w.get("en"))


TRACKER_ONLY_NOTE = "нет в TMDB/IMDb — данные только с трекера"   # films/tracker_only.py (owner, 2026-10-25)


def _main_row(r: dict, people: dict, collections: dict) -> list:
    t, rel = r["title"], r["release"]
    resp = r["responsible"]
    resp_txt = (resp["person"] + ("?" if resp["source"] == "auto_suggested" else "")) if resp and resp["person"] != "—" else ""
    kind = ("Мульт" if t["is_animation"] else "Фильм") if t["kind"] == "movie" else f"Сезон {t['season_number']}"
    p = people.get(t["id"], {})
    season_info = None
    if t["kind"] == "series":
        status = {"returning": "идёт", "ended": "завершён", "canceled": "закрыт", "in_production": "в производстве"}
        season_info = f"{t.get('episodes_total') or '?'} сер." + (f", {status.get(t['series_status'], t['series_status'])}"
                                                                    if t.get("series_status") else "")
        # a season still airing: «вышло N из M, идёт» (owner, 2026-10-25), counted on the day the sheet is built
        dates = _j(t.get("episode_dates"), [])
        aired = sum(1 for d in dates if d <= date.today().isoformat())
        if dates and 0 < aired < len(dates):
            season_info = f"вышло {aired} из {max(len(dates), t.get('episodes_total') or 0)}, идёт"
    have = r["have"]
    have_txt = None
    if have:
        have_txt = " ".join(str(x) for x in (have.get("resolution"), have.get("source_type"),
                                               rank.hdr_class(have["hdr"]) if have["hdr"] else None,
                                               "RU/UA" if have["has_ru_uk"] else
                                               "без RU/UA" if have["has_ru_uk"] is False else None) if x)
    if r.get("queued"):   # R47: put on download in qBittorrent → green row, white text, «В очереди»
        have_txt = "В очереди"   # exact text: the colour rule compares it (no functions — ru locale separators)
    elif r.get("wanted") and not have_txt:   # R48: the owner's «хочу» list → lighter green, «Хочу»
        have_txt = "Хочу"
    store = "куплено" if r["owned"] else "в желаемом" if r["in_wishlist"] else None
    imdb_url = f"https://www.imdb.com/title/{t['imdb_id'] or t['series_imdb_id']}/" if (t["imdb_id"] or t["series_imdb_id"]) else None
    return [
        _cell(_names(t["title_ru"], t["title_original"],
                     ("🟢" if r.get("urgent") and not r.get("list_marker") else "") + LIST_EMOJI.get(r.get("list_marker"), "")),
              link=rel.get("url"), bold=r["variant_index"] == 0),
        _cell(resp_txt), {"__score__": True},
        _cell(t["year"]), _cell(r["quality"] if r["variant_index"] == 0 else f"{r['variant']}: {r['quality']}"),
        _cell(_voice_text(rel)), _cell(", ".join(_LANG_LABEL(x) for x in _j(rel.get("forced_subs"), []))),
        _cell(round(rel["h264_equiv_kbps"] / 1000, 1) if rel.get("h264_equiv_kbps") else None),
        _cell(round(rel["size_bytes"] / 1024 ** 3, 1) if rel.get("size_bytes") else None),
        _cell(t.get("imdb_rating")),
        _cell(_rt(t.get("rt_critics"), "🍅", "🤢")), _cell(_rt(t.get("rt_audience"), "🍿", "🫗")),
        _cell(", ".join(_j(t.get("genres"), []))), _cell(_duration(t.get("runtime_min"))),
        # countries and names — one per line; actors — in one line (owner 2026-10-25)
        _cell(NL.join(_j(t.get("countries"), [])[:2])),   # «не больше двух первых стран» (owner 2026-10-25)
        _cell(NL.join(p.get("director", []))), _cell(NL.join(p.get("writer", []))),
        _cell(NL.join(p.get("dop", []))), _cell(NL.join(p.get("producer", []))), _cell(", ".join(p.get("actor", []))),
        _cell(NL.join(_companies(t)) or None),
        _cell(" / ".join(x for x in (_money(t.get("budget_usd")), _money(t.get("box_office_usd"))) if x) or None),
        _cell(season_info),
        _links_cell([("Трейлер", t.get("trailer_ru_url")) if t.get("trailer_ru_url") else
                     ("Трейлер (" + {"uk": "укр", "original": "ориг.", "ru_sub": "рус. суб.", "any": "другой"}
                      .get(t.get("trailer_alt_kind"), "?") + ")", t.get("trailer_alt_url")),
                     ("IMDb", imdb_url), ("RT", t.get("rt_url")),
                     ("Картинка", t.get("poster_url")), _wiki(t), ("Сайт", t.get("homepage"))],
                    note=TRACKER_ONLY_NOTE if t.get("tracker_only") else None),
        _cell(", ".join(r["upgrade"])), _cell(t["id"]), _cell(r["variant_index"]),
        _cell(("urgent" if r.get("urgent") and not r.get("list_marker") else r.get("list_marker")) or None),
        _cell("queued" if r.get("queued") else "wanted" if r.get("wanted") else None), _cell(_roi(t)),
        *(_cell((r.get("factors") or {}).get(k, 0)) for k, _, _ in CONSTS),
    ]


def _watched_rows(conn) -> list[list]:
    rows = []
    for t in conn.execute(
            """SELECT t.*, max(w.watched_at) AS watched_at FROM titles t JOIN watched w ON w.title_id = t.id
               WHERE substr(w.source, -8) != '_partial' GROUP BY t.id ORDER BY max(w.watched_at) DESC"""):
        items = conn.execute("SELECT * FROM store_items WHERE title_id = ?", (t["id"],)).fetchall()
        owned = any(i["owned"] for i in items)
        wish = any(i["in_wishlist"] for i in items)
        offers = [i for i in items if i["available"] and i["price"]]
        # the version to buy: RU audio, then UK (store_watch marks it raw.preferred), else the cheapest offer
        pref = [i for i in offers if _j(i["raw"], {}).get("preferred")]
        best = pref[0] if pref else min(offers, key=lambda i: i["price"]) if offers else None
        audio = sorted({a for i in items for a in (_j(i["raw"], {}).get("audio_langs") or [])})
        if owned:
            status = "куплено"
        elif wish and best:
            status = "в желаемом, можно купить"
        elif wish:
            status = "в желаемом, нельзя купить"
        elif best:
            status = "можно купить (не в желаемом)"
        else:
            status = "нет в магазине" if items else "нет в магазине / не найден"
        imdb_url = f"https://www.imdb.com/title/{t['imdb_id']}/" if t["imdb_id"] else None
        rows.append([_cell(_names(t["title_ru"], t["title_original"]), link=imdb_url),
                     _cell(t["year"]), _cell((t["watched_at"] or "")[:10]), _cell(status),
                     _cell(best["price"] if best else None), _cell(best["currency"] if best else None),
                     _cell(best["discount_pct"] if best and best["discount_pct"] else None),
                     _cell(", ".join(audio).upper()), _cell(best["url"] if best else None, link=best["url"] if best else None),
                     _cell(t["id"])])
    return rows


WATCHED_COLUMNS = [("Название", 300), ("Год", 50), ("Видел", 90), ("Статус", 200),
                   ("Цена", 70), ("Валюта", 60), ("Скидка, %", 70), ("Озвучка в магазине", 110), ("Ссылка", 220),
                   ("title_id", 60)]


# ---------------------------------------------------------------------------------------------- write

def _write(sh, ws, columns, rows, mark_col: bool, blocks: list | None = None):
    sheet_id = ws.id
    header = [{"userEnteredValue": {"stringValue": h}, "userEnteredFormat": {
        "textFormat": {"bold": True}, "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
        "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}} for h, _ in columns]
    ncol = len(columns)
    reqs = [
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
            "rowCount": len(rows) + 1, "columnCount": ncol, "frozenRowCount": 1,
            "frozenColumnCount": 1 if columns[0][0] == "Название" else 0}},   # the title column stays in view
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount,frozenColumnCount)"}},
        {"updateCells": {"range": {"sheetId": sheet_id}, "fields": "*"}},  # clear values + formats
    ]
    meta = sh.fetch_sheet_metadata()
    for s in meta["sheets"]:
        if s["properties"]["sheetId"] == sheet_id:
            for i, _ in enumerate(s.get("conditionalFormats", [])):
                reqs.insert(1, {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}})
            if s.get("basicFilter"):
                reqs.insert(1, {"clearBasicFilter": {"sheetId": sheet_id}})
    sh.batch_update({"requests": reqs})
    chunk = 400
    data = [header] + rows
    if any(h == "Баллы" for h, _ in columns) and any(h.startswith("f:") for h, _ in columns):
        for n, row in enumerate(data[1:], start=2):   # the «Баллы» formula needs its own row number
            for j, c in enumerate(row):
                if c.get("__score__"):
                    row[j] = {"userEnteredValue": {"formulaValue": _score_formula(columns, n)}}
    for start in range(0, len(data), chunk):
        sh.batch_update({"requests": [{"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": start, "columnIndex": 0},
            "rows": [{"values": r} for r in data[start:start + chunk]],
            "fields": "userEnteredValue,textFormatRuns,userEnteredFormat"}}]})
    fmt = [{"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                    "startIndex": i, "endIndex": i + 1},
                                          "properties": {"pixelSize": w}, "fields": "pixelSize"}}
           for i, (_, w) in enumerate(columns)]
    # every cell wraps (owner 2026-10-25: «строка должна переноситься, кое-где не помещается … со всеми строками»)
    fmt.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": len(rows) + 1,
                                         "startColumnIndex": 0, "endColumnIndex": ncol},
                               "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                               "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment"}})
    fmt.append({"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                                         "endRowIndex": len(rows) + 1, "startColumnIndex": 0,
                                                         "endColumnIndex": ncol}}}})
    # outlines: franchise block — thick, the variant rows of one title — dashed; nothing inside (see _blocks)
    visible = [i for i, (h, _) in enumerate(columns) if h not in ("title_id", "variant", "list", "state", "roi")
               and not h.startswith("f:")]
    last_col = max(visible) + 1 if visible else ncol
    for a, b, kind in blocks or []:
        line = {"style": "SOLID_THICK" if kind == "franchise" else "DASHED", "color": {"red": 0, "green": 0, "blue": 0}}
        fmt.append({"updateBorders": {"range": {"sheetId": sheet_id, "startRowIndex": a + 1, "endRowIndex": b + 2,
                                                "startColumnIndex": 0, "endColumnIndex": last_col},
                                      "top": line, "bottom": line, "left": line, "right": line}})
    hidden = [i for i, (h, _) in enumerate(columns) if h in ("title_id", "variant", "list", "state", "roi") or h.startswith("f:")]
    for i in hidden:
        fmt.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                            "startIndex": i, "endIndex": i + 1},
                                                  "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    if mark_col:
        rng = {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": len(rows) + 1, "startColumnIndex": 0,
               "endColumnIndex": ncol}
        col = {h: _col_letter(i) for i, (h, _) in enumerate(columns)}
        lst = col["list"]   # hidden key of the owner's list emoji (want ❤️ / maybe 💛 / order 🧞‍♀ …)
        st = col["state"]   # hidden: queued (in qBittorrent) / wanted (the owner's «хочу» list)
        rules = [("queued", f'=${st}2="queued"'),
                 ("wanted", f'=${st}2="wanted"'), ("want", f'=${lst}2="want"'),
                 ("maybe", f'=${lst}2="maybe"'), ("order", f'=${lst}2="order"'),
                 ("upgrade", f'=(${lst}2="")*(${col["Апгрейд"]}2<>"")'),
                 ("dima", f'=(${lst}2="")*((${col["Ответственный"]}2="Дима")+(${col["Ответственный"]}2="Дима?")'
                          f'+(${col["Ответственный"]}2="Яна")+(${col["Ответственный"]}2="Яна?"))')]
        # the «Ответственный» cell itself: Дима — violet, Яна — pink (owner 2026-10-23); first in the rule list
        ci = [h for h, _ in columns].index("Ответственный")
        cell = dict(rng, startColumnIndex=ci, endColumnIndex=ci + 1)
        cell_rules = [("resp_dima", f'=(${col["Ответственный"]}2="Дима")+(${col["Ответственный"]}2="Дима?")'),
                      ("resp_yana", f'=(${col["Ответственный"]}2="Яна")+(${col["Ответственный"]}2="Яна?")')]
        # «Бюджет / сборы»: paid off (owner 2026-10-25: зелёный — окупилось, жёлтый — в ноль, красный — нет, с оттенками);
        # hidden «roi» = box office ÷ (2.5 × budget) in % — integers only (ru locale: no decimals or separators in formulas)
        bi, roi = [h for h, _ in columns].index("Бюджет / сборы"), col["roi"]
        bcell = dict(rng, startColumnIndex=bi, endColumnIndex=bi + 1)
        money_rules = [("roi_big", f'=(${roi}2<>"")*(${roi}2>=150)'), ("roi_ok", f'=(${roi}2<>"")*(${roi}2>=110)'),
                       ("roi_even", f'=(${roi}2<>"")*(${roi}2>=90)'), ("roi_low", f'=(${roi}2<>"")*(${roi}2>=60)'),
                       ("roi_bad", f'=(${roi}2<>"")*(${roi}2>=30)'), ("roi_flop", f'=(${roi}2<>"")*(${roi}2<30)')]
        cell_rules = [(k, f, cell) for k, f in cell_rules] + [(k, f, bcell) for k, f in money_rules]
        # no function-argument separators: the spreadsheet locale (ru) uses ";" instead of ","
        for i, (key, formula, where) in enumerate(cell_rules):
            r, g, b = COLORS[key]
            fmt.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [where], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}}}}})
        for i, (key, formula) in enumerate(rules, start=len(cell_rules)):
            r, g, b = COLORS[key]
            fmt.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [rng], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}}}}})
    sh.batch_update({"requests": fmt})


WRITTEN = db.ROOT / "data" / "cache" / "sheet_written.json"


def _remember_written(rows: list[dict]):
    """title_id → the «Ответственный» text written to the sheet (read_back compares the sheet with this)."""
    out = {}
    for r in rows:
        if r["variant_index"] == 0:
            resp = r["responsible"]
            out[str(r["title"]["id"])] = (resp["person"] + ("?" if resp["source"] == "auto_suggested" else ""))                 if resp and resp["person"] != "—" else ""
    WRITTEN.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


# the title links to the store page; price «344 ₴», discount «−20% (было 430 ₴)» — one column each (owner 2026-10-25:
# «всё, что я говорил про колонки, применимо ко всем листам»: no extra columns, UA for Ukrainian)
BUY_COLUMNS = [("Название", 300), ("Год", 50), ("Озвучка", 90), ("Качество", 70), ("Цена", 90), ("Скидка", 150),
               ("title_id", 60)]
_QNAME = {"uhd": "4K", "hd": "HD", "sd": "SD"}


def _buy_rows(conn) -> list[list]:
    """«Купить» (owner, 2026-10-24): watched, not bought, can be bought. RU audio first, then UK, then the rest; inside each —
    by the buy price of the BEST quality available (4K > HD > SD); rent and worse qualities are ignored."""
    owned = {r[0] for r in conn.execute("SELECT DISTINCT title_id FROM store_items WHERE owned = 1 AND title_id IS NOT NULL")}
    seen = {r[0] for r in conn.execute("SELECT DISTINCT title_id FROM watched WHERE substr(source, -8) != '_partial'")}
    best_by_title = {}
    for r in conn.execute("""SELECT s.*, t.title_ru, t.title_original, t.year AS t_year, t.imdb_id FROM store_items s JOIN titles t ON t.id = s.title_id
                             WHERE s.store = 'youtube' AND s.available = 1 AND coalesce(s.owned, 0) = 0"""):
        if r["title_id"] in owned or r["title_id"] not in seen:
            continue
        raw = _j(r["raw"], {})
        q = raw.get("best_quality")
        offer = (raw.get("buy_by_quality") or {}).get(q) if q else None
        if not offer:
            continue
        langs = set(raw.get("audio_langs") or [])
        group = 0 if "ru" in langs else 1 if "uk" in langs else 2
        key = (group, offer["price"])
        if r["title_id"] not in best_by_title or key < best_by_title[r["title_id"]][0]:
            best_by_title[r["title_id"]] = (key, r, q, offer, langs)
    out = []
    for (group, price), r, q, offer, langs in sorted(best_by_title.values(), key=lambda x: x[0]):
        imdb_url = f"https://www.imdb.com/title/{r['imdb_id']}/" if r["imdb_id"] else None
        old = offer.get("old_price")
        cur = {"UAH": "₴", "USD": "$", "EUR": "€"}.get(r["currency"] or "UAH", r["currency"] or "")
        out.append([_cell(_names(r["title_ru"], r["title_original"]), link=r["url"] or imdb_url), _cell(r["t_year"]),
                    _cell(", ".join(_LANG_LABEL(x) for x in sorted(langs))), _cell(_QNAME.get(q, q)),
                    _cell(f"{price:g} {cur}" if price else None),
                    _cell(f"−{round((old - price) * 100 / old)}% (было {old:g} {cur})" if old else None),
                    _cell(r["title_id"])])
    return out


PLAYLIST_COLUMNS = [("Плейлист", 260), ("Фильмов", 80), ("Что внутри", 520)]   # the name links to the playlist
PLAYLIST_ABOUT = {
    "kostya_want": "❤️ из списка владельца — трейлеры того, что очень хочется посмотреть",
    "probably": "💛/🟡 из списка владельца — вероятно хочу",
    "possible": "без смайлика из списка владельца — возможно",
    "dima": "ответственный Дима — комедии",
    "yana": "ответственная Яна — мультфильмы, которые мы ещё не видели",
}


QUALITY_COLUMNS = [("Название", 300), ("Год", 50), ("У меня", 220), ("Добавлено в Plex", 90),
                   ("Лучшее доступное", 300),
                   ("Что лучше", 260), ("Мой размер, GB", 80), ("Новый размер, GB", 80), ("title_id", 60)]
# ↑ owner 2026-10-25: «надо добавить колонку старого размера и нового, потому что сейчас колонка размера ничего не даёт»


def plex_added(conn) -> dict[int, str]:
    """title_id -> local date the present Plex copy was added (the latest copy when there are several)."""
    from datetime import datetime, timezone
    out = {}
    for tid, ts in conn.execute("SELECT title_id, max(added_at) FROM plex_items WHERE present = 1 AND title_id IS NOT NULL "
                                "AND added_at IS NOT NULL GROUP BY title_id"):
        try:
            out[tid] = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")
        except ValueError:
            out[tid] = ts[:10]
    return out


def _quality_rows(rows: list[dict], added: dict | None = None) -> list[list]:
    """«Обновить качество» (owner, 2026-10-24): films from his rutor list that he owns (downloaded / queued) — the best
    release vs his copy; «дубляж важнее качества всегда», so voice upgrades come first, then other upgrades, then the rest.
    `added`: plex_added() — Plex «date added», shown only on this tab (survey 2026-10-25)."""
    added = added or {}
    out = []
    for r in rows:
        if not r.get("quality_watch") or r["variant_index"] or "видел" in r["hidden_reasons"]:
            continue
        t, rel, have = r["title"], r["release"], r["have"]
        url = f"https://www.imdb.com/title/{t['imdb_id']}/" if t.get("imdb_id") else None
        mine = (" ".join(str(x) for x in (have.get("resolution") or "?", have.get("source_type") or "",
                                            ",".join(have.get("hdr") or []), have.get("voice") or
                                            ("RU/UA" if have.get("has_ru_uk") else "без RU/UA" if have.get("has_ru_uk") is False else ""))
                         if x) if have else ("в очереди" if r.get("queued") else "—"))
        why = r["upgrade"] or []
        if not why:
            continue   # owner 2026-10-25: only films with an improvement («там должны быть фильмы только с улучшениями»)
        voice_up = any(w.startswith(("вышла озвучка", "вышел дубляж")) for w in why)
        size = rel.get("size_bytes")
        old = (have or {}).get("size_bytes")
        worse = rank.downgrade_reasons(have, rel) if have else []
        key = (0 if voice_up else 1, -(t.get("imdb_rating") or 0))
        # no «Статус» column: everything here is downloaded («оно там всё скачано же, зачем это писать?»)
        out.append((key, [_cell(_names(t["title_ru"], t["title_original"]), link=url), _cell(t.get("year")),
                          _cell(mine), _cell(added.get(t["id"])),
                          # what the new release is worse at — on a new line in the same cell (owner, 2026-10-25)
                          _cell(r["quality"] + (f" — {rel.get('name')}" if rel.get("name") else "")
                                + (NL + "хуже: " + "; ".join(worse) if worse else ""), link=rel.get("url")),
                          _cell("; ".join(why)), _cell(round(old / 2**30, 1) if old else None),
                          _cell(round(size / 2**30, 1) if size else None),
                          _cell(t["id"])]))
    return [row for _, row in sorted(out, key=lambda x: x[0])]


NOT_FOUND_COLUMNS = [("Название", 300), ("Франшиза", 200), ("Год", 50)]   # the title links to IMDb / TMDB


def _not_found_rows(conn) -> list[list]:
    """«Нет на трекерах» (owner, 2026-10-25: «вынести отдельным листом список того, что ты не нашёл по франшизам в источниках,
    но так-то оно существует в мире по твоим данным»): members of the followed franchises (Wikidata / Wikipedia / TMDB,
    not marked review) that have no release on the trackers, are not watched and not hidden by the owner's franchise rules."""
    import tomllib
    with open(db.ROOT / "config" / "franchises.toml", "rb") as f:
        fr = {x["key"]: x for x in tomllib.load(f)["franchise"]}
    with_rel = set()
    for imdb, simdb, tmdb, stmdb in conn.execute(
            "SELECT t.imdb_id, t.series_imdb_id, t.tmdb_id, t.series_tmdb_id FROM titles t WHERE t.id IN (SELECT title_id FROM release_titles "
            "WHERE coalesce(how, '') NOT LIKE 'wrong_link%')"):
        with_rel |= {x for x in (imdb, simdb, f"tmdb:{tmdb}" if tmdb else None, f"tmdb:{stmdb}" if stmdb else None) if x}
    seen = set()
    for imdb, simdb, tmdb, stmdb in conn.execute(
            "SELECT t.imdb_id, t.series_imdb_id, t.tmdb_id, t.series_tmdb_id FROM titles t JOIN watched w ON w.title_id = t.id "
            "WHERE substr(w.source, -8) != '_partial'"):
        seen |= {x for x in (imdb, f"tmdb:{tmdb}" if tmdb else None) if x}
    out = []
    for key, imdb, tmdb, ttype, kind, title, title_ru, year in conn.execute(
            "SELECT franchise_key, imdb_id, tmdb_id, tmdb_type, kind, title, title_ru, year FROM franchise_members "
            "WHERE confidence != 'review'"):
        f = fr.get(key)
        if not f or f.get("scope") == "none":
            continue
        ids = {x for x in (imdb, f"tmdb:{tmdb}" if tmdb else None) if x}
        if ids & with_rel or ids & seen or imdb in set(f.get("hide") or []):
            continue
        series = ttype == "tv" or "series" in (kind or "")
        before = f.get("hide_series_before" if series else "hide_films_before")
        if not year or year > date.today().year:
            continue   # announced, not out anywhere yet (owner 2026-10-25: «не хочу видеть то, что анонсировано и не вышло»)
        if year and before and year < before:
            continue
        if year and f.get("scope") == "2024-2026" and year < 2024:
            continue
        url = f"https://www.imdb.com/title/{imdb}/" if imdb else (f"https://www.themoviedb.org/{ttype or 'movie'}/{tmdb}" if tmdb else None)
        out.append(((f.get("name") or key), year or 0, [_cell(_names(title_ru, title), link=url), _cell(f.get("name") or key),
                    _cell(year)]))
    return [row for _, _, row in sorted(out, key=lambda x: (x[0], x[1]))]


# «Баллы» tab = the constants (owner 2026-10-25: «баллы — это категория, за что сколько баллов даётся, и каждое медиа имеет
# формулу, которая обращается к этим константам, с множителями»): one row per weight of config/settings.toml [sort.weights];
# every media tab has a «Баллы» column = Σ constant × the row's coefficient (hidden «f:…» columns). Row k+2 ↔ CONSTS[k].
CONSTS = [
    ("owner_want", "❤️ в твоём списке", "за тайтл"), ("owner_maybe", "💛 в твоём списке", "за тайтл"),
    ("owner_possible", "🔵 в твоём списке", "за тайтл"), ("watchlist", "«хочу» (сериалы)", "за тайтл"),
    ("upgrade", "апгрейд скачанного", "есть раздача лучше твоей копии"), ("franchise", "франшиза", "ты следишь за франшизой"),
    ("imax", "IMAX", "снят / шёл в IMAX"), ("auto_want", "новинка с высоким RT ❤️", "если нет в твоём списке"),
    ("auto_maybe", "новинка с высоким RT 💛", "если нет в твоём списке"), ("new_since", "новинка на rutor с 2026", "за тайтл"),
    ("people_per_point", "важные люди", "× сумма баллов важности людей (потолок — people_max в настройках)"),
    ("hdr.DV", "Dolby Vision", "у лучшей раздачи"), ("hdr.HDR10+", "HDR10+", "у лучшей раздачи"),
    ("hdr.DV*", "DV из HDR10 (статические)", "у лучшей раздачи"), ("hdr.HDR10", "HDR10", "у лучшей раздачи"),
    ("hdr.HLG", "HLG", "у лучшей раздачи"), ("uhd", "4K", "у лучшей раздачи"),
    ("voice.dub", "дубляж", "у лучшей раздачи"), ("voice.mvo", "проф. многоголосый", "у лучшей раздачи"),
    ("voice.dvo", "проф. двухголосый", "у лучшей раздачи"), ("voice.svo", "проф. одноголосый", "у лучшей раздачи"),
    ("voice.avo", "авторский", "у лучшей раздачи"), ("voice.amateur", "любительский", "у лучшей раздачи"),
    ("wishlist", "в желаемом (Google TV)", "за тайтл"), ("imdb_per_point_over_5", "IMDb", "× (рейтинг − 5), если выше 5"),
    ("rt_per_point_over_50", "RT", "× (оценка − 50), если выше 50"),
]
SCORE_COLUMNS = [("Признак", 300), ("Баллы", 80), ("Как считается", 520)]
SCORE_NOTES = ["ТОП (список в настройках) — всегда в самом верху, в названном тобой порядке; баллы не нужны",
               "Срочно (🟢 обязательно скачать, 🧞‍♀ заказы, ❤️ без галочки «Костя Хочет») — сразу под ТОП",
               "Остальное — по колонке «Баллы» в каждом листе; поменяй число здесь — пересчитаются все листы, "
               "при следующей пересборке оно запишется в настройки"]
WEIGHTS_WRITTEN = db.ROOT / "data" / "cache" / "sheet_weights_written.json"


def _weight(w: dict, key: str):
    a, _, b = key.partition(".")
    return (w.get(a) or {}).get(b) if b else w.get(a)


def _score_rows(rows: list[dict] | None = None) -> list[list]:
    w = rank.settings()["sort"]["weights"]
    out = [[_cell(label), _cell(_weight(w, k)), _cell(how)] for k, label, how in CONSTS]
    return out + [[_cell(n), {}, {}] for n in SCORE_NOTES]


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _score_formula(columns, row_no: int) -> str:
    """=Σ 'Баллы'!$B$k × coefficient of the row (operators only: the ru locale changes function separators)."""
    idx = {h: i for i, (h, _) in enumerate(columns)}
    return "=" + "+".join(f"'Баллы'!$B${k + 2}*{_col_letter(idx['f:' + key])}{row_no}" for k, (key, _, _) in enumerate(CONSTS))


def read_back_weights(sh) -> dict:
    """The owner's edits of the «Баллы» constants → config/settings.toml [sort.weights] (a value differing from what the
    script wrote last time is his edit — same rule as the marks)."""
    stats = {"weights": 0}
    try:
        values = sh.worksheet("Баллы").get_all_values()
    except Exception:
        return stats
    if not values or values[0][:2] != ["Признак", "Баллы"] or not WEIGHTS_WRITTEN.exists():
        return stats
    written = json.loads(WEIGHTS_WRITTEN.read_text(encoding="utf-8"))
    path = db.ROOT / "config" / "settings.toml"
    text = path.read_text(encoding="utf-8")
    for (key, _, _), row in zip(CONSTS, values[1:]):
        raw = (row[1] if len(row) > 1 else "").replace(",", ".").replace(" ", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        val = int(val) if val == int(val) else val
        if key in written and val != written[key]:
            text = _set_weight(text, key, val)
            stats["weights"] += 1
    if stats["weights"]:
        path.write_text(text, encoding="utf-8")
    return stats


def _set_weight(text: str, key: str, val) -> str:
    """Replace one [sort.weights] value in settings.toml text (scalar `key = N` or `"sub" = N` inside an inline table)."""
    a, _, b = key.partition(".")
    lines = text.split("\n")
    sec = False
    for i, line in enumerate(lines):
        if line.startswith("["):
            sec = line.strip() == "[sort.weights]"
            continue
        if not sec:
            continue
        if not b and re.match(rf"^{re.escape(a)}\s*=", line):
            lines[i] = re.sub(r"=\s*[-0-9.]+", f"= {val}", line, count=1)
        elif b and re.match(rf"^{re.escape(a)}\s*=\s*\{{", line):
            lines[i] = re.sub(rf'("{re.escape(b)}"\s*=\s*)[-0-9.]+', lambda m: f"{m.group(1)}{val}", line, count=1)
    return "\n".join(lines)


def _remember_weights() -> None:
    w = rank.settings()["sort"]["weights"]
    WEIGHTS_WRITTEN.parent.mkdir(parents=True, exist_ok=True)
    WEIGHTS_WRITTEN.write_text(json.dumps({k: _weight(w, k) for k, _, _ in CONSTS}, ensure_ascii=False), encoding="utf-8")


def _playlist_rows(conn) -> list[list]:
    """«Плейлисты» tab (owner, 2026-10-24): links to the unlisted trailer playlists (ids — config/private.toml)."""
    from . import playlist_notes
    yt = db.private().get("youtube", {})
    ro = playlist_notes.connect_ro()
    desired = json.loads(playlist_notes.DESIRED.read_text(encoding="utf-8")) if playlist_notes.DESIRED.exists() else None
    rows = []
    for key, name in playlist_notes.PLAYLISTS.items():
        pid = yt.get(f"playlist_{key}")
        url = f"https://www.youtube.com/playlist?list={pid}" if pid else None
        # the checked list that is in YouTube (desired), not members() before dedup / trailer checks (code review 2026-10-24)
        n = len(desired.get("playlists", {}).get(key, [])) if desired else len(playlist_notes.members(ro, key)[0])
        rows.append([_cell(name if url else f"{name} (ещё не создан)", link=url, bold=True), _cell(n),
                     _cell(PLAYLIST_ABOUT.get(key, ""))])
    return rows


# R77 (owner, 2026-10-23): so many series that they get their own tabs; «Главная» keeps the films
MARK_TABS = ("Фильмы", "Сериалы", "Мультфильмы", "Мультсериалы", "Аниме")   # «Фильмы» was «Главная» until 2026-10-25


def owner_cartoons(conn) -> set[int]:
    """Cartoons of the franchises Яна dislikes and the owner likes (DC, Marvel, Mario, Star Wars…): «это ко мне из мультиков
    в фильмы перемести» (owner, 2026-10-24) → shown with the films / series, not on the cartoon tabs."""
    out = set()
    for d in rank.settings()["responsible"].values():
        if d.get("owner_cartoons"):
            ex = rank.excluded_for(conn, d)
            out |= {r[0] for r in conn.execute("SELECT id FROM titles WHERE is_animation = 1") if r[0] in ex}
    return out


_OWNER_CARTOONS: set[int] = set()


def _tab(t: dict) -> str:
    if t.get("tracker_category") == "anime":
        return "Аниме"
    if t.get("is_animation") and t["id"] not in _OWNER_CARTOONS:
        return "Мультфильмы" if t["kind"] == "movie" else "Мультсериалы"
    return "Фильмы" if t["kind"] == "movie" else "Сериалы"


def rebuild(conn) -> dict:
    from .locks import lock
    with lock("sheet", timeout=3600):   # one writer of the Google Sheet at a time (semaphore, 2026-10-24)
        return _rebuild(conn)


def _rebuild(conn) -> dict:
    gc = client()
    sh, created = open_spreadsheet(gc)
    stats = {"created": created, "url": sh.url}
    if not created:
        stats["read_back"] = read_back(conn, sh)
        stats["read_back"].update(read_back_weights(sh))   # constants edited on «Баллы» → settings (before compute)
    rows = rank.compute(conn)
    _OWNER_CARTOONS.clear()
    _OWNER_CARTOONS.update(owner_cartoons(conn))
    people = _people(conn)
    collections = {r[0]: r[1] for r in conn.execute("SELECT id, name_ru FROM collections")}
    tabs = {t: [] for t in MARK_TABS}
    quality = _quality_rows(rows, plex_added(conn))
    rows = [r for r in rows if not r.get("quality_watch")]   # those live only on «Обновить качество» (owner, 2026-10-24)
    shown: dict[str, list] = {t: [] for t in MARK_TABS}
    for r in rows:
        if not r["hidden_reasons"]:
            shown[_tab(r["title"])].append(r)
    groups = _group_keys(conn, rows)
    blocks: dict[str, list] = {}
    for tab, rs in shown.items():
        ordered = _grouped(rs, groups)
        tabs[tab] = [_main_row(r, people, collections) for r in ordered]
        blocks[tab] = _blocks(ordered, groups)
    # watched titles never appear outside «Просмотренные» (owner, 2026-10-23)
    order = [*((t, MAIN_COLUMNS, tabs[t], True) for t in MARK_TABS), ("Обновить качество", QUALITY_COLUMNS, quality, False),
             # no «Просмотренные» (owner 2026-10-25: «лист вообще бесполезный, для купить есть свой лист»)
             ("Купить", BUY_COLUMNS, _buy_rows(conn), False), ("Плейлисты", PLAYLIST_COLUMNS, _playlist_rows(conn), False),
             ("Нет на трекерах", NOT_FOUND_COLUMNS, _not_found_rows(conn), False),
             ("Баллы", SCORE_COLUMNS, _score_rows(), False)]   # no «Все» tab (owner 2026-10-25: «я такой лист не просил»)
    # «Баллы» first: the media tabs' formulas refer to it
    for title, cols, data, marks in sorted(order, key=lambda o: o[0] != "Баллы"):
        ws = _ws(sh, title, len(data) + 1, len(cols))
        _write(sh, ws, cols, data, marks, blocks.get(title))
        stats[title] = len(data)
    names = [o[0] for o in order]
    for ws in sh.worksheets():  # the default empty first sheet of a new spreadsheet
        if ws.title not in names and len(sh.worksheets()) > len(names):
            sh.del_worksheet(ws)
    sh.reorder_worksheets([sh.worksheet(n) for n in names])
    _remember_written([r for r in rows if "видел" not in r["hidden_reasons"]])
    _remember_weights()
    return stats
