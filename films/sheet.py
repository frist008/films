"""Google Sheets output (stage 7).

Access model (owner, 2026-10-23): the spreadsheet belongs to <google.main> and only the owner edits it;
everyone with the link can view. The script acts AS the owner through OAuth (desktop client from GCP
project films-db on <google.cloud>); no service accounts, no other editors.
Scope is drive.file: the app sees only files it created itself (the films spreadsheet).

rebuild(): read the owner's marks back into the DB first (never lose them), then rewrite every sheet.
"""
import json
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
COLORS = {"queued": (0.204, 0.596, 0.325), "wanted": (0.576, 0.769, 0.490), "want": (0.957, 0.8, 0.8), "maybe": (1.0, 0.949, 0.8), "order": (0.851, 0.824, 0.914),
          "upgrade": (0.851, 0.918, 0.827), "dima": (0.812, 0.886, 0.953),
          "resp_dima": (0.706, 0.655, 0.839), "resp_yana": (0.957, 0.761, 0.851)}  # name cell: Дима фиолетовый, Яна розовый

# (header, width px) — the order is the sheet's column order; editable owner columns come first
MAIN_COLUMNS = [
    ("Метка", 120), ("Заказчик", 90), ("Заметка", 160), ("Ответственный", 95), ("Список", 55), ("Вариант", 110),
    ("Название", 260), ("Оригинальное название", 200), ("Год", 50), ("Тип", 80), ("Качество", 170),
    ("Озвучка", 230), ("Форс. субтитры", 80), ("Битрейт H.264-экв., Мбит/с", 90), ("Размер, ГБ", 70),
    ("IMAX", 45), ("IMDb", 50), ("RT критики", 60), ("RT зрители", 60), ("Жанры", 150), ("Длит., мин", 55),
    ("Страны", 120), ("Режиссёр", 150), ("Сценарист", 150), ("В ролях", 260), ("Студия", 150),
    ("Бюджет / сборы", 120), ("Сезон: серии, статус", 140), ("Ссылки", 200), ("У меня (Plex)", 170),
    ("Апгрейд", 200), ("Магазин", 110), ("Франшиза", 150), ("Важные люди", 120), ("Почему скрыт", 140),
    ("title_id", 60), ("variant", 40),
]


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


def _ws(sh, title: str, rows: int, cols: int):
    try:
        ws = sh.worksheet(title)
        ws.resize(rows=max(rows, 2), cols=cols)
        return ws
    except Exception:  # gspread.WorksheetNotFound
        return sh.add_worksheet(title=title, rows=max(rows, 2), cols=cols)


# ---------------------------------------------------------------------------------------------- read back

def read_back(conn, sh) -> dict:
    """Owner's marks from the title tabs (Главная, Сериалы, …) and «Все» → user_marks / responsibility. Rows matched by title_id."""
    stats = {"marks": 0, "responsible": 0}
    seen = set()
    for title in (*MARK_TABS, "Все"):
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
                mark = MARKS.get(get("Метка"))
                ordered_by, note = get("Заказчик") or None, get("Заметка") or None
                cur = conn.execute("SELECT mark, ordered_by, note FROM user_marks WHERE title_id = ?", (tid,)).fetchone()
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


def _links_cell(links: list[tuple[str, str]]):
    links = [(t, u) for t, u in links if u]
    if not links:
        return {}
    text, runs, pos = "", [], 0
    for i, (t, u) in enumerate(links):
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


def _voice_text(release: dict) -> str:
    parts = []
    for a in _j(release.get("audio"), []):
        if a.get("lang") not in ("ru", "uk"):
            continue
        label = VOICE_RU.get(a.get("kind") or "", a.get("kind") or "?")
        s = f"{a['lang'].upper()}: {label}" + (f" ({a['studio']})" if a.get("studio") else "")
        if s not in parts:
            parts.append(s)
    return "; ".join(parts) or VOICE_RU.get(release.get("best_voice") or "none", "")


def _money(v):
    return None if not v else (f"${v / 1e9:.2f}B" if v >= 1e9 else f"${v / 1e6:.0f}M")


def _people(conn) -> dict:
    out: dict[int, dict] = {}
    for tid, role, name, job, ord_ in conn.execute(
            """SELECT c.title_id, c.role, p.name, c.job, c.ord FROM credits c JOIN people p ON p.id = c.person_id
               ORDER BY c.title_id, c.role, COALESCE(c.ord, 999)"""):
        d = out.setdefault(tid, {"director": [], "writer": [], "actor": []})
        if role in d and len(d[role]) < (5 if role == "actor" else 3):
            d[role].append(name)
    return out


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
    have = r["have"]
    have_txt = None
    if have:
        have_txt = " ".join(str(x) for x in (have.get("resolution"), have.get("source_type"),
                                               rank.hdr_class(have["hdr"]) if have["hdr"] else None,
                                               "RU/UK" if have["has_ru_uk"] else
                                               "без RU/UK" if have["has_ru_uk"] is False else None) if x)
    if r.get("queued"):   # R47: put on download in qBittorrent → green row, white text, «В очереди»
        have_txt = "В очереди"   # exact text: the colour rule compares it (no functions — ru locale separators)
    elif r.get("wanted") and not have_txt:   # R48: the owner's «хочу» list → lighter green, «Хочу»
        have_txt = "Хочу"
    store = "куплено" if r["owned"] else "в желаемом" if r["in_wishlist"] else None
    imdb_url = f"https://www.imdb.com/title/{t['imdb_id'] or t['series_imdb_id']}/" if (t["imdb_id"] or t["series_imdb_id"]) else None
    return [
        _cell(MARK_LABEL.get(r["mark"].get("mark"))), _cell(r["mark"].get("ordered_by")), _cell(r["mark"].get("note")),
        _cell(resp_txt), _cell(("🟢 " if r.get("urgent") and not r.get("list_marker") else "") +
                               ({"want": "❤️", "maybe": "💛", "possible": "🔵", "order": "🧞‍♀", "yana": "🧸"}
                                .get(r.get("list_marker"), "") or "") or None),
        _cell(r["variant"]),
        _cell(t["title_ru"] or t["title_original"], link=rel.get("url"), bold=r["variant_index"] == 0),
        _cell(t["title_original"]), _cell(t["year"]), _cell(kind), _cell(r["quality"]),
        _cell(_voice_text(rel)), _cell(", ".join(_j(rel.get("forced_subs"), [])).upper()),
        _cell(round(rel["h264_equiv_kbps"] / 1000, 1) if rel.get("h264_equiv_kbps") else None),
        _cell(round(rel["size_bytes"] / 1024 ** 3, 1) if rel.get("size_bytes") else None),
        _cell(bool(t.get("is_imax"))), _cell(t.get("imdb_rating")), _cell(t.get("rt_critics")), _cell(t.get("rt_audience")),
        _cell(", ".join(_j(t.get("genres"), []))), _cell(t.get("runtime_min")), _cell(", ".join(_j(t.get("countries"), []))),
        _cell(", ".join(p.get("director", []))), _cell(", ".join(p.get("writer", []))), _cell(", ".join(p.get("actor", []))),
        _cell(", ".join(_j(t.get("studios"), [])[:2])),
        _cell(" / ".join(x for x in (_money(t.get("budget_usd")), _money(t.get("box_office_usd"))) if x) or None),
        _cell(season_info),
        _links_cell([("Трейлер", t.get("trailer_ru_url")) if t.get("trailer_ru_url") else
                     ("Трейлер (" + {"uk": "укр", "original": "ориг.", "ru_sub": "рус. суб.", "any": "другой"}
                      .get(t.get("trailer_alt_kind"), "?") + ")", t.get("trailer_alt_url")),
                     ("IMDb", imdb_url), ("RT", t.get("rt_url")),
                     ("Картинка", t.get("poster_url"))]),
        _cell(have_txt), _cell(", ".join(r["upgrade"])), _cell(store),
        _cell(collections.get(t.get("collection_id")) if r["franchise"] else None), _cell(", ".join(r["people_hits"])),
        _cell(", ".join(r["hidden_reasons"])), _cell(t["id"]), _cell(r["variant_index"]),
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
        rows.append([_cell(t["title_ru"] or t["title_original"], link=imdb_url), _cell(t["title_original"]),
                     _cell(t["year"]), _cell((t["watched_at"] or "")[:10]), _cell(status),
                     _cell(best["price"] if best else None), _cell(best["currency"] if best else None),
                     _cell(best["discount_pct"] if best and best["discount_pct"] else None),
                     _cell(", ".join(audio).upper()), _cell(best["url"] if best else None, link=best["url"] if best else None),
                     _cell(t["id"])])
    return rows


WATCHED_COLUMNS = [("Название", 260), ("Оригинальное название", 200), ("Год", 50), ("Видел", 90), ("Статус", 200),
                   ("Цена", 70), ("Валюта", 60), ("Скидка, %", 70), ("Озвучка в магазине", 110), ("Ссылка", 220),
                   ("title_id", 60)]


# ---------------------------------------------------------------------------------------------- write

def _write(sh, ws, columns, rows, mark_col: bool):
    sheet_id = ws.id
    header = [{"userEnteredValue": {"stringValue": h}, "userEnteredFormat": {
        "textFormat": {"bold": True}, "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
        "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}} for h, _ in columns]
    ncol = len(columns)
    reqs = [
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
            "rowCount": len(rows) + 1, "columnCount": ncol, "frozenRowCount": 1}},
            "fields": "gridProperties(rowCount,columnCount,frozenRowCount)"}},
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
    for start in range(0, len(data), chunk):
        sh.batch_update({"requests": [{"updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": start, "columnIndex": 0},
            "rows": [{"values": r} for r in data[start:start + chunk]],
            "fields": "userEnteredValue,textFormatRuns,userEnteredFormat"}}]})
    fmt = [{"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                    "startIndex": i, "endIndex": i + 1},
                                          "properties": {"pixelSize": w}, "fields": "pixelSize"}}
           for i, (_, w) in enumerate(columns)]
    fmt.append({"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                                         "endRowIndex": len(rows) + 1, "startColumnIndex": 0,
                                                         "endColumnIndex": ncol}}}})
    hidden = [i for i, (h, _) in enumerate(columns) if h in ("title_id", "variant")]
    for i in hidden:
        fmt.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                                            "startIndex": i, "endIndex": i + 1},
                                                  "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}})
    if mark_col:
        rng = {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": len(rows) + 1, "startColumnIndex": 0,
               "endColumnIndex": ncol}
        fmt.append({"setDataValidation": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": len(rows) + 1,
                                                    "startColumnIndex": 0, "endColumnIndex": 1},
                                          "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                                              {"userEnteredValue": k} for k in MARKS]}, "showCustomUi": True,
                                              "strict": False}}})
        col = {h: chr(ord("A") + i) if i < 26 else "A" + chr(ord("A") + i - 26) for i, (h, _) in enumerate(columns)}
        rules = [("queued", f'=${col["У меня (Plex)"]}2="В очереди"'),
                 ("wanted", f'=${col["У меня (Plex)"]}2="Хочу"'), ("want", f'=$A2="🔴 Очень хочу"'), ("maybe", f'=$A2="🟡 Глянуть"'), ("order", f'=$A2="🟣 Заказ"'),
                 ("upgrade", f'=($A2="")*(${col["Апгрейд"]}2<>"")'),
                 ("dima", f'=($A2="")*((${col["Ответственный"]}2="Дима")+(${col["Ответственный"]}2="Дима?")'
                          f'+(${col["Ответственный"]}2="Яна")+(${col["Ответственный"]}2="Яна?"))')]
        # the «Ответственный» cell itself: Дима — violet, Яна — pink (owner 2026-10-23); first in the rule list
        ci = [h for h, _ in columns].index("Ответственный")
        cell = dict(rng, startColumnIndex=ci, endColumnIndex=ci + 1)
        cell_rules = [("resp_dima", f'=(${col["Ответственный"]}2="Дима")+(${col["Ответственный"]}2="Дима?")'),
                      ("resp_yana", f'=(${col["Ответственный"]}2="Яна")+(${col["Ответственный"]}2="Яна?")')]
        # no function-argument separators: the spreadsheet locale (ru) uses ";" instead of ","
        for i, (key, formula) in enumerate(cell_rules):
            r, g, b = COLORS[key]
            fmt.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [cell], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": {"red": r, "green": g, "blue": b}}}}}})
        for i, (key, formula) in enumerate(rules, start=len(cell_rules)):
            r, g, b = COLORS[key]
            fmt.append({"addConditionalFormatRule": {"index": i, "rule": {"ranges": [rng], "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": {"red": r, "green": g, "blue": b},
                           **({"textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}}}
                              if key == "queued" else {})}}}}})
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


BUY_COLUMNS = [("Название", 260), ("Оригинальное название", 200), ("Год", 50), ("Озвучка", 90), ("Качество", 70),
               ("Цена покупки", 90), ("Валюта", 60), ("Скидка, %", 70), ("Старая цена", 80), ("Ссылка", 220), ("title_id", 60)]
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
        out.append([_cell(r["title_ru"] or r["title_original"], link=imdb_url), _cell(r["title_original"]), _cell(r["t_year"]),
                    _cell(", ".join(sorted(langs)).upper()), _cell(_QNAME.get(q, q)), _cell(price), _cell(r["currency"] or "UAH"),
                    _cell(round((old - price) * 100 / old) if old else None), _cell(old), _cell(r["url"], link=r["url"]),
                    _cell(r["title_id"])])
    return out


PLAYLIST_COLUMNS = [("Плейлист", 200), ("Ссылка", 360), ("Фильмов в списке", 110), ("Что внутри", 520)]
PLAYLIST_ABOUT = {
    "kostya_want": "❤️ из списка владельца — трейлеры того, что очень хочется посмотреть",
    "probably": "💛/🟡 из списка владельца — вероятно хочу",
    "possible": "без смайлика из списка владельца — возможно",
    "dima": "ответственный Дима — комедии",
    "yana": "ответственная Яна — мультфильмы, которые мы ещё не видели",
}


SCORE_PARTS = ("твой список", "хочу (сериалы)", "апгрейд", "франшиза", "IMAX", "новинка RT", "новинка 2026", "люди", "HDR",
               "4K", "озвучка", "желаемое", "IMDb", "RT")
SCORE_COLUMNS = [("№", 50), ("Лист", 100), ("Название", 260), ("Год", 50), ("Срочно", 60), ("Итого", 60),
                 *((p, 70) for p in SCORE_PARTS), ("title_id", 60)]


def _score_rows(rows: list[dict]) -> list[list]:
    """«Баллы» tab (owner, 2026-10-24: «нужно видеть список глазами в таблице»): every visible title in the sheet order, its
    total and the points of every feature (weights — config/settings.toml [sort.weights]); «Срочно» sits above any sum."""
    out = []
    for r in rows:
        if r["hidden_reasons"] or r["variant_index"]:
            continue
        t = r["title"]
        name = (t["title_ru"] or t["title_original"]) + (f" — сезон {t['season_number']}" if t.get("season_number") else "")
        url = f"https://www.imdb.com/title/{t['imdb_id']}/" if t.get("imdb_id") else None
        parts = r["score_parts"]
        out.append([_cell(len(out) + 1), _cell(_tab(t)), _cell(name, link=url), _cell(t.get("year")),
                    _cell("ТОП" if r.get("top") else "да" if r["urgent"] else ""), _cell(r["score"], bold=True),
                    *(_cell(parts.get(p) or None) for p in SCORE_PARTS), _cell(t["id"])])
    return out


def _playlist_rows(conn) -> list[list]:
    """«Плейлисты» tab (owner, 2026-10-24): links to the unlisted trailer playlists (ids — config/private.toml)."""
    from . import playlist_notes
    yt = db.private().get("youtube", {})
    ro = playlist_notes.connect_ro()
    rows = []
    for key, name in playlist_notes.PLAYLISTS.items():
        pid = yt.get(f"playlist_{key}")
        url = f"https://www.youtube.com/playlist?list={pid}" if pid else None
        members, _ = playlist_notes.members(ro, key)
        rows.append([_cell(name, bold=True), _cell(url or "ещё не создан", link=url), _cell(len(members)),
                     _cell(PLAYLIST_ABOUT.get(key, ""))])
    return rows


# R77 (owner, 2026-10-23): so many series that they get their own tabs; «Главная» keeps the films
MARK_TABS = ("Главная", "Сериалы", "Мультфильмы", "Мультсериалы", "Аниме")


def owner_cartoons(conn) -> set[int]:
    """Cartoons of the franchises Яна dislikes and the owner likes (DC, Marvel, Mario, Star Wars…): «это ко мне из мультиков
    в фильмы перемести» (owner, 2026-10-24) → shown with the films / series, not on the cartoon tabs."""
    keys = [k for d in rank.settings()["responsible"].values() if d.get("owner_cartoons") for k in d.get("exclude_franchises", [])]
    if not keys:
        return set()
    return {r[0] for r in conn.execute(
        f"""SELECT t.id FROM titles t JOIN franchise_members m ON m.imdb_id = coalesce(t.imdb_id, t.series_imdb_id)
            WHERE t.is_animation = 1 AND m.franchise_key IN ({",".join("?" * len(keys))}) AND m.confidence != 'review'""", keys)}


_OWNER_CARTOONS: set[int] = set()


def _tab(t: dict) -> str:
    if t.get("tracker_category") == "anime":
        return "Аниме"
    if t.get("is_animation") and t["id"] not in _OWNER_CARTOONS:
        return "Мультфильмы" if t["kind"] == "movie" else "Мультсериалы"
    return "Главная" if t["kind"] == "movie" else "Сериалы"


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
    rows = rank.compute(conn)
    _OWNER_CARTOONS.clear()
    _OWNER_CARTOONS.update(owner_cartoons(conn))
    people = _people(conn)
    collections = {r[0]: r[1] for r in conn.execute("SELECT id, name_ru FROM collections")}
    tabs = {t: [] for t in MARK_TABS}
    for r in rows:
        if not r["hidden_reasons"]:
            tabs[_tab(r["title"])].append(_main_row(r, people, collections))
    # watched titles never appear outside «Просмотренные» (owner, 2026-10-23)
    everything = [_main_row(r, people, collections) for r in rows if "видел" not in r["hidden_reasons"]]
    watched = _watched_rows(conn)
    order = [*((t, MAIN_COLUMNS, tabs[t], True) for t in MARK_TABS), ("Просмотренные", WATCHED_COLUMNS, watched, False),
             ("Купить", BUY_COLUMNS, _buy_rows(conn), False), ("Плейлисты", PLAYLIST_COLUMNS, _playlist_rows(conn), False),
             ("Баллы", SCORE_COLUMNS, _score_rows(rows), False), ("Все", MAIN_COLUMNS, everything, True)]
    for title, cols, data, marks in order:
        ws = _ws(sh, title, len(data) + 1, len(cols))
        _write(sh, ws, cols, data, marks)
        stats[title] = len(data)
    names = [o[0] for o in order]
    for ws in sh.worksheets():  # the default empty first sheet of a new spreadsheet
        if ws.title not in names and len(sh.worksheets()) > len(names):
            sh.del_worksheet(ws)
    sh.reorder_worksheets([sh.worksheet(n) for n in names])
    _remember_written([r for r in rows if "видел" not in r["hidden_reasons"]])
    return stats
