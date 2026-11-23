"""R212 / R322 for rutracker and toloka (owner, 2026-11-13: «ищем только всё новое для новых дат и новых фильмов и не трогаем
ничего старого на что материал уже найден … Если даже новое качество для фильма вышло на трекере … это просто новое
качество для обновления»). Offline: in-memory DB, temp state files, the collectors through node with fetch mocked.

  C1 films.sources.rutracker — a query searched cleanly is not sent again (only new titles / seasons / needs, `--extra`,
     `--again`); a re-sent changed query is searched in full;
  C2 films.sources.toloka    — the same;
  C3 both collectors         — the job's `listing` (date-ordered forum listing back to the forum's sync point) keeps rows
     of tracked titles (every built query) and of year_from and later; the ingest takes them like search rows.

The listing was written without a live run: check it on the next «обнови базу» (update-db steps 6 / 6а).
Run: python -X utf8 -m unittest tests.test_incremental_rt_tl
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
from films.parse.rutracker import split_topic
from films.sources import rutracker as rt
from films.sources import toloka as tl

ROOT = Path(__file__).resolve().parent.parent
FIX_RT = Path(__file__).resolve().parent / "fixtures" / "rutracker"
FIX_TL = Path(__file__).resolve().parent / "fixtures" / "toloka"
JS_RT = ROOT / "films" / "browser" / "rutracker_collect.js"
JS_TL = ROOT / "films" / "browser" / "toloka_collect.js"
NODE = shutil.which("node")


def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def node(js: Path, code: str, *args) -> object:
    out = subprocess.run([NODE, "-e", code, str(js), *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                         check=True)
    return json.loads(out.stdout)


def q(name, lo, hi, need, titles, why="r9"):
    return {"q": name, "name": name, "lo": lo, "hi": hi, "need": need, "titles": titles, "why": [why]}


# ----------------------------------------------------------------------------------------------------------------- C1

class TestRutrackerOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"

    def test_pending(self):
        st = {"queries": {
            "dune|2024|2024": {"at": "x", "newest": 1760000000, "titles": [1], "need": ["4k", "hdr"]},
            "alien|2024|2024": {"at": "x", "newest": 1750000000},                       # before R322: no coverage
            "heat|1995|1995": {"at": "x", "newest": 1}}}
        built = [q("Dune", 2024, 2024, ["4k"], [1]),                 # covered: not sent
                 q("Alien", 2024, 2024, ["any"], [5]),               # legacy entry: adopted, not sent
                 q("South Park", 2023, None, ["any"], [7]),          # never searched
                 q("Heat", 1995, 1995, ["dub"], [9], why="extra"),   # explicit name: always
                 ]
        send, info = rt.pending(built, st)
        self.assertEqual([(x["q"], x["send"]) for x in send], [("South Park", "new"), ("Heat", "extra")])
        self.assertEqual((info["skipped"], info["adopted"], info["built"]), (2, 1, 4))
        self.assertEqual(st["queries"]["alien|2024|2024"]["titles"], [5])
        # a new season (title) or a new need: sent again, searched in full
        more = [q("Dune", 2024, 2024, ["4k"], [1, 2]), q("Alien", 2024, 2024, ["any"], [5]),
                q("Dune Two", 2024, 2024, ["4k", "dub"], [3])]
        st["queries"]["dune two|2024|2024"] = {"newest": 1, "titles": [3], "need": ["4k"]}
        send, info = rt.pending(more, st)
        self.assertEqual([(x["q"], x["send"]) for x in send], [("Dune", "changed"), ("Dune Two", "changed")])
        # --again (owner's request only): everything
        send, info = rt.pending(more, st, again=True)
        self.assertEqual([x["send"] for x in send], ["changed", "again", "changed"])

    def test_need_covered(self):
        self.assertTrue(rt.need_covered(["4k"], ["any"]))
        self.assertTrue(rt.need_covered(["4k"], ["4k", "hdr"]))
        self.assertFalse(rt.need_covered(["any"], ["4k", "hdr", "dub"]))
        self.assertFalse(rt.need_covered(["flarrow"], ["4k"]))
        self.assertTrue(rt.need_covered([], ["4k"]))

    def test_round_trip_and_since(self):
        built = [q("Dune", 2024, 2024, ["4k"], [1]), q("Alien", 2024, 2024, ["hdr"], [2])]
        st = rt.load_state(self.state)
        send, _ = rt.pending(built, st)
        rt.save_state(st, self.state)
        qs = rt.with_since(send, self.state)
        self.assertEqual([x.get("since") for x in qs], [None, None])
        out = Path(self.tmp.name) / "run"
        rt.write_run(qs, [], out=out)
        cfg = json.loads((out / "queries_run.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["queries"][0], {"q": "Dune", "lo": 2024, "hi": 2024, "need": ["4k"], "titles": [1]})
        # the collector echoes the query; Dune clean, Alien failed
        pays = [{"run": cfg["run"], "query": cfg["queries"][0], "errors": [], "at": "t",
                 "searches": [{"rows": [{"t": "1", "added": 1760000000}]}]},
                {"run": cfg["run"], "query": cfg["queries"][1], "errors": [{"q": "Alien", "err": "HTTP 502"}], "searches": []},
                {"run": cfg["run"], "query": None, "listing": {"f": 313}, "errors": [], "searches": [{"rows": []}]}]
        self.assertEqual(rt.record_sync(pays, self.state), 1)             # the listing payload is not a query sync
        ent = rt.load_state(self.state)["queries"]["dune|2024|2024"]
        self.assertEqual((ent["titles"], ent["need"], ent["newest"]), ([1], ["4k"], 1760000000))
        send, info = rt.pending(built, rt.load_state(self.state))
        self.assertEqual([x["q"] for x in send], ["Alien"])                # only the failed one comes back
        # a changed query ignores its old sync point, --again keeps it
        ch = rt.with_since([dict(built[0], send="changed"), dict(built[0], send="again")], self.state)
        self.assertEqual([x.get("since") for x in ch], [None, 1760000000 - 2 * 86400])


# ----------------------------------------------------------------------------------------------------------------- C2

class TestTolokaOnce(unittest.TestCase):
    def test_pending_and_since(self):
        state = {"queries": {
            "dune": {"q": "Dune", "newest_added": "2026-10-01", "titles": [1], "need": ["ukdub"]},
            "up": {"q": "Up", "newest_added": "2026-09-01"},                             # before R322
            "alien": {"q": "Alien", "newest_added": "2026-09-01", "titles": [3], "need": ["4k"]}}}
        built = [q("Dune", 2024, 2024, ["4k", "hdr"], [1]),        # 'ukdub' took every row: covered
                 q("Up", 2009, 2009, ["dub"], [2]),                # adopted
                 q("Alien", 2024, 2024, ["4k", "hdr"], [3]),       # a new need: changed
                 q("New", 2025, 2025, ["dub"], [4])]
        send, info = tl.pending(built, state)
        self.assertEqual([(x["q"], x["send"]) for x in send], [("Alien", "changed"), ("New", "new")])
        self.assertEqual((info["skipped"], info["adopted"]), (2, 1))
        self.assertTrue(tl.need_covered(["4k"], ["dub"]))
        self.assertFalse(tl.need_covered(["ukdub"], ["4k", "hdr"]))
        with tempfile.TemporaryDirectory() as d:
            tl.write_run(send + [dict(built[0], send="again")], [], out=Path(d), state=state)
            cfg = json.loads((Path(d) / "queries_run.json").read_text(encoding="utf-8"))
        # changed: searched in full; new: never synced; again: from its sync point
        self.assertEqual([(c["q"], c.get("since"), c.get("titles")) for c in cfg["queries"]],
                         [("Alien", None, [3]), ("New", None, [4]), ("Dune", "2026-09-29", [1])])
        tl.remember_syncs(state, [{"run": "R", "at": "t", "query": cfg["queries"][0], "errors": [],
                                   "searches": [{"rows": [{"t": "5", "added": "2026-11-01 10:00"}]}]},
                                  {"run": "R", "query": None, "listing": {"f": 96}, "errors": [], "searches": []}])
        self.assertEqual((state["queries"]["alien"]["titles"], state["queries"]["alien"]["need"]), ([3], ["4k", "hdr"]))
        self.assertEqual([x["q"] for x in tl.pending(built, state)[0]], ["New"])


# ----------------------------------------------------------------------------------------------------------------- C3

RT_TITLES = [  # (forum, title) samples for the Python / JS twins
    ("Зарубежные сериалы (HD Video)", "Южный Парк / South Park / Сезон: 27 / Серии: 1-5 из 5 (Мэтт Стоун) [2025, США, WEB-DL 2160p, SDR] DVO"),
    ("Зарубежные сериалы (HD Video)", "Южный Парк / South Park / Сезон: 27 (Мэтт Стоун) [2025, США, WEB-DL 1080p] DVO"),
    ("Зарубежное кино (UHD Video)", "Шрек / Shrek (Эндрю Адамсон / Andrew Adamson) [2001, США, UHD BDRemux 2160p, HDR10] Dub"),
    ("Зарубежное кино (HD Video)", "Шрек / Shrek (Эндрю Адамсон) [2001, США, BDRip 1080p] MVO"),
    ("Зарубежное кино (HD Video)", "Шрек / Shrek [2004, США, BDRip 1080p] Dub"),
    ("Аниме (HD Video)", "Магическая битва / Jujutsu Kaisen [TV] [2025, WEB-DL 1080p]"),
    ("Аниме (HD Video)", "Ван-Пис / One Piece [TV] [1999-2025, WEB-DL 1080p] Dub"),
    ("Зарубежное кино (HD Video)", "[ANIME] Что-то / Something [2025, WEB-DL 1080p]"),
    ("Горячие Новинки (Игры для Windows)", "South Park: Snow Day! [P] (2024, Action)"),
    ("Мультсериалы (HD Video)", "South Park — без года"),
    ("Зарубежное кино (HD Video)", "Ёлки / Yolki (Режиссёр) [2010, BDRip] Dub"),
]
RT_INDEX = {"south park": [[2023, None, ["4k", "hdr", "dub"]]], "shrek": [[2001, 2001, ["4k", "hdr"]]],
            "ван пис": [[1999, None, ["any"]]], "елки": [[2010, 2010, ["any"]]]}
RT_EXPECT = ["tracked", "year", "tracked", None, None, None, "tracked", None, None, None, "tracked"]

TL_TITLES = [
    ("Фільми HD українською", "Дюна: Частина друга / Dune: Part Two (2024) BDRemux 2160p HDR10 DV Ukr/Eng"),
    ("Фільми HD українською", "Дюна: Частина друга / Dune: Part Two (2024) WEB-DL 1080p Ukr/Eng"),
    ("Фільми HD українською", "Дюна / Dune (2021) BDRip 1080p Ukr/Eng"),
    ("Дюна (1984) — обговорення та документальні фільми", "Дюна Ходоровського / Jodorowsky's Dune (2013) WEB-DL"),
    ("Фільми", "Старе / Old One (2015) DVDRip Ukr"),
    ("Серіали в HD", "Новий серіал / New Show (Сезон 1) (2025) WEB-DL 1080p Ukr"),
]
TL_INDEX = {"dune part two": [[2024, 2024, ["4k"]]], "дюна": [[2021, 2021, ["ukdub"]]]}
TL_EXPECT = ["tracked", "year", "tracked", None, None, "year"]


class TestListingPython(unittest.TestCase):
    def test_rutracker_match(self):
        got = [rt.listing_match(t, f, RT_INDEX, 2024) for f, t in RT_TITLES]
        self.assertEqual(got, RT_EXPECT)
        self.assertIsNone(rt.listing_match(RT_TITLES[1][1], RT_TITLES[1][0], RT_INDEX, 2024, anime_only_tracked=True))
        self.assertEqual(rt.listing_names("[MULT] Южный Парк / South Park (Стоун) Сезоны: 1-28 [1997-2025, x]"),
                         ["южный парк", "south park сезоны 1 28"])

    def test_toloka_match(self):
        self.assertEqual([tl.listing_match(t, f, TL_INDEX, 2024) for f, t in TL_TITLES], TL_EXPECT)

    def test_index_takes_every_built_query(self):
        idx = rt.listing_index([q("South Park", 2023, None, ["any"], [1]), q("F1 2025", 2025, 2025, ["dub"], [2]),
                                dict(q("F1", 2025, 2025, ["dub"], [2]), q="F1 2025"), q("X", 2020, 2020, ["4k"], [3])])
        self.assertEqual(idx["south park"], [[2023, None, ["any"]]])
        self.assertIn("f1", idx)                       # the bare name, not the «F1 2025» query text
        self.assertNotIn("x", idx)                     # too short

    def test_since_points(self):
        conn = mem_db()
        now = 1_800_000_000
        self.assertEqual(rt.listing_since({}, 313, conn, now=now), now - rt.LISTING_DEFAULT_DAYS * 86400)
        conn.execute("INSERT INTO runs (kind, started_at, finished_at) VALUES ('update_db', '2026-11-10 10:00:00', "
                     "'2026-11-10 12:00:00')")
        self.assertEqual(rt.listing_since({}, 313, conn, now=now), rt._unix("2026-11-08 12:00:00"))
        st = {"listing": {"313": {"newest": 1763000000}}}
        self.assertEqual(rt.listing_since(st, 313, conn, now=now), 1763000000 - 2 * 86400)
        self.assertEqual(tl.listing_since({}, 96, conn), "2026-11-08")
        self.assertEqual(tl.listing_since({"listing": {"96": {"newest_added": "2026-11-12"}}}, 96, conn), "2026-11-10")
        self.assertEqual(tl.listing_since({}, 96, None, today=date(2026, 11, 13)), "2026-10-14")

    def test_job_and_write_run(self):
        conn = mem_db()
        built = [q("South Park", 2023, None, ["any"], [1]), q("Dune", 2024, 2024, ["4k"], [2])]
        job = rt.listing_job(conn, built, {}, year_from=2024)
        self.assertEqual(len(job["forums"]), len(rt.LISTING_FORUMS))
        self.assertEqual({f["f"] for f in job["forums"] if f["trackedOnly"]}, {1105, 1389, 2491})
        self.assertEqual(set(job["titles"]), {"south park", "dune"})
        with tempfile.TemporaryDirectory() as d:
            res = rt.write_run([dict(built[1], send="new")], [], out=Path(d), part_size=1, listing=job)
            cfg = json.loads((Path(d) / "queries_run.json").read_text(encoding="utf-8"))
            self.assertEqual((cfg["listing"]["yearFrom"], res["listing_forums"]), (2024, len(rt.LISTING_FORUMS)))
            # nothing to search: the listing still gets its inline part
            res = rt.write_run([], [], out=Path(d), listing=job)
            self.assertEqual(res["inline_parts"], ["run_inline_1.js"])
            self.assertIn('"listing"', (Path(d) / "run_inline_1.js").read_text(encoding="utf-8"))
            tres = tl.write_run([], [], out=Path(d) / "tl", listing=tl.listing_job(conn, built, {}, year_from=2024))
            self.assertEqual((tres["inline_parts"], tres["listing_forums"]), (["run_inline_1.js"], len(tl.LISTING_FORUMS)))
            if NODE:
                for f in (Path(d) / "run_inline_1.js", Path(d) / "tl" / "run_inline_1.js"):
                    subprocess.run([NODE, "--check", str(f)], check=True)

    def test_ingest_listing_rows(self):
        conn = mem_db()
        topic = split_topic((FIX_RT / "topic_6731886.html").read_text(encoding="utf-8"))
        topic.update(topic_id="6731886", fetched_at="2026-11-13T10:00:00Z")
        rows = [{"t": "6731886", "forum": "Зарубежные сериалы (HD Video)", "title": RT_TITLES[0][1], "added": 1763000000,
                 "sel": 1, "why": "tracked", "seeds": 5},
                {"t": "6755275", "forum": "Зарубежные сериалы (HD Video)", "title": RT_TITLES[1][1], "added": 1763000100,
                 "sel": 1, "why": "year", "seeds": 9, "size": 100},
                {"t": "1", "forum": "Зарубежное кино (HD Video)", "title": RT_TITLES[3][1], "added": 1763000200, "sel": 0}]
        pay = {"v": 1, "kind": "rutracker", "run": "L", "seq": -1, "query": None, "listing": {"f": 2366, "since": 1},
               "searches": [{"q": "f=2366", "total": 3, "rows": rows}], "topics": [topic], "errors": []}
        st = rt.ingest(conn, [pay])
        self.assertEqual((st["listing_forums"], st["listing_rows"], st["queries"], st["new"], st["pages_stored"],
                          st["name_only"]), (1, 3, 0, 2, 1, 1))
        self.assertIsNone(conn.execute("SELECT 1 FROM releases WHERE source_id = '1'").fetchone())
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            bad = dict(pay, listing={"f": 313}, errors=[{"q": "f=313", "err": "HTTP 502"}])
            self.assertEqual(rt.record_listing([pay, bad, {"final": True}], path), 1)
            self.assertEqual(rt.load_state(path)["listing"]["2366"]["newest"], 1763000200)
            self.assertNotIn("313", rt.load_state(path)["listing"])
        tstate = {}
        self.assertEqual(tl.remember_listing(tstate, [{"run": "R", "listing": {"f": 96}, "errors": [],
                                                       "searches": [{"rows": [{"added": "2026-11-01 10:00"}]}]}]), 1)
        self.assertEqual(tstate["listing"]["96"]["newest_added"], "2026-11-01")


@unittest.skipUnless(NODE, "node not installed")
class TestListingJsTwins(unittest.TestCase):
    CODE = r"""
const a = require(process.argv[1]); const s = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
console.log(JSON.stringify({
  m: s.rows.map(([f, t]) => a.listingMatch({forum: f, title: t}, {titles: s.index, yearFrom: 2024}, false)),
  names: s.rows.map(([f, t]) => a.titleNames(t)), anime: a.listingMatch({forum: s.rows[1][0], title: s.rows[1][1]},
                                                                        {titles: s.index, yearFrom: 2024}, true)}));
"""

    def run_js(self, js, rows, index):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "in.json"
            p.write_text(json.dumps({"rows": rows, "index": index}, ensure_ascii=False), encoding="utf-8")
            return node(js, self.CODE, p)

    def test_rutracker_js_equals_python(self):
        r = self.run_js(JS_RT, RT_TITLES, RT_INDEX)
        self.assertEqual(r["m"], RT_EXPECT)
        self.assertEqual(r["names"], [rt.listing_names(t) for _, t in RT_TITLES])
        self.assertIsNone(r["anime"])

    def test_toloka_js_equals_python(self):
        r = self.run_js(JS_TL, TL_TITLES, TL_INDEX)
        self.assertEqual(r["m"], TL_EXPECT)
        self.assertEqual(r["names"], [rt.listing_names(t) for _, t in TL_TITLES])


JS_RT_LOOP = r"""
// the rutracker loop with a listing only: fetch / localStorage mocked, pages windows-1251, bridge POSTs captured
const fs = require('fs'); const [js, search, topic, cfgPath] = process.argv.slice(1);
const posted = []; const urls = []; const store = {};
globalThis.document = {};
globalThis.localStorage = {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }};
const resp = path => ({ok: true, status: 200,
  arrayBuffer: async () => { const b = fs.readFileSync(path); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); }});
globalThis.fetch = async (url, opts) => {
  if (url.startsWith('http://127.0.0.1:8765/ingest/')) { posted.push(JSON.parse(opts.body)); return {ok: true, status: 200}; }
  urls.push(url);
  if (url.includes('/tracker.php?f=2366&o=1&s=2') || url.includes('/tracker.php?search_id=')) return resp(search);
  if (url.includes('/viewtopic.php?t=6731886')) return resp(topic);
  return {ok: false, status: 404};
};
require(js);
__rtCollect(JSON.parse(fs.readFileSync(cfgPath, 'utf8')));
(async () => {
  for (let i = 0; i < 200 && !/^(done|stopped|crashed)/.test(__rtStatus().state || ''); i++) await new Promise(r => setTimeout(r, 30));
  console.log(JSON.stringify({status: __rtStatus(), posted, urls}));
})();
"""


@unittest.skipUnless(NODE, "node not installed")
class TestRutrackerListingLoop(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        for name in ("search_south_park.html", "topic_6731886.html"):
            (self.d / name).write_bytes((FIX_RT / name).read_text(encoding="utf-8").replace('charset="Windows-1251"', "")
                                        .encode("cp1251", errors="replace"))

    def run_loop(self, since, search="search_south_park.html", max_pages=10):
        cfg = {"run": "L", "delayMs": 1, "known": [], "queries": [],
               "listing": {"forums": [{"f": 2366, "name": "Зарубежные сериалы (HD Video)", "since": since}],
                           "titles": {"south park": [[2023, None, ["4k", "hdr", "dub"]]]}, "yearFrom": 2024,
                           "maxTopics": 5, "maxPages": max_pages}}
        p = self.d / "cfg.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return node(JS_RT, JS_RT_LOOP, self.d / search, self.d / "topic_6731886.html", p)

    def test_listing_payload_and_ingest(self):
        r = self.run_loop(1700000000)
        self.assertEqual(r["status"]["state"], "done", r["status"])
        self.assertEqual(r["status"]["listing"], "1/1")
        self.assertTrue(r["urls"][0].endswith("/tracker.php?f=2366&o=1&s=2"), r["urls"])
        pay, fin = r["posted"]
        self.assertTrue(fin["final"])
        self.assertEqual((pay["listing"]["f"], pay["query"]), (2366, None))
        why = {x["t"]: x.get("why") for x in pay["searches"][0]["rows"] if x["sel"]}
        self.assertEqual(why, {"6731886": "tracked", "6755275": "year", "6554047": "year"})
        self.assertEqual([t["topic_id"] for t in pay["topics"]], ["6731886"])     # pages only for tracked titles
        conn = mem_db()
        st = rt.ingest(conn, r["posted"])
        self.assertEqual((st["listing_forums"], st["new"], st["pages_stored"], st["name_only"]), (1, 3, 1, 2))
        self.assertEqual(conn.execute("SELECT resolution FROM releases WHERE source_id='6731886'").fetchone()[0], 2160)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            rt.record_listing(r["posted"], path)
            self.assertEqual(rt.listing_since(rt.load_state(path), 2366), 1756054800 - 2 * 86400)

    def test_since_and_incomplete(self):
        r = self.run_loop(1900000000)                                     # everything older than the sync point
        pay = r["posted"][0]
        self.assertFalse([x for x in pay["searches"][0]["rows"] if x["sel"]])
        self.assertFalse(pay["topics"])
        # a full first page newer than the point, 120 results, a page limit of 1: the forum is reported incomplete
        import re
        s = (FIX_RT / "search_south_park.html").read_text(encoding="utf-8")
        row = re.search(r'<tr\b[^>]*id="trs-tr-6755275"[\s\S]*?</tr>', s).group(0)
        full = s.replace(row, "".join(row.replace("6755275", str(7000000 + i)) for i in range(50)), 1)
        full = full.replace("Результатов поиска: 9", "Результатов поиска: 120")
        (self.d / "full.html").write_bytes(full.replace('charset="Windows-1251"', "").encode("cp1251", errors="replace"))
        r = self.run_loop(1200000000, "full.html", max_pages=1)          # every row newer than the point
        pay = r["posted"][0]
        self.assertTrue(pay.get("incomplete"))
        self.assertEqual(pay["searches"][0]["pages"], 1)
        st = rt.ingest(mem_db(), r["posted"])
        self.assertEqual(st["listing_incomplete"], [2366])


JS_TL_LOOP = r"""
const fs = require('fs');
const posted = []; const urls = [];
global.window = global; global.document = {};
global.localStorage = {d: {}, getItem(k) { return this.d[k] || null; }, setItem(k, v) { this.d[k] = v; }};
const search = fs.readFileSync(process.argv[2], 'utf8'), topic = fs.readFileSync(process.argv[3], 'utf8');
global.fetch = async (url, opt) => {
  if (opt && opt.method === 'POST') { posted.push(JSON.parse(opt.body)); return {ok: true}; }
  urls.push(url);
  const body = /\/t681234$/.test(url) ? topic : /tracker\.php/.test(url) ? (/start=50/.test(url) ? '<table></table>' : search)
             : '<a href="login.php?logout=true">Вихід</a><a href="f96">Фільми HD українською</a>';
  return {ok: true, status: 200, url, text: async () => body};
};
require(process.argv[1]);
global.setTimeout = (f) => f();
window.__tlCollect(JSON.parse(fs.readFileSync(process.argv[4], 'utf8')));
const wait = () => new Promise(r => setImmediate(r));
(async () => {
  for (let i = 0; i < 400 && !/^(done|stopped|crashed)/.test(window.__tl.state); i++) await wait();
  console.log(JSON.stringify({state: window.__tl.state, status: window.__tlStatus(), posted, urls}));
})();
"""


@unittest.skipUnless(NODE, "node not installed")
class TestTolokaListingLoop(unittest.TestCase):
    def test_listing_payload_and_ingest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cfg.json"
            p.write_text(json.dumps({"run": "L", "delayMs": 0, "known": ["681300x"], "queries": [], "listing": {
                "forums": [{"f": 96, "name": "Фільми в HD", "since": "2024-01-01"}], "titles": TL_INDEX, "yearFrom": 2024,
                "maxTopics": 5}}, ensure_ascii=False), encoding="utf-8")
            r = node(JS_TL, JS_TL_LOOP, FIX_TL / "search_dune.html", FIX_TL / "topic_681234.html", p)
        self.assertEqual(r["state"], "done")
        self.assertEqual(r["status"]["listing"], "1/1")
        self.assertIn("https://toloka.to/tracker.php?f=96&o=1&s=2", r["urls"])
        pay = [x for x in r["posted"] if x.get("listing")][0]
        sel = {x["t"]: x.get("why") for x in pay["searches"][0]["rows"] if x["sel"]}
        self.assertEqual(sel, {"681234": "tracked", "681300": "year"})          # «Dune (2021)» is older than `since`
        self.assertTrue(pay["searches"][0].get("sinceStop"))
        self.assertEqual([t["topic_id"] for t in pay["topics"]], ["681234"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tl, "CACHE", Path(d)):
            conn = mem_db()
            st = tl.ingest(conn, r["posted"], date(2026, 11, 13))
        self.assertEqual((st["listing_forums"], st["queries"], st["pages_stored"], st["name_only"]), (1, 0, 1, 1))
        state = {}
        tl.remember_listing(state, r["posted"])
        self.assertEqual(state["listing"]["96"]["newest_added"], "2025-06-10")


if __name__ == "__main__":
    unittest.main()
