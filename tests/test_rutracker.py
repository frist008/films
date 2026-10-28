"""Tests for the rutracker collector: films/parse/rutracker.py, films/browser/rutracker_collect.js (parsers run through
node, skipped without it) and films/sources/rutracker.py (ingest into a fresh, migrated in-memory DB).

The fixtures in tests/fixtures/rutracker/ are written by hand from the rows saved in
data/cache/rutracker/south_park_rows.json (titles, sizes, seeders as collected in Chrome) and rutracker's usual markup —
not saved pages; check them against the first live run. Run:  python -X utf8 -m unittest tests.test_rutracker
"""
import json
import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path

from films import db
from films.parse.rutracker import (_name_ru_audio, category_for_forum, compact_topic_html, parse_reged, parse_title_only, parse_topic,
                                   rutor_style_name, split_topic)
from films.sources import rutracker as rt

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "rutracker"
JS = ROOT / "films" / "browser" / "rutracker_collect.js"
SAMPLE = json.loads((ROOT / "data" / "cache" / "rutracker" / "south_park_rows.json").read_text(encoding="utf-8")) \
    if (ROOT / "data" / "cache" / "rutracker" / "south_park_rows.json").exists() else None
NODE = shutil.which("node")


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def node(code: str, *args) -> object:
    out = subprocess.run([NODE, "-e", code, str(JS), *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                         check=True)
    return json.loads(out.stdout)


class TestTitle(unittest.TestCase):
    def test_season_episodes_quality_voices(self):
        n, w = rutor_style_name(
            "Южный Парк / South Park / Сезон: 27 / Серии: 1-5 из 5 (Мэтт Стоун, Трэй Паркер / Matt Stone, Trey Parker) "
            "[2025, США, комедия, сатира, WEB-DL 2160p, SDR] DVO (Кубик в Кубе) + 3 MVO + Original + Sub (Rus, Eng)")
        self.assertEqual(n, "Южный Парк / South Park [27x1-5 из 5] (2025) WEB-DL 2160p | SDR | DVO, MVO | Кубик в Кубе")
        self.assertEqual(w, [])
        p = parse_title_only("Южный Парк / South Park / Сезон: 27 / Серии: 1-5 из 5 (Мэтт Стоун / Matt Stone) "
                             "[2025, США, комедия, WEB-DL 2160p, SDR] DVO (Кубик в Кубе) + 3 MVO + Original + Sub (Rus, Eng)")
        np = p["name_parsed"]
        self.assertEqual((np["title_ru"], np["title_original"]), ("Южный Парк", "South Park"))
        self.assertEqual((np["seasons"], np["episodes"], np["episodes_total"]), ([27], "1-5", 5))
        self.assertEqual((np["source_type"], np["resolution"], np["hdr"]), ("WEB-DL", 2160, []))
        self.assertEqual(np["best_voice"], "mvo")

    def test_two_seasons_list_and_ukr(self):
        n, _ = rutor_style_name(
            "Южный Парк / South Park / Сезон: 14, 15 / Серии: 1-28 из 28 (Мэт Стоун, Трэй Паркер / Matt Stone, Trey Parker) "
            "[2010-2011, США, комедия, BDRip 720p] 2x DVO (2x2, Кубик в Кубе) + Ukr + Original + Sub (Rus, Eng)")
        self.assertEqual(n, "Южный Парк / South Park [14-15x1-28 из 28] (2010-2011) BDRip 720p | DVO | 2x2, Кубик в Кубе | UKR")
        np = parse_title_only(n)["name_parsed"]  # already rutor-shaped: parsing it again is stable
        self.assertEqual(np["seasons"], [14, 15])

    def test_pack_directors_before_seasons_nested_parens(self):
        n, _ = rutor_style_name(
            "[MULT] Южный Парк / South Park (Мэтт Стоун, Трэй Паркер / Matt Stone, Trey Parker) Сезоны: 1-28 / "
            "Серии: 1-333 из 333 + 5 фильмов [1997-2025, мультфильм, комедия, HDRip, WEB-DLRip] [MP4, 960x] "
            "VO (Д. Пучков (Гоблин)), DVO (Paramount Comedy, MTV и др.) + Original + Sub (Rus, Eng)")
        self.assertEqual(n, "Южный Парк / South Park [1-28x1-333 из 333 + 5 фильмов] (1997-2025) HDRip WEB-DLRip | "
                            "VO, DVO | Д. Пучков (Гоблин), Paramount Comedy, MTV")
        np = parse_title_only(n)["name_parsed"]
        self.assertEqual(np["seasons"], list(range(1, 29)))
        self.assertEqual((np["year_from"], np["year_to"]), (1997, 2025))

    def test_film_ukr_voice_and_leading_tag(self):
        n, _ = rutor_style_name(
            "Южный Парк: Не предназначено для просмотра детьми / South Park: Not Suitable for Children (Трей Паркер / "
            "Trey Parker) [2023, США, Комедия, Сатира, WEB-DL 1080p] 5x MVO (Red Head Sound, HDRezka, RuDub, TVShows, "
            "Coldfilm) + MVO Ukr + Original + Sub (Rus, Eng)")
        self.assertEqual(n, "Южный Парк: Не предназначено для просмотра детьми / South Park: Not Suitable for Children "
                            "(2023) WEB-DL 1080p | MVO | Red Head Sound, HDRezka, RuDub, TVShows, Coldfilm | UKR")
        n, _ = rutor_style_name("[MULTSERIAL] Южный парк / South Park / Сезон: 14-15 (Мэтт Стоун, Трей Паркер)[28 Серии]"
                                "[2010-2011, мультсериал, комедия, пародия, WEB-DLRip][VO-Production]")
        self.assertEqual(n, "Южный парк / South Park [S14-15] (2010-2011) WEB-DLRip")
        # review round 11: anime episodes in brackets, no season — episodes, not a film
        n, _ = rutor_style_name("[ANIME] Одним куском / One Piece [Серии 501-519] (Уда Коносукэ) [2011, приключения, HDTVRip]")
        self.assertEqual(n, "Одним куском / One Piece [501-519] (2011) HDTVRip")
        n, _ = rutor_style_name("Ван-Пис / One Piece [Уда Коносукэ][378 Серий][1999-2008, приключения, DVDRip]")
        self.assertEqual(n, "Ван-Пис / One Piece [1-378 из 378] (1999-2008) DVDRip")
        # review round 12: «[ONA] [10 из 10]» — episodes; an OVA «[1 из 1]» stays one work
        n, _ = rutor_style_name("Киберпанк: Бегущие по краю 2 / Cyberpunk: Edgerunners 2 [ONA] [10 из 10] "
                                "[2026, фантастика, WEB-DL 1080p]")
        self.assertEqual(n, "Киберпанк: Бегущие по краю 2 / Cyberpunk: Edgerunners 2 [1-10 из 10] (2026) WEB-DL 1080p")
        n, _ = rutor_style_name("Боец Баки / Grappler Baki [Асада Юдзи][OVA][1 из 1][1994, боевые искусства, DVDRip]")
        self.assertNotIn("из 1]", n)

    def test_hdr_dv_tags_and_dub(self):
        n, _ = rutor_style_name("Дюна: Часть вторая / Dune: Part Two (Дени Вильнёв / Denis Villeneuve) [2024, США, "
                                "фантастика, UHD BDRemux 2160p, HDR10, Dolby Vision, IMAX] Dub (Невафильм) + Original")
        self.assertEqual(n, "Дюна: Часть вторая / Dune: Part Two (2024) UHD BDRemux 2160p | HDR10, Dolby Vision, IMAX | "
                            "DUB | Невафильм")
        np = parse_title_only(
            "Дюна: Часть вторая / Dune: Part Two (Дени Вильнёв / Denis Villeneuve) [2024, США, фантастика, UHD BDRemux 2160p, "
            "HDR10, Dolby Vision, IMAX] Dub (Невафильм) + Original")["name_parsed"]
        self.assertEqual(np["hdr"], ["DV", "HDR10"])
        self.assertTrue(np["is_imax_cut"])
        self.assertEqual(np["best_voice"], "dub")

    @unittest.skipUnless(SAMPLE, "data/cache/rutracker/south_park_rows.json missing")
    def test_all_sample_rows_have_year_and_titles(self):
        for t, size, seeds, title in SAMPLE["rows"]:
            n, w = rutor_style_name(title)
            np = parse_title_only(title)["name_parsed"]
            self.assertEqual(w, [], title)
            self.assertTrue(np["year_from"], title)
            self.assertIn(np["title_original"], ("South Park", "South Park: Not Suitable for Children"), title)


class TestForumAndDates(unittest.TestCase):
    def test_category(self):
        self.assertEqual(category_for_forum("Зарубежные сериалы (HD Video)"), "foreign_series")
        self.assertEqual(category_for_forum("Мультсериалы (HD Video)"), "animation")
        self.assertEqual(category_for_forum("Иностранные мультфильмы (UHD Video)"), "animation")
        self.assertEqual(category_for_forum("Аниме (HD Video)"), "anime")
        self.assertEqual(category_for_forum("Фильмы 2021-2025"), "foreign_films")
        self.assertEqual(category_for_forum("Фильмы UHD Video"), "foreign_films")
        self.assertEqual(category_for_forum("Русские сериалы"), "russian_series")
        self.assertIsNone(category_for_forum("Наше кино"))
        self.assertIsNone(category_for_forum("Горячие Новинки (Игры для Windows)"))
        self.assertIsNone(category_for_forum("Документальные фильмы (HD Video)"))
        self.assertIsNone(category_for_forum("Саундтреки к фильмам (lossless)"))
        # review 2026-10-28: «[ANIME] Семья шпиона …» in a film forum is anime
        from films.parse.rutracker import category_for
        self.assertEqual(category_for("Фильмы HD Video", "[ANIME] Семья шпиона / Spy x Family / Сезон: 1-3"), "anime")
        self.assertEqual(category_for("Фильмы HD Video", "Семья / Family (2024)"), "foreign_films")
        self.assertIsNone(category_for("Наше кино", "[ANIME] x"))

    def test_reged(self):
        self.assertEqual(parse_reged("24-Июл-25 18:22"), "2025-07-24T18:22:00")
        self.assertEqual(parse_reged("15-Дек-23"), "2023-12-15T00:00:00")
        self.assertEqual(parse_reged(1753381320), "2025-07-24T18:22:00")
        self.assertIsNone(parse_reged("вчера"))


class TestTopic(unittest.TestCase):
    def test_external_russian_audio_file(self):
        # review 2026-10-28: «[RUS(ext), JAP+Sub]» — the Russian voice-over is a separate file, MediaInfo of the video has
        # only Japanese; 80 releases were «no voice-over», «Братство: Последняя фантазия XV» from [top] was hidden
        d = parse_topic(fx("topic_rus_ext_audio.html"))
        ru = [a for a in d["audio"] if a["lang"] == "ru"]
        self.assertEqual([(a["kind"], a.get("external")) for a in ru], [("mvo", True)])
        self.assertEqual(d["best_voice"], "mvo")

    def test_russian_track_named_only_in_title(self):
        t = parse_title_only("Моя геройская академия (ТВ-1) / Boku no Hero Academia [TV] [13 из 13] [RUS(ext), JAP+Sub] "
                             "[2016, экшен, BDRemux] [1080p]", "Аниме (HD Video)")
        self.assertEqual([(a["lang"], a["kind"], a["external"], a.get("kind_assumed")) for a in t["audio"]
                          if a["lang"] == "ru"], [("ru", "amateur", True, True)])
        self.assertEqual(parse_title_only("X / Y [TV] [RUS(Dub, 9*MVO), JAP+Sub] [2016, BDRip] [1080p]")["best_voice"], "dub")
        d = {"audio": [{"lang": "ru", "kind": None}, {"lang": "ja", "kind": None}], "best_voice": None}
        _name_ru_audio(d, "One Outs [TV] [25 из 25] [RUS(int),JAP+Sub] [2008, HDTVRip] [720p]", "")
        self.assertEqual((d["best_voice"], d["audio"][0]["kind_assumed"]), ("amateur", True))
        subs = parse_title_only("X / Y [TV] [JAP+Sub] [RUS Sub] [2016, BDRip] [1080p]")
        self.assertFalse([a for a in subs.get("audio") or [] if a.get("from_name")])   # subtitles are not a voice

    def test_sp_body_title_mediainfo(self):
        d = parse_topic(fx("topic_6731886.html"))
        self.assertEqual(d["rutracker"], {"topic_id": "6731886", "forum": "Зарубежные сериалы (HD Video)", "forum_id": "2366"})
        self.assertEqual(d["category"], "foreign_series")
        self.assertEqual(d["imdb_id"], "tt0121955")    # the second post's link is not read
        self.assertEqual((d["season"], d["episodes"]), ("27", "1-5"))
        self.assertEqual((d["resolution"], d["source_type"], d["video_codec"], d["video_bit_depth"], d["video_kbps"]),
                         (2160, "WEB-DL", "HEVC", 10, 12000))
        self.assertTrue(d["mediainfo_found"])
        self.assertEqual(d["hdr"], [])
        self.assertEqual([(a["lang"], a["kind"], a.get("studio")) for a in d["audio"]],
                         [("ru", "dvo", "Кубик в Кубе"), ("ru", "mvo", "HDRezka Studio"), ("en", "original", None)])
        self.assertEqual(d["best_voice"], "mvo")
        self.assertEqual((d["size_bytes"], d["seeders"], d["leechers"]), (11091673193, 63, 4))
        self.assertEqual(d["btih"], "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(d["added_at"], "2025-07-24T18:22:00")
        self.assertTrue(d["name"].startswith("Южный Парк / South Park / Сезон: 27"))

    def test_script_built_and_nested_spoilers(self):
        d = parse_topic(fx("topic_6466056.html"))
        self.assertEqual(d["category"], "animation")
        self.assertIsNone(d["imdb_id"])
        self.assertTrue(d["mediainfo_found"])     # MediaInfo nested inside «Тех. данные»
        self.assertEqual((d["resolution"], d["video_codec"], d["video_kbps"]), (1080, "AVC", 5800))
        self.assertEqual([(a["lang"], a["kind"]) for a in d["audio"]], [("ru", "mvo"), ("uk", "mvo"), ("en", "original")])
        self.assertEqual(d["forced_subs"], ["ru"])
        self.assertEqual(d["added_at"], "2023-12-15T10:05:00")

    def test_dv5_only(self):
        d = parse_topic(fx("topic_dv5.html"))
        self.assertEqual(d["dv_profile"], 5)
        self.assertTrue(d["dv5_only"])

    def test_compact_roundtrip(self):
        page = fx("topic_6731886.html")
        t = split_topic(page)
        t["topic_id"] = "6731886"
        d1, d2 = parse_topic(page), parse_topic(compact_topic_html(t))
        for k in ("name", "imdb_id", "season", "episodes", "resolution", "video_kbps", "audio", "best_voice", "size_bytes",
                  "seeders", "btih", "added_at", "category", "rutracker"):
            self.assertEqual(d1[k], d2[k], k)
        self.assertNotIn("tt9999999", compact_topic_html(t))   # only the first post is kept


JS_PARSE = r"""
const a = require(process.argv[1]); const fs = require('fs');
const s = a.parseSearch(fs.readFileSync(process.argv[2], 'utf8'));
const t = a.parseTopic(fs.readFileSync(process.argv[3], 'utf8'));
const q = {q: 'South Park', lo: 2023, hi: null, need: ['4k', 'hdr', 'dub']};
console.log(JSON.stringify({
  total: s.total, searchId: s.searchId, rows: s.rows,
  sel: s.rows.map(r => a.select(r, q)), selAny: s.rows.map(r => a.select(r, {q: 'x', lo: 2023, hi: null})),
  topic: Object.assign({}, t, {post_html: t.post_html.length, post_end: t.post_html.slice(-30)}),
  enc: a.enc1251('Южный парк: Ї і'), split1: a.splitQueries({q: 'Alien', lo: 2024, hi: 2024}, 2026),
  split2: a.splitQueries({q: 'South Park', lo: 1997, hi: null, need: ['4k', 'dub']}, 2026),
  problem: [a.pageProblem('<input name="login_username">'), a.pageProblem('<a id="logged-in-username">x</a>'),
            a.pageProblem('<h1>По техническим причинам форум временно недоступен</h1>'),
            a.pageProblem('<a id="logged-in-username">x</a> Zen and the Art of Motorcycle Maintenance')],
}));
"""


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = node(JS_PARSE, FIX / "search_south_park.html", FIX / "topic_6731886.html")

    def test_search_rows(self):
        r = self.r
        self.assertEqual((r["total"], r["searchId"], len(r["rows"])), (9, "AbC123xyz", 9))
        first = r["rows"][0]
        self.assertEqual((first["t"], first["fid"], first["forum"], first["size"], first["seeds"], first["leech"],
                          first["added"]),
                         ("6731886", "2366", "Зарубежные сериалы (HD Video)", 11091673193, 63, 4, 1753381320))
        self.assertTrue(first["title"].startswith("Южный Парк / South Park / Сезон: 27"))
        by_id = {x["t"]: x for x in r["rows"]}
        self.assertEqual(by_id["5000001"]["seeds"], 0)          # «-3» = days without a seeder
        if SAMPLE:
            for t, size, seeds, title in SAMPLE["rows"]:
                self.assertEqual((by_id[t]["size"], by_id[t]["seeds"], by_id[t]["title"]), (size, seeds, title))

    def test_selection(self):
        ids = [x["t"] for x in self.r["rows"]]
        sel = dict(zip(ids, self.r["sel"]))
        # 2023+ and a needed feature in the title (only the 2160p one); old seasons, the game, the yearless row: no
        self.assertEqual([t for t, v in sel.items() if v], ["6731886"])
        sel_any = dict(zip(ids, self.r["selAny"]))
        self.assertEqual([t for t, v in sel_any.items() if v], ["6731886", "6755275", "6466056", "6554047"])

    def test_topic(self):
        t = self.r["topic"]
        self.assertEqual((t["forum"], t["forum_id"], t["size_bytes"], t["seeders"], t["leechers"], t["reged"]),
                         ("Зарубежные сериалы (HD Video)", "2366", 11091673193, 63, 4, "24-Июл-25 18:22"))
        self.assertEqual(t["btih"], "0123456789abcdef0123456789abcdef01234567")
        self.assertGreater(t["post_html"], 3000)
        self.assertTrue(t["post_end"].rstrip().endswith('<div class="clear"></div>'))

    def test_helpers(self):
        self.assertEqual(self.r["enc"], "%DE%E6%ED%FB%E9+%EF%E0%F0%EA%3A+%AF+%B3")
        self.assertEqual(self.r["split1"], ["Alien 2023", "Alien 2024", "Alien 2025"])
        self.assertEqual(self.r["split2"], ["South Park 2160p", "South Park Dub"])
        # a maintenance page is a problem (owner, 2026-10-26: «у ру трекера вроде есть время технических работ»), the
        # same word in a logged-in page (a torrent title) is not
        self.assertEqual(self.r["problem"], ["not_logged_in", None, "maintenance", None])


JS_LOOP = r"""
// the browser loop with fetch / localStorage mocked: rutracker pages from cp1251 files, bridge POSTs captured
const fs = require('fs'); const [js, search, topic, mode] = process.argv.slice(1);
const posted = []; const urls = []; const store = {}; let outage = mode === 'relogin' ? 3 : 0;
globalThis.document = {};
globalThis.localStorage = {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }};
const resp = (path, status) => ({ok: !!path, status: path ? 200 : status,
  arrayBuffer: async () => { const b = fs.readFileSync(path); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); }});
const page = html => ({ok: true, status: 200, arrayBuffer: async () => new TextEncoder().encode(html).buffer});
globalThis.fetch = async (url, opts) => {
  if (url === 'http://127.0.0.1:8765/queries/rutracker') return {ok: true, status: 200, json: async () => (
    {run: 'u', delayMs: 1, known: [], queries: [{q: 'South Park', lo: 2023, hi: null, need: ['4k', 'hdr', 'dub']}]})};
  if (url.startsWith('http://127.0.0.1:8765/ingest/')) { posted.push(JSON.parse(opts.body)); return {ok: true, status: 200}; }
  urls.push(url);
  // relogin mode: maintenance page, then a login page, then logged in again — the loop waits and goes on
  if (outage-- > 0) return page(outage === 2 ? '<h1>Technical maintenance / технические работы</h1>' : '<input name="login_username">');
  if (url.includes('/tracker.php?nm=South+Park')) return resp(search);
  if (url.includes('/viewtopic.php?t=6731886')) return resp(topic);
  return resp(null, 404);
};
require(js);
const msg = mode === 'url' ? __rtCollect({queriesUrl: 'http://127.0.0.1:8765/queries/rutracker'})
  : __rtCollect({run: 't', delayMs: 1, known: [], maxTopics: 5, waitPollMs: 5,
                 queries: [{q: 'South Park', lo: 2023, hi: null, need: ['4k', 'hdr', 'dub'],
                            ...(mode === 'since' ? {since: 1893456000} : {})}]});
(async () => {
  for (let i = 0; i < 200 && !/^(done|stopped|crashed|no queries)/.test(__rtStatus().state || ''); i++) await new Promise(r => setTimeout(r, 50));
  console.log(JSON.stringify({msg, status: __rtStatus(), posted, urls, progress: store.rtc_progress}));
})();
"""


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorLoop(unittest.TestCase):
    def test_loop_posts_one_payload_per_query_and_final(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        for name in ("search_south_park.html", "topic_6731886.html"):   # rutracker serves windows-1251
            (d / name).write_bytes(fx(name).replace('charset="Windows-1251"', "").encode("cp1251", errors="replace"))
        r = node(JS_LOOP, d / "search_south_park.html", d / "topic_6731886.html")
        self.assertEqual(r["status"]["state"], "done", r["status"])
        self.assertEqual(r["status"]["queries"], "1/1")
        self.assertEqual(len(r["posted"]), 2)
        p, fin = r["posted"]
        self.assertTrue(fin["final"])
        self.assertEqual([t["topic_id"] for t in p["topics"]], ["6731886"])
        self.assertEqual(len(p["searches"][0]["rows"]), 9)
        self.assertIn("Южный Парк", p["topics"][0]["title"])      # decoded from cp1251
        self.assertEqual(json.loads(r["progress"]), {"run": "t", "next": 1})
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        st = rt.ingest(conn, r["posted"])
        self.assertEqual((st["new"], st["pages_stored"], st["finals"]), (1, 1, 1))
        row = conn.execute("SELECT resolution, season, seeders FROM releases WHERE source_id='6731886'").fetchone()
        self.assertEqual(tuple(row), (2160, "27", 63))
        # the same through the bridge's job list (GET /queries/rutracker) — the mode run.js uses
        u = node(JS_LOOP, d / "search_south_park.html", d / "topic_6731886.html", "url")
        self.assertEqual((u["status"]["state"], u["status"]["run"], len(u["posted"])), ("done", "u", 2))
        # maintenance → login page → back (owner, 2026-10-26: «там ещё и релогин нужен будет после тех работ»): the loop
        # waits on the same request, reports «waiting» to the bridge, then finishes the query — nothing is lost
        w = node(JS_LOOP, d / "search_south_park.html", d / "topic_6731886.html", "relogin")
        self.assertEqual(w["status"]["state"], "done", w["status"])
        states = [p["state"] for p in w["posted"] if p.get("kind") == "rutracker_status"]
        self.assertEqual(states, ["waiting: maintenance", "waiting: not_logged_in", "waiting: not_logged_in"])
        res = [p for p in w["posted"] if p.get("kind") == "rutracker" and not p.get("final")]
        self.assertEqual((len(res), res[0]["errors"], [t["topic_id"] for t in res[0]["topics"]]), (1, [], ["6731886"]))

    def test_since_sorts_new_first_and_stops_at_old_rows(self):
        # R207 / R212 (owner 2026-10-26: «только на новые от даты последнего сканирования»): with `since` the search is
        # sorted by registration date (o=1&s=2), rows older than the sync point are not selected, no topic is fetched
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        for name in ("search_south_park.html", "topic_6731886.html"):
            (d / name).write_bytes(fx(name).replace('charset="Windows-1251"', "").encode("cp1251", errors="replace"))
        r = node(JS_LOOP, d / "search_south_park.html", d / "topic_6731886.html", "since")
        self.assertEqual(r["status"]["state"], "done", r["status"])
        searches = [u for u in r["urls"] if "tracker.php" in u]
        self.assertTrue(searches and all("&o=1&s=2" in u for u in searches), searches)
        self.assertFalse([u for u in r["urls"] if "viewtopic.php" in u])
        s = r["posted"][0]["searches"][0]
        self.assertTrue(s["rows"] and not any(x.get("sel") for x in s["rows"]))

    def test_since_stops_paging_at_a_full_page_of_old_rows(self):
        # review 2026-10-28: sinceStop was not covered — a full first page (50 rows), all older than the sync point, and
        # 120 results: the next pages are not fetched
        import re
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        s = fx("search_south_park.html")
        row = re.search(r'<tr\b[^>]*id="trs-tr-6731886"[\s\S]*?</tr>', s).group(0)
        full = s.replace(row, "".join(row.replace("6731886", str(7000000 + i)) for i in range(50)), 1)
        full = full.replace("Результатов поиска: 9", "Результатов поиска: 120")
        (d / "search.html").write_bytes(full.replace('charset="Windows-1251"', "").encode("cp1251", errors="replace"))
        (d / "topic.html").write_bytes(fx("topic_6731886.html").replace('charset="Windows-1251"', "").encode("cp1251", errors="replace"))
        r = node(JS_LOOP, d / "search.html", d / "topic.html", "since")
        self.assertEqual(r["status"]["state"], "done", r["status"])
        res = r["posted"][0]["searches"][0]
        self.assertTrue(res.get("sinceStop"), res.get("pages"))
        self.assertEqual(res["pages"], 1)
        self.assertEqual(len([u for u in r["urls"] if "tracker.php" in u]), 1)

    def test_sync_points_and_since(self):
        # R207 / R212 (owner, 2026-10-26: «фильтр на сканер только на новые от даты последнего сканирования»): a clean
        # payload moves its query's point to the newest registration it saw; a failed one does not; `since` = point − 2 days
        import tempfile
        st = Path(tempfile.mkdtemp()) / "state.json"
        self.addCleanup(__import__("shutil").rmtree, st.parent, True)   # review 2026-10-28: the folders were left in %TEMP%
        q1, q2 = {"q": "Dune", "lo": 2024, "hi": 2024}, {"q": "Alien", "lo": 2024, "hi": 2024}
        pays = [{"run": "R", "query": q1, "errors": [], "searches": [{"rows": [{"t": "1", "added": 1700000000},
                                                                              {"t": "2", "added": 1760000000}]}]},
                {"run": "R", "query": q2, "errors": [{"q": "Alien", "err": "down: HTTP 502"}], "searches": []},
                {"run": "R", "final": True}]
        self.assertEqual(rt.record_sync(pays, st), 1)
        qs = rt.with_since([dict(q1, need=["any"]), dict(q2, need=["any"])], st)
        self.assertEqual(qs[0]["since"], 1760000000 - 2 * 86400)
        self.assertNotIn("since", qs[1])                     # never synced cleanly → full search
        self.assertNotIn("since", rt.with_since([q1], st, full=True)[0])
        # an older run never moves the point back
        rt.record_sync([{"query": q1, "errors": [], "searches": [{"rows": [{"t": "3", "added": 1600000000}]}]}], st)
        self.assertEqual(rt.with_since([q1], st)[0]["since"], 1760000000 - 2 * 86400)

    def test_resume_tail(self):
        # owner, 2026-10-26: «если сбился … мы смогли и хвосты доказать»: never posted, failed and empty queries come back
        cfg = {"run": "R", "queries": [{"q": "A", "lo": 2024, "hi": 2024}, {"q": "B", "lo": None, "hi": None},
                                       {"q": "C", "lo": 2025, "hi": 2025}, {"q": "D", "lo": 2026, "hi": 2026}]}
        pays = [{"run": "R", "query": {"q": "A", "lo": 2024, "hi": 2024}, "searches": [{"total": 3}], "errors": []},
                {"run": "R", "query": {"q": "B", "lo": None, "hi": None}, "searches": [{"total": 0}], "errors": []},
                {"run": "R", "query": {"q": "C", "lo": 2025, "hi": 2025}, "searches": [],
                 "errors": [{"q": "C", "err": "not_logged_in"}]},
                {"run": "OTHER", "query": {"q": "D", "lo": 2026, "hi": 2026}, "searches": [{"total": 9}], "errors": []}]
        left, info = rt.resume_queries(cfg, pays)
        self.assertEqual([q["q"] for q in left], ["B", "C", "D"])
        self.assertEqual((info["done_ok"], info["left"]), (1, 3))
        self.assertEqual([q["q"] for q in rt.resume_queries(cfg, pays, retry_empty=False)[0]], ["C", "D"])
        # a resumed run «R-r1» that finished B and D: only C is left (cfg of the resume carries base = R)
        more = [{"run": "R-r1", "query": {"q": "B", "lo": None, "hi": None}, "searches": [{"total": 2}], "errors": []},
                {"run": "R-r1", "query": {"q": "D", "lo": 2026, "hi": 2026}, "searches": [{"total": 1}], "errors": []}]
        self.assertEqual([q["q"] for q in rt.resume_queries(cfg, pays + more)[0]], ["C"])


def payload_from_fixtures(sel_ids=("6731886", "6466056", "9000001"), seeds_delta: int = 0) -> dict:
    """What rutracker_collect.js posts for one query (built with the Python twin of its topic parser)."""
    rows = []
    for t, size, seeds, title in (SAMPLE or {"rows": []})["rows"]:
        forum = "Зарубежные сериалы (HD Video)" if t in ("6731886", "6755275") else "Мультсериалы (HD Video)"
        rows.append({"t": t, "fid": "1", "forum": forum, "title": title, "size": size, "seeds": seeds + seeds_delta,
                     "leech": 1, "added": 1753381320, "sel": 1 if t in sel_ids else 0})
    rows.append({"t": "5000001", "forum": "Горячие Новинки (Игры для Windows)", "title": "South Park: Snow Day!",
                 "size": 1, "seeds": 0, "sel": 1})
    topics = []
    for name, tid in (("topic_6731886.html", "6731886"), ("topic_6466056.html", "6466056"), ("topic_dv5.html", "9000001")):
        t = split_topic(fx(name))
        t.update(topic_id=tid, fetched_at="2026-10-24T12:00:00Z")
        topics.append(t)
    return {"v": 1, "kind": "rutracker", "run": "test", "seq": 0,
            "query": {"q": "South Park", "lo": 2023, "hi": None, "need": ["any"]},
            "searches": [{"q": "South Park", "total": len(rows), "capped": False, "pages": 1, "rows": rows}],
            "topics": topics, "errors": []}


@unittest.skipUnless(SAMPLE, "data/cache/rutracker/south_park_rows.json missing")
class TestIngest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def rel(self, sid):
        return self.conn.execute("SELECT * FROM releases WHERE source='rutracker' AND source_id=?", (sid,)).fetchone()

    def test_ingest_idempotent_dv5_and_refresh(self):
        st = rt.ingest(self.conn, [payload_from_fixtures(), {"v": 1, "kind": "rutracker", "final": True, "fatal": None}])
        self.assertEqual((st["new"], st["pages_stored"], st["dv5_excluded"], st["skipped_forum"]), (2, 2, 1, 1))
        r = self.rel("6731886")
        self.assertEqual((r["url"], r["category"], r["size_bytes"], r["seeders"], r["resolution"], r["source_type"],
                          r["best_voice"], r["season"], r["episodes"]),
                         ("https://rutracker.org/forum/viewtopic.php?t=6731886", "foreign_series", 11091673193, 63,
                          2160, "WEB-DL", "mvo", "27", "1-5"))
        self.assertEqual(json.loads(r["parsed"])["imdb_id"], "tt0121955")
        self.assertTrue(r["parsed_version"].startswith("rutracker-page:"))
        self.assertIsNotNone(rt.raw_page(self.conn, r["id"]))
        self.assertEqual(self.rel("6466056")["category"], "animation")
        self.assertIsNone(self.rel("9000001"))         # DV5-only: never stored …
        self.assertEqual(self.conn.execute("SELECT reason FROM excluded_releases WHERE source='rutracker' AND "
                                           "source_id='9000001'").fetchone()[0], "dv5_only")
        self.assertIsNone(self.rel("5000001"))         # game forum
        self.assertIsNone(self.rel("6755275"))         # not selected, not known: no new row

        st2 = rt.ingest(self.conn, [payload_from_fixtures(seeds_delta=5)])
        self.assertEqual((st2["new"], st2["excluded_skipped"]), (0, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM releases").fetchone()[0], 2)
        self.assertEqual(self.rel("6466056")["seeders"], 102)   # the topic page's own count wins over the row

    def test_row_without_page_gets_name_parse(self):
        p = payload_from_fixtures(sel_ids=("6755275",))
        p["topics"] = []
        st = rt.ingest(self.conn, [p])
        self.assertEqual((st["new"], st["name_only"]), (1, 1))
        r = self.rel("6755275")
        self.assertEqual((r["resolution"], r["season"], r["best_voice"], r["size_bytes"], r["seeders"]),
                         (1080, "27", "mvo", 5151467535, 179))
        self.assertTrue(r["parsed_version"].startswith("rutracker-title:"))
        self.assertEqual(r["added_at"], "2025-07-24T18:22:00")
        # a later run brings the page: stored and re-parsed from it
        p2 = payload_from_fixtures(sel_ids=("6755275",), seeds_delta=1)
        p2["topics"] = []
        st2 = rt.ingest(self.conn, [p2])
        self.assertEqual((st2["new"], st2["seeds_refreshed"]), (0, 1))
        self.assertEqual(self.rel("6755275")["seeders"], 180)

    def test_row_without_page_dv5_by_title_excluded(self):
        # review 2026-10-28: «… WEB-DL 2160p | Dolby Vision TV» rows stored from the title alone reached the table
        p = payload_from_fixtures(sel_ids=("7000001",))
        p["topics"] = []
        p["searches"][0]["rows"].append({"t": "7000001", "forum": "Зарубежное кино (UHD Video)", "sel": 1, "size": 1,
                                         "seeds": 3, "title": "Тайная жизнь животных / Secret Lives (2024) "
                                                              "WEB-DL 2160p | Dolby Vision TV | D"})
        st = rt.ingest(self.conn, [p])
        self.assertEqual((st["new"], st["dv5_excluded"]), (0, 1))
        self.assertIsNone(self.rel("7000001"))
        self.assertEqual(self.conn.execute("SELECT reason FROM excluded_releases WHERE source='rutracker' AND "
                                           "source_id='7000001'").fetchone()[0], "dv5_only")

    def test_load_payloads_chunk_readout(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        (d / "bridge.txt").write_text(json.dumps(payload_from_fixtures()), encoding="utf-8")
        (d / "chunks.json").write_text(json.dumps([json.dumps(payload_from_fixtures()), "not json"]), encoding="utf-8")
        ps = rt.load_payloads([str(d / "bridge.txt"), str(d / "chunks.json")])
        self.assertEqual(len(ps), 2)
        self.assertTrue(all(p["topics"] for p in ps))

    def test_reparse(self):
        rt.ingest(self.conn, [payload_from_fixtures()])
        self.conn.execute("UPDATE releases SET parsed = NULL WHERE source='rutracker'")
        st = rt.reparse(self.conn)
        self.assertEqual((st["parsed"], st["failed"]), (2, 0))
        self.assertEqual(self.rel("6731886")["resolution"], 2160)


class TestQueries(unittest.TestCase):
    def test_merge_and_short_names(self):
        q = rt._Queries()
        q.add_title({"id": 1, "title_original": "South Park", "title_ru": "Южный парк", "year": 2025}, ["any"], "gap", True)
        q.add_title({"id": 2, "title_original": "South Park", "title_ru": "Южный Парк", "year": 2023}, ["4k"], "r9")
        q.add_title({"id": 3, "title_original": "F1", "title_ru": "Формула 1", "year": 2025}, ["dub"], "r9")
        q.add_title({"id": 4, "title_original": "名探偵ホームズ", "title_ru": "Великий детектив Холмс", "year": 1984}, ["any"], "gap")
        items = {i["q"]: i for i in q.items.values()}
        self.assertEqual(items["South Park"]["lo"], 2023)
        self.assertIsNone(items["South Park"]["hi"])               # open end (series) wins
        self.assertEqual(items["South Park"]["need"], ["any"])
        self.assertEqual(items["South Park"]["titles"], [1, 2])
        self.assertIn("Южный парк", items)                        # Russian name: a separate query (R151)
        self.assertNotIn("Южный Парк", items)
        self.assertIn("F1 2025", items)                           # short name + year
        self.assertNotIn("名探偵ホームズ", items)                   # not searchable in cp1251
        self.assertIn("Великий детектив Холмс", items)

    def test_write_run(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, d, True)   # review 2026-10-28: the folders were left in %TEMP%
        res = rt.write_run([{"q": "South Park", "lo": None, "hi": None, "need": ["any"], "why": ["extra"], "titles": []}],
                           [6731886], out=d, part_size=1)
        self.assertEqual(res["inline_parts"], ["run_inline_1.js"])
        run = json.loads((d / "queries_run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["known"], [6731886])
        self.assertEqual(run["queries"], [{"q": "South Park", "lo": None, "hi": None, "need": ["any"]}])
        self.assertIn('__rtCollect({queriesUrl: "http://127.0.0.1:8765/queries/rutracker"})', (d / "run.js").read_text(encoding="utf-8"))
        if NODE:   # the generated files are valid JavaScript
            for f in ("run.js", "run_inline_1.js"):
                subprocess.run([NODE, "--check", str(d / f)], check=True)


if __name__ == "__main__":
    unittest.main()
