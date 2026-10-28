"""Tests for kinozal.guru (source priority 4): films/parse/kinozal.py, films/browser/kinozal_collect.js (parsers and the
loop run through node, skipped without it) and films/sources/kinozal.py (ingest / resume into a fresh in-memory DB).

The fixtures in tests/fixtures/kinozal/ are HAND-WRITTEN from Jackett's kinozal definition and what is known of the
details page — kinozal could not be opened from here (Cloudflare). Check them against the first live run (its `probe`).
Run:  python -X utf8 -m unittest tests.test_kinozal
"""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from films import db
from films.parse import kinozal as K
from films.sources import kinozal as KZ
from films.sources import nnmclub as N

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "kinozal"
JS = ROOT / "films" / "browser" / "kinozal_collect.js"
NODE = shutil.which("node")


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def node(code: str, *args) -> object:
    out = subprocess.run([NODE, "-e", code, str(JS), *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                         check=True)
    return json.loads(out.stdout)


class TestTitle(unittest.TestCase):
    def test_series_codes_studios_hdr(self):
        n, w = K.rutor_style_name("Медведь (5 сезон: 1-8 серии из 8) / The Bear / 2026 / ПМ (HDrezka Studio), ДБ (Red Head "
                                  "Sound), СТ / WEB-DL (2160p, HDR10, Dolby Vision)")
        self.assertEqual(n, "Медведь / The Bear [5x1-8 из 8] (2026) WEB-DL 2160p | HDR10, Dolby Vision | MVO, DUB | "
                            "HDrezka Studio, Red Head Sound")
        self.assertEqual(w, [])

    def test_film_edition_ukr_subs_only(self):
        n, _ = K.rutor_style_name("Оппенгеймер / Oppenheimer / 2023 / ДБ, АП (Гаврилов), СТ / UHD BDRemux (2160p, HDR10) / IMAX")
        self.assertEqual(n, "Оппенгеймер / Oppenheimer (2023) UHD BDRemux 2160p | HDR10, IMAX | DUB, AVO | Гаврилов")
        n, _ = K.rutor_style_name("Фоллаут (1 сезон: 1-8 серии из 8) / Fallout / 2024 / УКР, ПМ, СТ / WEB-DL (1080p)")
        self.assertTrue(n.endswith("| MVO | UKR"))
        n, _ = K.rutor_style_name("Фильм / Film / 2025 / СТ / WEBRip (1080p)")
        self.assertIn("| Sub", n)

    def test_tech_part_before_quality(self):
        # live titles (2026-10-26): «… / ДБ, ПМ, СТ / 4K, HEVC, HDR, HDR10+, Dolby Vision P8 / WEB-DL (2160p)»
        n, _ = K.rutor_style_name("Хитрый койот / Coyote vs. Acme / 2026 / ДБ, ПМ, СТ / 4K, HEVC, HDR, HDR10+, Dolby Vision P8 / "
                                  "WEB-DL (2160p)")
        self.assertEqual(n, "Хитрый койот / Coyote vs. Acme (2026) WEB-DL 2160p | 4K, HEVC, HDR, HDR10+, Dolby Vision P8 | DUB, MVO")
        d = K.parse_title_only("X / X / 2026 / ДБ / 4K, HEVC, Dolby Vision P5 / WEB-DL (2160p)", "foreign_films")
        self.assertTrue(d["dv5_only"])
        self.assertTrue(K.russian_original("Колокол надежды / 2025 / РУ / WEB-DL (1080p)"))
        self.assertFalse(K.russian_original("Пила 10 / Saw X / 2023 / ДБ, СТ / HDRip"))

    def test_years_dates_sizes_categories(self):
        self.assertEqual(K.title_years("Рик и Морти (1-7 сезоны) / Rick and Morty / 2013-2023 / ЛМ / WEB-DLRip (720p)"), (2013, 2023))
        today = date(2026, 10, 26)
        self.assertEqual(K.parse_date("сегодня в 09:10", today), "2026-10-26T09:10:00")
        self.assertEqual(K.parse_date("вчера в 13:04", today), "2026-10-25T13:04:00")
        self.assertEqual(K.parse_date("24.10.2017 в 23:44"), "2017-10-24T23:44:00")
        self.assertEqual(K.parse_date("26 октября 2026 в 03:12"), "2026-10-26T03:12:00")
        self.assertEqual(K.parse_size("41.2 ГБ (44 237 899 776)"), 44237899776)
        self.assertEqual((K.category_for(46), K.category_for(45), K.category_for(21), K.category_for(17)),
                         ("foreign_series", None, "animation", "foreign_films"))
        self.assertEqual(K.category_from_name("Кино - Новый раздел"), "foreign_films")
        self.assertIsNone(K.category_from_name("Сериал - Русский"))

    def test_rows_python(self):
        r = K.parse_rows(fx("browse_search.html"), date(2026, 10, 26))
        self.assertEqual([x["t"] for x in r], ["2091001", "2090555", "2089999", "1500001"])
        self.assertEqual((r[0]["cat"], r[0]["seeds"], r[0]["leech"], r[0]["added"]), (46, 57, 9, "2026-10-26T03:12:00"))

    def test_scrub(self):
        s = K.scrub('<a href="/download.php?id=1&passkey=abc123">x</a> magnet:?xt=urn:btih:' + "a" * 40 + "&tr=http://t/ann?uk=SECRET")
        self.assertNotIn("abc123", s)
        self.assertNotIn("SECRET", s)


JS_PARSE = r"""
const fs = require('fs'); const [js, rows, det] = process.argv.slice(1);
const k = require(js);
const html = fs.readFileSync(rows, 'utf8');
const d = k.parseDetails(fs.readFileSync(det, 'utf8'));
console.log(JSON.stringify({rows: k.parseRows(html, new Date(2026, 9, 26, 12, 0)), cats: k.parseCategories(html), d,
  enc: k.enc1251('Дюна: Часть'), years: k.rowYears('Медведь (5 сезон) / The Bear / 2026 / ПМ / WEB-DL (1080p)'),
  problem: [k.pageProblem('<title>Just a moment...</title>'), k.pageProblem('<a href="/details.php?id=1">Just a moment</a>'),
            k.pageProblem('<td class="bt"></td><a href="/details.php?id=1">Just a moment</a>'),
            k.pageProblem('<h1>Технические работы</h1>')],
  sel: [k.select({cat: 46, title: 'М / M / 2026 / ДБ / WEB-DL (2160p)'}, {lo: 2026, hi: null, need: ['4k']}, new Set([46])),
        k.select({cat: 45, title: 'М / M / 2026 / ДБ / WEB-DL (2160p)'}, {lo: 2026, hi: null, need: ['any']}, new Set([46])),
        k.select({cat: 46, title: 'М / M / 2020 / ДБ / WEB-DL (2160p)'}, {lo: 2026, hi: 2026, need: ['any']}, new Set([46]))]}));
"""


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorParsers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = node(JS_PARSE, FIX / "browse_search.html", FIX / "details_2091001.html")

    def test_rows_categories(self):
        rows = self.r["rows"]
        self.assertEqual([x["t"] for x in rows], ["2091001", "2090555", "2089999", "1500001"])
        self.assertEqual((rows[1]["cat"], rows[1]["added"], rows[3]["added"]), (17, "2026-10-25T22:10:00", "2015-03-12T10:00:00"))
        self.assertIn({"id": 99, "name": "Кино - Новый раздел"}, self.r["cats"])

    def test_details(self):
        d = self.r["d"]
        self.assertEqual((d["cat"], d["seeders"], d["leechers"], d["size_bytes"], d["reged"]),
                         (46, 57, 9, 44237899776, "2026-10-26T03:12:00"))
        self.assertEqual(d["tabIds"], [1, 2])
        self.assertIn("Перевод:", d["post_html"])
        self.assertNotIn("Комментарий", d["post_html"])

    def test_helpers(self):
        self.assertEqual(self.r["enc"], "%C4%FE%ED%E0%3A+%D7%E0%F1%F2%FC")
        self.assertEqual(self.r["years"], [2026, 2026])
        # the live challenge page (2026-10-26 10:44) links details.php?id=… itself: only real markup means «no problem»
        self.assertEqual(self.r["problem"], ["cloudflare", "cloudflare", None, "maintenance"])
        self.assertEqual(self.r["sel"], [True, False, False])


JS_LOOP = r"""
// the loop with fetch / localStorage mocked: kinozal pages from cp1251 files, bridge POSTs captured
const fs = require('fs'); const [js, browse, det, mode] = process.argv.slice(1);
const posted = []; const store = {}; let cf = mode === 'cloudflare' ? 2 : 0;
globalThis.document = {};
globalThis.localStorage = {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }};
const file = p => ({ok: true, status: 200, arrayBuffer: async () => { const b = fs.readFileSync(p); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); }});
const page = (html, status) => ({ok: (status || 200) === 200, status: status || 200, arrayBuffer: async () => new TextEncoder().encode(html).buffer});
const empty = page('<html><table class="t_peer"></table></html>');
globalThis.fetch = async (url, opts) => {
  if (url.startsWith('http://127.0.0.1:8765/ingest/')) { posted.push(JSON.parse(opts.body)); return {ok: true, status: 200}; }
  if (cf-- > 0) return page('<title>Just a moment...</title><div class="cf-chl"></div>', 403);
  if (url.includes('/browse.php?c=1002&d=2026&t=0&f=0&page=0') || (mode === 'cap' && url.includes('/browse.php?c=1002'))) return file(browse);
  if (url.includes('/browse.php?c=1002')) return empty;
  if (url.endsWith('/browse.php')) return file(browse);
  if (url.includes('/browse.php?s=')) return file(browse);
  if (url.includes('/details.php?id=')) return file(det);
  if (url.includes('get_srv_details.php') && url.includes('pagesd=2')) return page('<div>General<br>Format : MPEG-4<br>Video<br>Format : HEVC<br>Width : 3 840 pixels</div>');
  if (url.includes('action=2')) return page('Инфо хеш: ' + 'AB'.repeat(20));
  return {ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0)};
};
require(js);
const msg = __kzCollect({run: 't', base: 'https://kinozal.guru', delayMs: 1, waitPollMs: 5, cats: [46, 17, 21], yearFrom: 2024,
  known: [], topics: [], maxTopics: 5, searchPages: 1,
  listing: [{c: 1002, d: 2026, since: '2026-10-25T00:00:00', startPage: 0, maxPages: 5}],
  queries: [{q: 'Shrek', lo: 2001, hi: 2001, need: ['any']}]});
(async () => {
  for (let i = 0; i < 400 && !/^(done|stopped|crashed)/.test(__kzStatus().state || ''); i++) await new Promise(r => setTimeout(r, 25));
  console.log(JSON.stringify({msg, status: __kzStatus(), posted}));
})();
"""


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorLoopAndIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("browse_search.html", "details_2091001.html"):     # kinozal serves windows-1251
            (self.tmp / name).write_bytes(fx(name).replace('charset="windows-1251"', "").encode("cp1251", errors="replace"))
        self.p = [mock.patch.object(KZ, "STATE", self.tmp / "state.json"), mock.patch.object(KZ, "ROWS", self.tmp / "rows.json"),
                  mock.patch.object(KZ, "CACHE", self.tmp)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def run_loop(self, mode=""):
        return node(JS_LOOP, self.tmp / "browse_search.html", self.tmp / "details_2091001.html", mode)

    def test_loop_ingest_sync_point_and_resume(self):
        r = self.run_loop()
        self.assertEqual(r["status"]["state"], "done", r["status"])
        types = [p.get("type") for p in r["posted"]]
        self.assertEqual(types, ["categories", "listing", "search", "topics", "final"])
        lst = r["posted"][1]
        self.assertTrue(lst["done"])
        self.assertEqual(lst["reason"], "last page")           # page 0 has rows newer than `since`, page 1 is empty
        tps = r["posted"][3]["topics"]
        # listing rows of collected categories from 2024 (2091001 series, 2090555 film) + the selected search hit (Shrek 2001)
        self.assertEqual(sorted(t["topic_id"] for t in tps), ["1500001", "2090555", "2091001"])
        bear = next(t for t in tps if t["topic_id"] == "2091001")
        self.assertEqual(bear["btih"], "ab" * 20)
        self.assertEqual([t.get("name") for t in bear["tabs"]], ["MediaInfo"])
        self.assertIn("probe", bear)
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.migrate(conn)
        st = KZ.ingest(conn, r["posted"], today=date(2026, 10, 26))
        self.assertEqual((st["topics"], st["pages"]["pages_stored"], st["groups_synced"]), (3, 3, ["1002:2026"]))
        self.assertIn("99 Кино - Новый раздел → foreign_films", st["unknown_categories"])
        row = conn.execute("SELECT resolution, best_voice, season, btih, size_bytes, url FROM releases "
                           "WHERE source='kinozal' AND source_id='2091001'").fetchone()
        self.assertEqual(tuple(row), (2160, "dub", "5", "ab" * 20, 44237899776, "https://kinozal.guru/details.php?id=2091001"))
        self.assertIsNone(conn.execute("SELECT 1 FROM releases WHERE source='kinozal' AND source_id='2089999'").fetchone())
        state = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        # the fixture row says «сегодня в 03:12»: the collector (node) dates it by the real clock, so the sync point is
        # today, not the date the test was written (failed on 2026-10-27)
        from datetime import timedelta
        self.assertEqual(state["groups"]["1002:2026"]["last_success"]["newest"], f"{date.today():%Y-%m-%d}T03:12:00")
        self.assertEqual(KZ._since(state, "1002:2026", "2024-01-01T00:00:00", False),
                         f"{date.today() - timedelta(days=2):%Y-%m-%d}T03:12:00")
        # idempotent
        st2 = KZ.ingest(conn, r["posted"], today=date(2026, 10, 26))
        self.assertEqual(st2["pages"]["new"], 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM releases WHERE source='kinozal'").fetchone()[0], 3)
        # resume of a finished run: nothing left
        cfg = {"run": "t", "listing": [{"c": 1002, "d": 2026, "since": "x", "startPage": 0}],
               "queries": [{"q": "Shrek", "lo": 2001, "hi": 2001, "need": ["any"]}], "yearFrom": 2024}
        new, info = KZ.resume_job(cfg, r["posted"], conn)
        self.assertEqual((info["listing_left"], info["queries_left"], info["topics_left"]), ([], 0, 0))
        self.assertEqual(new["run"], "t-r1")

    def test_page_cap_ends_the_slice(self):
        # kinozal repeats page 99 for every page ≥ 100: a repeated page ends the slice («page cap») instead of looping on
        r = self.run_loop("cap")
        lst = [p for p in r["posted"] if p.get("type") == "listing"][-1]
        self.assertEqual((lst["done"], lst["reason"], lst["d"]), (True, "page cap", 2026))
        cats = next(p for p in r["posted"] if p.get("type") == "categories")
        self.assertEqual([y["id"] for y in cats["years"]], [0, 2026, 2025])

    def test_cloudflare_waits_on_the_same_request(self):
        r = self.run_loop("cloudflare")
        self.assertEqual(r["status"]["state"], "done", r["status"])
        states = [p["state"] for p in r["posted"] if p.get("kind") == "kinozal_status"]
        self.assertEqual(states, ["waiting: cloudflare", "waiting: cloudflare"])
        self.assertEqual([p.get("type") for p in r["posted"] if p.get("kind") == "kinozal"][0], "categories")


class TestStoreSource(unittest.TestCase):
    def test_kinozal_source_accepted(self):
        conn = sqlite3.connect(":memory:")
        db.migrate(conn)
        N.ensure_sources(conn)
        st = N.new_stats()
        rid = N.store_topic(conn, "kinozal", "1", "Шрек / Shrek / 2001 / ДБ / BDRip (1080p)", "animation", K.topic_url(1), st,
                            set(), title_parse=K.parse_title_only)
        self.assertTrue(rid)
        self.assertEqual(conn.execute("SELECT best_voice, resolution FROM releases WHERE id=?", (rid,)).fetchone(), ("dub", 1080))


if __name__ == "__main__":
    unittest.main()
