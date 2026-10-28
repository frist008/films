"""Tests for the toloka.to collector: films/browser/toloka_collect.js (parsers and loop run through node, skipped without
it), the toloka side of films/parse/ua_trackers.py and films/sources/toloka.py (ingest into a fresh, migrated in-memory DB).

tests/fixtures/toloka/ are WRITTEN BY HAND from toloka's public pages and the usual phpBB / TorrentPier (Jackett toloka)
markup — the logged-in pages were never seen (no account in this project): check them against the first live run.
Run:  python -X utf8 -m unittest tests.test_toloka
"""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from films import db
from films.parse import ua_trackers as u
from films.sources import toloka as tl

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "toloka"
JS = ROOT / "films" / "browser" / "toloka_collect.js"
NODE = shutil.which("node")
TODAY = date(2026, 10, 26)


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def node(code: str, *args) -> object:
    out = subprocess.run([NODE, "-e", code, str(JS), *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                         check=True)
    return json.loads(out.stdout)


JS_PARSE = r"""
const a = require(process.argv[1]); const fs = require('fs');
const s = a.parseSearch(fs.readFileSync(process.argv[2], 'utf8'));
const t = a.parseTopic(fs.readFileSync(process.argv[3], 'utf8'));
const q = {q: 'Dune', lo: 2024, hi: 2024, need: ['4k']};
console.log(JSON.stringify({
  rows: s.rows, maxStart: s.maxStart,
  sel4k: s.rows.map(r => a.select(r, q)), selDub: s.rows.map(r => a.select(r, {q: 'Dune', lo: 2021, hi: null, need: ['ukdub']})),
  topic: Object.assign({}, t, {post_len: t.post_html.length, secret: /SECRETKEY/.test(t.post_html)}),
  problem: [a.pageProblem('<input name="username"><input name="password">'),
            a.pageProblem('<a href="login.php?logout=true">Вихід</a><input name="password"><input name="username">')],
  scrub: a.scrub('<a href="magnet:?xt=urn:btih:ABCDEF&tr=https%3A%2F%2Fbt.toloka.to%2Fannounce%3Fh%3DKEY">m</a> download.php?id=5&h=KEY2'),
  index: a.parseIndex('<a href="f96">Фільми HD українською</a><a href="/f170">Ігри українською</a><a href="f96">dup</a>'),
  size: [a.sizeBytes('61.2&nbsp;GB'), a.sizeBytes('700 МБ')],
}));
"""

JS_LOOP = r"""
const fs = require('fs');
const posted = [];
global.window = global; global.document = {};
global.localStorage = {d: {}, getItem(k) { return this.d[k] || null; }, setItem(k, v) { this.d[k] = v; }};
const search = fs.readFileSync(process.argv[2], 'utf8'), topic = fs.readFileSync(process.argv[3], 'utf8');
global.fetch = async (url, opt) => {
  if (opt && opt.method === 'POST') { posted.push(JSON.parse(opt.body)); return {ok: true}; }
  const body = /\/t681234$/.test(url) ? topic : /tracker\.php/.test(url) ? (/start=50/.test(url) ? '<table></table>' : search)
             : '<a href="login.php?logout=true">Вихід</a><a href="f96">Фільми HD українською</a>';
  return {ok: true, status: 200, url, text: async () => body};
};
require(process.argv[1]);
global.setTimeout = (f) => f();
window.__tlCollect({queries: [{q: 'Dune', lo: 2024, hi: 2024, need: ['4k']}], known: ['681300'], run: 'test', delayMs: 0, maxTopics: 5});
const wait = () => new Promise(r => setImmediate(r));
(async () => {
  for (let i = 0; i < 400 && !/^(done|stopped|crashed)/.test(window.__tl.state); i++) await wait();
  console.log(JSON.stringify({state: window.__tl.state, posted}));
})();
"""


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = node(JS_PARSE, FIX / "search_dune.html", FIX / "topic_681234.html")

    def test_search_rows(self):
        rows = self.r["rows"]
        self.assertEqual(len(rows), 5)
        self.assertEqual(self.r["maxStart"], 50)
        first = rows[0]
        self.assertEqual((first["t"], first["fid"], first["forum"], first["size"], first["seeds"], first["leech"],
                          first["added"]),
                         ("681234", "96", "Фільми HD українською", round(61.2 * 1024 ** 3), 34, 2, "2024-05-20 18:02"))
        self.assertTrue(first["title"].startswith("Дюна: Частина друга / Dune: Part Two (2024)"))
        self.assertEqual(rows[4]["seeds"], 0)            # «-1» seeders on the page -> 0

    def test_selection(self):
        # 4K need: only the 2160p film of 2024 (±1); documentary and game forums never
        self.assertEqual(self.r["sel4k"], [True, False, False, False, False])
        # a dub (Ukrainian or any) is not visible in a title: every video row of the year window
        self.assertEqual(self.r["selDub"], [True, True, False, False, True])

    def test_topic(self):
        t = self.r["topic"]
        self.assertEqual((t["title"], t["forum"], t["forum_id"], t["size_bytes"], t["seeders"], t["leechers"], t["btih"],
                          t["reged"]),
                         ("Дюна: Частина друга / Dune: Part Two (2024) BDRemux 2160p HDR10 DV Ukr/Eng | Sub Ukr/Eng",
                          "Фільми HD українською", "96", round(61.2 * 1024 ** 3), 34, 2,
                          "0123456789abcdef0123456789abcdef01234567", "2024-05-20 18:02"))
        self.assertGreater(t["post_len"], 1000)
        self.assertFalse(t["secret"])

    def test_helpers(self):
        self.assertEqual(self.r["problem"], ["not_logged_in", None])
        self.assertNotIn("KEY", self.r["scrub"])
        self.assertIn("magnet:?xt=urn:btih:ABCDEF\"", self.r["scrub"])
        self.assertEqual(self.r["index"], [{"f": "96", "name": "Фільми HD українською", "video": True},
                                           {"f": "170", "name": "Ігри українською", "video": False}])
        self.assertEqual(self.r["size"], [round(61.2 * 1024 ** 3), 700 * 1024 ** 2])


JS_WAIT = r"""
const fs = require('fs');
const posted = [];
global.window = global; global.document = {};
global.localStorage = {d: {}, getItem(k) { return this.d[k] || null; }, setItem(k, v) { this.d[k] = v; }};
const search = fs.readFileSync(process.argv[2], 'utf8');
let n = 0;
global.fetch = async (url, opt) => {
  if (opt && opt.method === 'POST') { posted.push(JSON.parse(opt.body)); return {ok: true}; }
  n++;
  if (n === 2) return {ok: true, status: 200, url, text: async () => '<h1>Технічні роботи</h1>'};            // maintenance
  if (n === 3) return {ok: true, status: 200, url: 'https://toloka.to/login.php', text: async () => '<input name="username"><input name="password">'};
  const body = /tracker\.php/.test(url) ? (/start=50/.test(url) ? '<table></table>' : search) : '<a href="login.php?logout=true">x</a>';
  return {ok: true, status: 200, url, text: async () => body};
};
require(process.argv[1]);
global.setTimeout = (f) => f();
window.__tlCollect({queries: [{q: 'Dune', lo: 2024, hi: 2024, need: ['hdr'], since: '2030-01-01'}], known: [], run: 'w',
                    delayMs: 0, waitPollMs: 0});
const wait = () => new Promise(r => setImmediate(r));
(async () => {
  for (let i = 0; i < 400 && !/^(done|stopped|crashed)/.test(window.__tl.state); i++) await wait();
  console.log(JSON.stringify({state: window.__tl.state, kinds: posted.map(p => p.kind + (p.state ? ':' + p.state : '')),
                              q: posted.filter(p => p.query).map(p => ({rows: p.searches[0].rows.length, topics: p.topics.length,
                                                                       sinceStop: !!p.searches[0].sinceStop}))}));
})();
"""


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorWaits(unittest.TestCase):
    def test_maintenance_and_logout_pause_then_go_on(self):
        r = node(JS_WAIT, FIX / "search_dune.html")
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["kinds"], ["toloka", "toloka_status:waiting: maintenance", "toloka_status:waiting: not_logged_in",
                                      "toloka", "toloka"])
        # since 2030: every row is older -> nothing opened, paging stops at once
        self.assertEqual(r["q"], [{"rows": 5, "topics": 0, "sinceStop": True}])


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorLoop(unittest.TestCase):
    def test_loop_posts_forums_query_and_final_then_ingests(self):
        r = node(JS_LOOP, FIX / "search_dune.html", FIX / "topic_681234.html")
        self.assertEqual(r["state"], "done")
        kinds = [("forums" in p, "query" in p, bool(p.get("final"))) for p in r["posted"]]
        self.assertEqual(kinds, [(True, False, False), (False, True, False), (False, False, True)])
        q = r["posted"][1]
        self.assertEqual([tp["topic_id"] for tp in q["topics"]], ["681234"])   # 681300 known, others not selected
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        st = tl.ingest(conn, r["posted"], TODAY)
        self.assertEqual((st["pages_stored"], st["forums"], st["finals"], st["fatal"]), (1, 1, 1, []))
        rel = conn.execute("SELECT * FROM releases WHERE source='toloka'").fetchone()
        self.assertEqual((rel["source_id"], rel["url"], rel["best_voice"], rel["resolution"], rel["category"]),
                         ("681234", "https://toloka.to/t681234", "dub", 2160, "foreign_films"))


class TestLivePosts(unittest.TestCase):
    """Real logged-in toloka posts (tests/fixtures/toloka/live_*.html, trimmed): per-track blocks «Аудіо # 1:» /
    «мова: …» / «переклад: …», a «Відео:» block, Ukrainian MediaInfo, studio logos. Before the fix (2026-10-26 06:45) the
    voice kind of every such track was lost and its studio was the word «мова»."""

    def parse(self, name):
        return u.parse_topic("toloka", fx(name), TODAY)

    def tracks(self, d):
        return [(a["lang"], a["kind"], a.get("studio")) for a in d["audio"]]

    def test_dub_and_multivoice_blocks(self):
        d = self.parse("live_flash_dub_mvo.html")
        self.assertEqual(self.tracks(d)[:3], [("uk", "dub", "Постмодерн"), ("uk", "mvo", "DniproFilm"), ("en", "original", None)])
        self.assertEqual((d["best_voice"], d["voice_by_lang"]), ("dub", {"uk": ["dub", "mvo"]}))
        self.assertEqual((d["resolution"], d["video_codec"], d["video_kbps"]), (2160, "HEVC", 50900))
        self.assertNotIn("мова", " ".join(str(a.get("studio")) for a in d["audio"]))

    def test_studio_one_plus_one(self):
        # the «1+1» studio (logo 1+1.gif) is a name, not an age mark like «16+» (R244, 2026-10-26)
        from films.parse import rutor as rp
        self.assertEqual(rp._studio_from_text("професійний дубльований | 1+1", []), "1+1")
        self.assertEqual(rp._studio_from_text("Українською 1 + 1", []), "1+1")
        self.assertNotEqual(rp._studio_from_text("многоголосый 18+ | LostFilm", []), "1+1")

    def test_ukrainian_mediainfo(self):
        d = self.parse("live_mandalorian_mi_ua.html")
        self.assertTrue(d["mediainfo_found"])
        self.assertEqual((d["video_codec"], d["video_kbps"], d["video_width"], d["video_height"], d["video_bit_depth"]),
                         ("HEVC", 4756, 1920, 802, 10))
        self.assertEqual(d["hdr"], ["HDR10"])
        self.assertEqual(self.tracks(d), [("uk", "mvo", "Цікава Ідея"), ("en", "original", None)])
        self.assertEqual(d["forced_subs"], ["uk"])

    def test_video_block_is_not_mediainfo_and_dv_from_mediainfo(self):
        d = self.parse("live_monsters_video_block_dv.html")
        self.assertEqual((d["hdr"], d["dv_profile"], d["dv5_only"], d["video_kbps"]), (["DV", "HDR10"], 8, False, 41000))
        self.assertEqual(self.tracks(d), [("uk", "dub", "LeDoyen"), ("en", "original", None), ("en", "original", None),
                                          ("en", "commentary", None)])

    def test_studio_logos_in_track_headers(self):
        d = self.parse("live_shadow_dub_logos.html")
        self.assertEqual([t[:2] for t in self.tracks(d)], [("uk", "mvo")] * 4 + [("en", "original")])
        self.assertEqual(self.tracks(d)[3][2], "ICTV")      # <img src="//toloka.to/img/dubbing/ICTV.gif">
        self.assertEqual(d["voice_by_lang"], {"uk": ["mvo"]})

    def test_block_merge_unit(self):
        self.assertEqual(u.ua_to_ru("Аудіо # 1:\nмова: українська\nпереклад: професійний дубльований | Постмодерн\n"
                                    "кодек: E-AC3 5.1\nАудіо # 2:\nмова: англійська\nпереклад: оригінал"),
                         "Аудио # 1: украинский, профессиональный дублированный | Постмодерн, E-AC3 5.1\n"
                         "Аудио # 2: английский, оригинал")
        self.assertEqual(u.ua_to_ru(" Відео \n Мова        : Ukrainian \n За замовчуванням : Так \n Бітрейт : 41.0 Мбіт/сек "),
                         " Video\n Language : Ukrainian \n Default : Yes \n Bit rate : 41.0 Mb/s ")


class TestTopicPython(unittest.TestCase):
    def test_split_and_parse(self):
        page = fx("topic_681234.html")
        sp = u.split_toloka_topic(page)
        self.assertEqual((sp["topic_id"], sp["forum"], sp["forum_id"], sp["seeders"], sp["leechers"], sp["reged"]),
                         ("681234", "Фільми HD українською", "96", 34, 2, "2024-05-20 18:02"))
        d = u.parse_topic("toloka", page, TODAY)
        self.assertEqual((d["category"], d["imdb_id"], d["resolution"], d["source_type"], d["hdr"], d["dv_profile"],
                          d["dv_fallback"], d["dv5_only"]),
                         ("foreign_films", "tt15239678", 2160, "BDRemux", ["DV", "HDR10"], 7, "HDR10", False))
        self.assertEqual((d["best_voice"], d["voice_by_lang"], d["forced_subs"], d["runtime_min"], d["added_at"]),
                         ("dub", {"uk": ["dub"]}, ["uk"], 166, "2024-05-20T18:02:00"))
        self.assertEqual((d["audio"][0]["lang"], d["audio"][0]["studio"]), ("uk", "Le Doyen"))
        c = u.compact_topic_html("toloka", dict(sp, fetched_at="x"))
        self.assertNotIn("SECRETKEY", c)
        self.assertEqual(u.parse_topic("toloka", c, TODAY)["audio"], d["audio"])

    def test_forum_names(self):
        self.assertEqual(u.category_for_forum("Фільми HD українською"), "foreign_films")
        self.assertEqual(u.category_for_forum("Серіали українською"), "foreign_series")
        self.assertEqual(u.category_for_forum("Мультсеріали"), "animation")
        for f in ("Ігри українською", "Документальні фільми", "Аніме українською", "Фільми з українськими субтитрами",
                  "Музичні кліпи"):
            self.assertIsNone(u.category_for_forum(f), f)


def payload(rows, topics=(), errors=()):
    return {"v": 1, "kind": "toloka", "run": "t", "seq": 0, "query": {"q": "Dune", "lo": 2024, "hi": 2024, "need": ["4k"]},
            "searches": [{"q": "Dune", "pages": 1, "rows": rows}], "topics": list(topics), "errors": list(errors)}


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def rel(self, sid):
        return self.conn.execute("SELECT * FROM releases WHERE source='toloka' AND source_id=?", (sid,)).fetchone()

    def test_rows_topics_dv5_refresh_and_fatal(self):
        topic = dict(u.split_toloka_topic(fx("topic_681234.html")), fetched_at="2026-10-26T02:00:00")
        rows = [{"t": "681234", "fid": "96", "forum": "Фільми HD українською", "title": topic["title"], "size": 1, "seeds": 9,
                 "sel": 1},
                {"t": "681300", "fid": "96", "forum": "Фільми HD українською",
                 "title": "Дюна: Частина друга / Dune: Part Two (2024) WEB-DL 1080p Ukr/Eng", "size": 10, "seeds": 51,
                 "added": "2024-04-16 09:11", "sel": 1},
                {"t": "681400", "fid": "96", "forum": "Фільми HD українською",
                 "title": "Дюна / Dune (2024) WEB-DL 2160p Dolby Vision P5 Ukr/Eng", "sel": 1},
                {"t": "650002", "fid": "170", "forum": "Ігри українською", "title": "[Win] Dune: Awakening (2025) Ukr", "sel": 1},
                {"t": "999", "fid": "96", "forum": "Фільми HD українською", "title": "Інше / Other (2010) BDRip", "seeds": 3,
                 "sel": 0}]
        st = tl.ingest(self.conn, [payload(rows, [topic])], TODAY)
        self.assertEqual((st["new"], st["pages_stored"], st["name_only"], st["dv5_excluded"], st["skipped_category"]),
                         (2, 1, 1, 1, 1))
        r = self.rel("681234")
        self.assertEqual((r["best_voice"], r["seeders"], r["size_bytes"], r["btih"]),
                         ("dub", 34, round(61.2 * 1024 ** 3), "0123456789abcdef0123456789abcdef01234567"))
        r2 = self.rel("681300")
        self.assertEqual((r2["resolution"], r2["seeders"], r2["added_at"], r2["best_voice"]),
                         (1080, 51, "2024-04-16T09:11:00", None))
        self.assertIsNone(self.rel("681400"))
        self.assertIsNone(self.rel("999"))       # unselected rows never create releases
        # the same payload again: idempotent; an unselected row refreshes seeds of a stored release
        rows[1]["sel"], rows[1]["seeds"] = 0, 60
        st = tl.ingest(self.conn, [payload(rows, [topic]),
                                   payload([], errors=[{"q": "x", "err": "not_logged_in"}])], TODAY)
        self.assertEqual((st["new"], st["seeds_refreshed"], st["fatal"]), (0, 1, ["not_logged_in"]))
        self.assertEqual(self.rel("681300")["seeders"], 60)

    def test_load_payloads_chunk_readout_and_kind(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "chunk.txt"
            p.write_text(json.dumps([json.dumps(payload([])), {"kind": "rutracker"}]), encoding="utf-8")
            self.assertEqual(len(tl.load_payloads([str(p)])), 1)


class TestQueries(unittest.TestCase):
    def test_names_merge_priority(self):
        self.assertEqual(tl._names({"title_original": "Dune: Part Two", "title_latin": "Dune: Part Two",
                                    "title_uk": "Дюна: Частина друга", "title_ru": "Дюна: Часть вторая"}),
                         ["Dune: Part Two", "Дюна: Частина друга"])
        # never the Russian name, even without a Ukrainian one (owner, 2026-10-26: «ру названия бестолку искать»)
        self.assertEqual(tl._names({"title_original": "Up", "title_ru": "Вверх"}), ["Up"])
        qs = tl._Queries()
        qs.add_title({"id": 1, "title_original": "Up", "title_uk": "Вгору", "year": 2009}, ["ukdub"], "no_uk_dub", 4)
        qs.add_title({"id": 2, "title_original": "Dune", "year": 2021}, ["dub"], "no_dub", 2)
        qs.add_title({"id": 3, "title_original": "Dune", "year": 2024}, ["4k"], "picture:4k", 3)
        items = sorted(qs.items.values(), key=lambda q: q["prio"])
        self.assertEqual([q["q"] for q in items], ["Dune", "Up 2009", "Вгору"])
        self.assertEqual((items[0]["lo"], items[0]["hi"], items[0]["need"], items[0]["titles"]), (2021, 2024, ["4k", "dub"], [2, 3]))

    def test_coverage(self):
        c = sqlite3.connect(":memory:")
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (1, 'movie', 'X', 2025)")
        audio_ru = json.dumps([{"lang": "ru", "kind": "dub"}])
        audio_uk = json.dumps([{"lang": "uk", "kind": "dub"}])
        for rid, src, res, hdr, audio in ((1, "rutor", 1080, "[]", audio_ru), (2, "mazepa", 2160, '["HDR10"]', audio_uk),
                                          (3, "rutor", 2160, '["DV"]', "[]")):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, resolution, hdr, audio) VALUES (?, ?, ?, 'u', 'n', ?, ?, ?)",
                      (rid, src, str(rid), res, hdr, audio))
            c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (?, 1)", (rid,))
        c.execute("INSERT INTO excluded_releases (source, source_id, reason) VALUES ('mazepa', '2', 'dv5_only')")
        self.assertEqual(tl.title_coverage(c)[1], {"dub": True, "uk_dub": False, "4k": True, "hdr": True})

    def test_sync_point_per_query(self):
        state = {}
        ok = payload([{"t": "681234", "added": "2024-05-20 18:02"}, {"t": "681300", "added": "2024-04-16 09:11"}])
        ok["at"] = "2026-10-26T02:00:00"
        bad = payload([{"t": "1", "added": "2026-10-01 00:00"}], errors=[{"q": "Up", "err": "HTTP 500"}])
        bad["query"]["q"] = "Up"
        self.assertEqual(tl.remember_syncs(state, [ok, bad, {"kind": "toloka", "final": True}]), 1)
        self.assertEqual(state["queries"]["dune"]["newest_added"], "2024-05-20")
        self.assertEqual(state["queries"]["dune"]["newest_topic"], 681300)
        self.assertEqual(tl.since_for(state, "Dune"), "2024-05-18")       # minus the 2-day overlap
        self.assertIsNone(tl.since_for(state, "Up"))                       # failed query: no sync point
        # a later empty but successful sync keeps the newest date
        empty = payload([])
        tl.remember_syncs(state, [empty])
        self.assertEqual(tl.since_for(state, "Dune"), "2024-05-18")
        with tempfile.TemporaryDirectory() as d:
            q = [{"q": "Dune", "lo": 2024, "hi": 2024, "need": ["4k"]}, {"q": "Up", "lo": None, "hi": None, "need": ["any"]}]
            tl.write_run(q, [], out=Path(d), state=state)
            cfg = json.loads((Path(d) / "queries_run.json").read_text(encoding="utf-8"))
            self.assertEqual([c.get("since") for c in cfg["queries"]], ["2024-05-18", None])
            tl.write_run(q, [], out=Path(d), state={})                     # --full
            cfg = json.loads((Path(d) / "queries_run.json").read_text(encoding="utf-8"))
            self.assertEqual([c.get("since") for c in cfg["queries"]], [None, None])

    @unittest.skipUnless(NODE, "node not installed")
    def test_js_since(self):
        r = node(r"""
const a = require(process.argv[1]);
const rows = [{added: '2024-05-20 18:02'}, {added: '2024-04-16 09:11'}];
console.log(JSON.stringify([a.isOld(rows[0], '2024-05-18'), a.isOld(rows[1], '2024-05-18'), a.isOld({added: null}, '2024-05-18'),
  a.isOld(rows[1], null), a.olderThan(rows, '2024-05-18'), a.olderThan([rows[1]], '2024-05-18'), a.olderThan([], '2024-05-18')]));
""")
        self.assertEqual(r, [False, True, False, False, False, True, False])

    def test_write_run(self):
        with tempfile.TemporaryDirectory() as d:
            res = tl.write_run([{"q": "Dune", "lo": 2024, "hi": 2024, "need": ["4k"], "why": ["x"], "prio": 3, "titles": [1]}],
                               [681234], out=Path(d), part_size=1)
            run = (Path(d) / "run.js").read_text(encoding="utf-8")
            self.assertTrue(run.rstrip().endswith('__tlCollect({queriesUrl: "http://127.0.0.1:8765/queries/toloka"});'))
            cfg = json.loads((Path(d) / "queries_run.json").read_text(encoding="utf-8"))
            self.assertEqual((cfg["queries"], cfg["known"], res["inline_parts"]),
                             ([{"q": "Dune", "lo": 2024, "hi": 2024, "need": ["4k"]}], [681234], ["run_inline_1.js"]))


if __name__ == "__main__":
    unittest.main()
