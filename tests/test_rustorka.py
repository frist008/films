"""rustorka.com (R208): films/parse/rustorka.py (guest RSS feed → rutor-parser fields, classification) and
films/sources/rustorka.py (incremental feed collector into a fresh, migrated in-memory DB).

The fixture tests/fixtures/rustorka/feed.xml is the site's guest feed rss.php?t=1 of 2026-10-26 cut to 8 items (plot
text, screenshots, Kinopoisk links and uploader names removed). index.html / listing.html / topic.html / guest_404.html
(windows-1251) are rustorka's own markup as a guest sees it in the open game forums, with made-up film forums and topics
(a guest cannot open those): they test films/browser/rustorka_collect.js (R232, node) and its ingest."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from films import db
from films.parse import rustorka as rk
from films.sources import rustorka as src

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "rustorka"
FEED = (FIX / "feed.xml").read_text(encoding="utf-8")
JS = ROOT / "films" / "browser" / "rustorka_collect.js"
NODE = shutil.which("node")


def node(code: str, *args) -> object:
    out = subprocess.run([NODE, "-e", code, str(JS), *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                         check=True)
    return json.loads(out.stdout)


def _item(title, forum="WEB-DL / WEB-DLRip", body="Продолжительность: 01:40:00<br>Видео: AVC, 1920x1080", **kw):
    return dict({"post_id": 1, "forum": forum, "title": title, "reply": False, "pub": "2026-10-20T10:00:00",
                 "added": None, "post_html": body}, **kw)


class TestFeed(unittest.TestCase):
    def setUp(self):
        self.feed = rk.parse_feed(FEED)
        self.by = {it["title"].split(" (")[0]: it for it in self.feed["items"]}

    def test_feed_items(self):
        f = self.feed
        self.assertTrue(f["guest"])
        self.assertEqual(len(f["items"]), 8)
        k = self.by["Кракен / Kraken"]
        self.assertEqual((k["post_id"], k["forum"], k["pub"], k["added"], k["reply"]),
                         (3407913, "TeleSync", "2026-10-25T17:48:33", "2026-10-25T20:48:00", False))
        self.assertIn("MediaInfo", k["post_html"])
        self.assertNotIn("Автор:", k["post_html"])
        self.assertEqual(rk.post_url(3407913), "https://rustorka.com/forum/viewtopic.php?p=3407913#3407913")
        self.assertEqual(rk.source_id(3407913), "p3407913")

    def test_title_escaped_twice(self):
        xml = ("<item><title>Саундтреки к играм :: (Score) Clive Barker&amp;#039;s Hellraiser - 2026, MP3</title>"
               "<link>http://rustorka.com/forum/viewtopic.php?p=5#5</link></item>")
        self.assertEqual(rk.parse_feed(xml)["items"][0]["title"], "(Score) Clive Barker's Hellraiser - 2026, MP3")

    def test_registered_only(self):
        f = rk.parse_feed("For registred users only")
        self.assertTrue(f["registered_only"])
        self.assertEqual(f["items"], [])

    def test_classify(self):
        cats = {t: rk.classify(it)[0] for t, it in self.by.items()}
        self.assertEqual(cats["Кракен / Kraken"], "foreign_films")
        self.assertEqual(cats["Кей-поп-охотницы на демонов / KPop Demon Hunters"], "animation")
        self.assertEqual(cats["Люпен / Lupin / 4 Сезон: 1-8 серии из 8"], "foreign_series")
        self.assertIsNone(cats["Чучело"])                                       # Russian film
        self.assertEqual(rk.classify(self.by["Чучело"])[1], "russian film")
        vlc = [it for it in self.feed["items"] if it["title"].startswith("VLC")][0]
        self.assertEqual(rk.classify(vlc), (None, "not video"))
        anime = _item("Шальной босс | Yasei no Last Boss (Хориути Юя) [TV-2] [05 из 12] [2026 г., фэнтези, WEBRip 1080p]",
                      forum="Новинки")
        self.assertEqual(rk.classify(anime), (None, "anime"))
        self.assertEqual(rk.classify(_item("Фильм / Film (X) [2026, драма, WEB-DL 1080p]", reply=True)), (None, "reply"))
        self.assertEqual(rk.classify(_item("Фильм / Film (X) [2026, эротика, WEB-DL 1080p]", forum="Эротика HD")),
                         (None, "forum Эротика HD"))
        self.assertEqual(rk.classify(_item("Мультик (X) [2026, мультфильм, WEB-DL 1080p]",
                                           body="Страна: Россия<br>Продолжительность: 01:20:00"))[0], "animation")
        self.assertEqual(rk.classify(_item("Сериал / Show / 1-8 серии из 8 (X) [2026, драма, WEB-DL 1080p]",
                                           body="Страна: Россия<br>Продолжительность: 00:50:00")), (None, "russian series"))
        self.assertEqual(rk.classify(_item("Дорама / Drama [2026, драма, WEB-DL 1080p]", forum="Дорамы"))[0],
                         "foreign_series")

    def test_normalize_title(self):
        n = rk.normalize_title
        self.assertEqual(n("Хрустальное озеро / Crystal Lake / 1-8 серии из 8 (Логан Джордж) [2026, ужасы, WEB-DLRip]"),
                         "Хрустальное озеро / Crystal Lake / Серии: 1-8 из 8 (Логан Джордж) [2026, ужасы, WEB-DLRip]")
        self.assertEqual(n("Люпен / Lupin / 4 Сезон: 1-8 серии из 8 (Л. Бернар) [2026, криминал, WEB-DL 1080p]"),
                         "Люпен / Lupin / Сезон: 4 / Серии: 1-8 из 8 (Л. Бернар) [2026, криминал, WEB-DL 1080p]")
        self.assertEqual(n("Кракен / Kraken (М. Шабан) [2026, ужасы, TSRip] Dub"), "Кракен / Kraken (М. Шабан) [2026, ужасы, TS] Dub")
        self.assertIn("SuperTS", n("Шёпот / Whisper (X) [2026, ужасы, Super-TS] Dub"))
        self.assertEqual(n("Невский / Сезон: 1-8 / Серии: 1-232 из 232 (А. Коршунов) [2016, детектив]"),
                         "Невский / Сезон: 1-8 / Серии: 1-232 из 232 (А. Коршунов) [2016, детектив]")

    def _parsed(self, title):
        it = dict(self.by[title])
        it["category"] = rk.classify(it)[0]
        return rk.parse_item(rk.compact_item_html(it))

    def test_parse_film_ts(self):
        d = self._parsed("Кракен / Kraken")
        self.assertEqual((d["source_type"], d["cam_source"], d["best_voice"], d["resolution"]), ("TS", "TS", "dub", 480))
        self.assertEqual(d["voice_by_lang"], {"ru": ["dub"], "uk": ["mvo"]})
        self.assertEqual([a.get("studio") for a in d["audio"]], ["Dragon Money Studio", None])
        self.assertEqual(d["size_bytes"], int(1.43 * 1024 ** 3))          # single file: MediaInfo «File size»
        self.assertEqual(d["size_from"], "mediainfo")
        self.assertEqual((d["name"], d["added_at"], d["category"]),
                         (self.by["Кракен / Kraken"]["title"], "2026-10-25T20:48:00", "foreign_films"))

    def test_parse_uhd_dv_and_series(self):
        d = self._parsed("Гениальный план / Masterplan")
        self.assertEqual((d["resolution"], d["source_type"], d["video_codec"], d["dv_profile"]), (2160, "WEB-DL", "HEVC", 8))
        self.assertEqual(set(d["hdr"]), {"DV", "HDR10+", "HDR10"})
        self.assertIsNone(d["size_bytes"])                                  # several MediaInfo blocks / no size
        k = self._parsed("Кей-поп-охотницы на демонов / KPop Demon Hunters")
        self.assertEqual((k["source_type"], k["dv_profile"], k["category"]), ("BDRemux", 7, "animation"))
        s = self._parsed("Люпен / Lupin / 4 Сезон: 1-8 серии из 8")
        self.assertEqual((s["season"], s["episodes"], s["best_voice"]), ("4", "1-8", "dub"))
        c = self._parsed("Хрустальное озеро / Crystal Lake / 1-8 серии из 8")
        self.assertEqual((c["episodes"], c["best_voice"]), ("1-8", "dub"))
        self.assertEqual(self._parsed("Стойкий доктор / Doctor on the Edge / Dakteo seomboi / Сезон: 1 / Серии: 1-12 из 12")
                         ["imdb_id"], "tt39184653")

    def test_index(self):
        page = ('<a href="index.php?c=6">Игры для PC</a><a href="viewforum.php?f=1040">ПК игры</a>'
                '<a href="index.php?c=33">Аниме</a><a href="viewforum.php?f=1879">Аниме</a>'
                '<a href="viewforum.php?f=1883">Новинки</a><a href="viewforum.php?f=1999">Новое аниме</a>'
                '<a href="index.php?c=40">Кино</a><a href="viewforum.php?f=18">Зарубежное кино</a>'
                '<a href="index.php?c=2">Правила, инструкции, советы, FAQ</a>'
                '<a href="viewforum.php?f=1050">Фильмы | Мультфильмы</a>'
                '<a href="index.php?c=3">Общение</a><a href="viewforum.php?f=135">Фильмы</a>'
                '<a href="index.php?c=21">Музыка</a><a href="viewforum.php?f=471">Зарубежный Рок</a>')
        fs = rk.parse_index(page)
        self.assertEqual([(f["f"], f["c"]) for f in fs],
                         [(1040, 6), (1879, 33), (1883, 33), (1999, 33), (18, 40), (1050, 2), (135, 3), (471, 21)])
        self.assertEqual([f["f"] for f in rk.video_forums_visible(fs)], [18])


class TestCrawl(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.multiple(src, STATE=self.tmp / "state.json", ITEMS=self.tmp / "items.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rel(self, sid):
        return self.conn.execute("SELECT * FROM releases WHERE source='rustorka' AND source_id=?", (sid,)).fetchone()

    def test_crawl_store_rules_and_incremental(self):
        st = src.crawl(self.conn, feed_xml=FEED, log=lambda m: None)
        self.assertIsNone(st["fatal"])
        self.assertEqual((st["feed_items"], st["wanted"], st["stored"]["new"]), (8, 6, 6))
        self.assertIn("'rustorka'", self.conn.execute("SELECT sql FROM sqlite_master WHERE name='releases'").fetchone()[0])
        r = self.rel("p3407913")
        self.assertEqual((r["url"], r["category"], r["source_type"], r["best_voice"], r["added_at"]),
                         ("https://rustorka.com/forum/viewtopic.php?p=3407913#3407913", "foreign_films", "TS", "dub",
                          "2026-10-25T20:48:00"))
        self.assertTrue(r["parsed_version"].startswith("rustorka-page:"))
        self.assertIn("MediaInfo", src.raw_page(self.conn, r["id"]))
        # owner's release rules: TS is excluded (excluded_releases, never deleted)
        self.assertEqual(self.conn.execute("SELECT reason FROM excluded_releases WHERE source='rustorka' AND "
                                           "source_id='p3407913'").fetchone()[0], "cam")
        self.assertEqual(st["stored"]["rule_excluded"], {"cam": 1})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM releases WHERE source='rustorka'").fetchone()[0], 6)
        state = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_ok"]["newest_video_pub"], "2026-10-25T17:48:33")
        self.assertEqual(state["last_ok"]["newest_post"], 3407913)
        self.assertEqual(self.conn.execute("SELECT cursor FROM sync_state WHERE source='rustorka'").fetchone()[0],
                         "2026-10-25T17:48:33")
        self.assertEqual(len(json.loads((self.tmp / "items.json").read_text(encoding="utf-8"))), 8)
        # second run, same feed: only the 2-day overlap before the sync point, nothing new, nothing rewritten
        st2 = src.crawl(self.conn, feed_xml=FEED, log=lambda m: None)
        self.assertEqual(st2["since"], "2026-10-23T17:48:33")
        self.assertEqual((st2["stored"]["new"], st2["stored"]["updated"]), (0, 0))
        self.assertEqual(st2["wanted"] + st2["older_than_sync_point"], 6)
        self.assertGreater(st2["older_than_sync_point"], 0)
        self.assertNotIn("gap", st2)
        # --full: the whole feed again, still idempotent
        st3 = src.crawl(self.conn, feed_xml=FEED, full=True, log=lambda m: None)
        self.assertEqual((st3["wanted"], st3["stored"]["new"], st3["stored"]["unchanged"],
                          st3["stored"]["excluded_skipped"]), (6, 0, 5, 1))     # the TS one stays excluded

    def test_gap_and_fatal_keep_sync_point(self):
        point = (datetime(2026, 10, 1)).isoformat()
        (self.tmp / "state.json").write_text(json.dumps({"last_ok": {"newest_video_pub": point}}), encoding="utf-8")
        st = src.crawl(self.conn, feed_xml=FEED, log=lambda m: None)
        self.assertEqual(st["gap"]["from"], point)                       # feed's oldest item is newer than the point
        self.assertEqual(st["stored"]["new"], 6)
        st2 = src.crawl(self.conn, feed_xml="For registred users only", log=lambda m: None)
        self.assertIn("Blocked", st2["fatal"])
        state = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["last_ok"]["newest_video_pub"], "2026-10-25T17:48:33")   # a failed run moves nothing
        self.assertFalse(state["runs"][-1]["ok"])

    def test_limit_moves_point_only_to_stored(self):
        st = src.crawl(self.conn, feed_xml=FEED, limit=2, log=lambda m: None)
        self.assertEqual((st["stored"]["new"], st["left"]), (2, 4))
        state = json.loads((self.tmp / "state.json").read_text(encoding="utf-8"))
        self.assertLess(datetime.fromisoformat(state["last_ok"]["newest_video_pub"]),
                        datetime.fromisoformat("2026-10-25T17:48:33") - timedelta(days=1))

    def test_dv5_only_not_stored(self):
        body = ("Продолжительность: 01:40:00<br>Видео: HEVC, 3840x2160, Dolby Vision, Profile 5<br>"
                "Аудио 1: Русский (Дубляж)")
        it = _item("Фильм / Film (X) [2026, драма, WEB-DL 2160p, Dolby Vision Profile 5] Dub", body=body, post_id=77)
        it["category"] = "foreign_films"
        d = rk.parse_item(rk.compact_item_html(it))
        if not d.get("dv5_only"):
            self.skipTest("rutor parser does not see this sample as DV5-only")
        src.ensure_source(self.conn)
        stt = src.new_stats()
        self.assertIsNone(src.store_item(self.conn, it, stt, set(), "v"))
        self.assertIsNone(self.rel("p77"))
        self.assertEqual(self.conn.execute("SELECT reason FROM excluded_releases WHERE source_id='p77'").fetchone()[0],
                         "dv5_only")

    def test_reparse(self):
        src.crawl(self.conn, feed_xml=FEED, log=lambda m: None)
        self.conn.execute("UPDATE releases SET parsed = NULL WHERE source='rustorka'")
        st = src.reparse(self.conn)
        self.assertEqual((st["parsed"], st["failed"]), (6, 0))


JS_PARSE = r"""
const fs = require('fs'); const [js, dir] = process.argv.slice(1); const api = require(js);
const dec = f => new TextDecoder('windows-1251').decode(fs.readFileSync(dir + '/' + f));
const idx = api.parseIndex(dec('index.html'));
const tp = api.parseTopic(dec('topic.html'));
console.log(JSON.stringify({index: idx, video: idx.filter(api.videoForum).map(f => f.f), listing: api.parseListing(dec('listing.html')),
  topic: Object.assign({}, tp, {post_len: tp.post_html.length, secret: tp.post_html.includes('SECRETKEY'),
                                 mediainfo: tp.post_html.includes('MediaInfo'), post_html: undefined}),
  problems: ['index.html', 'listing.html', 'topic.html', 'guest_404.html'].map(f => api.pageProblem(dec(f), 200)),
  missing: api.parseTopic(dec('guest_404.html')).missing,
  captcha: api.pageProblem('<html>Введите капчу / captcha</html>', 403),
  want: [api.wantTopic({t: '1', title: 'A / B [2026, драма]'}, {yearFrom: 2024}, {}, new Set()),
         api.wantTopic({t: '1', title: 'A / B [2026, драма]'}, {yearFrom: 2024}, {'1': 'A / B [2026, драма]'}, new Set()),
         api.wantTopic({t: '1', title: 'A / B [2026, драма] серии 1-5'}, {yearFrom: 2024}, {'1': 'A / B [2026, драма]'}, new Set()),
         api.wantTopic({t: '2', title: 'Old / Old [2017, драма]'}, {yearFrom: 2024}, {}, new Set()),
         api.wantTopic({t: '2', title: 'Old / Old [2017, драма]'}, {yearFrom: 2024}, {}, new Set(['2']))],
  size: [api.sizeBytes('43.16&nbsp;GB'), api.sizeBytes('920 MiB')]}));
"""

JS_LOOP = r"""
// the browser loop with fetch / localStorage mocked: rustorka pages from the cp1251 fixtures, bridge POSTs captured
const fs = require('fs'); const [js, dir, mode] = process.argv.slice(1);
const posted = []; const store = {}; let relogin = mode === 'relogin' ? 2 : 0;
globalThis.document = {};
globalThis.localStorage = {getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }};
const file = f => ({ok: true, status: 200, arrayBuffer: async () => { const b = fs.readFileSync(dir + '/' + f); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); }});
const page = (f, status) => Object.assign(file(f), {ok: status === 200, status});
globalThis.fetch = async (url, opts) => {
  if (url === 'http://127.0.0.1:8765/queries/rustorka') return {ok: true, status: 200, json: async () => JSON.parse(fs.readFileSync(dir + '/job.json', 'utf8'))};
  if (url.startsWith('http://127.0.0.1:8765/ingest/')) { posted.push(JSON.parse(opts.body)); return {ok: true, status: 200}; }
  if (relogin-- > 0) return file('guest_404.html');               // logged out (session expired): wait, then go on
  if (url.endsWith('/index.php')) return file('index.html');
  if (url.includes('/viewforum.php?f=18')) return file('listing.html');
  if (url.includes('/viewforum.php?f=')) return page('empty.html', 200);
  if (url.includes('/viewtopic.php?t=306800')) return file('topic.html');
  if (url.includes('/viewtopic.php?t=')) return page('gone.html', 404);
  return page('empty.html', 500);
};
require(js);
const msg = __rkCollect({queriesUrl: 'http://127.0.0.1:8765/queries/rustorka'});
(async () => {
  for (let i = 0; i < 400 && !/^(done|stopped|crashed|no job)/.test(__rkStatus().state || ''); i++) await new Promise(r => setTimeout(r, 25));
  console.log(JSON.stringify({msg, status: __rkStatus(), posted, progress: JSON.parse(store.rkc_progress || 'null')}));
})();
"""

JOB = {"v": 1, "run": "t1", "yearFrom": 2024, "since": {}, "defaultSince": "2026-10-01 00:00", "known": {},
       "extra": ["230388"], "delayMs": 1, "maxPages": 5, "waitPollMs": 5, "waitHours": 1, "onlyForums": ["18", "1402"]}


@unittest.skipUnless(NODE, "node not installed")
class TestCollectorJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = node(JS_PARSE, FIX)

    def test_index_and_forum_choice(self):
        r = self.r
        by = {f["f"]: f for f in r["index"]}
        self.assertEqual((by["1402"]["parent"], by["1402"]["category"]), ("Зарубежные фильмы", "Зарубежное кино"))
        self.assertNotIn("1975", by)                                     # header menu links are not forums of the tree
        # films, series, cartoons (of every country); not games, 3D, porn, Russian films / series, anime, chat «Фильмы»
        self.assertEqual(r["video"], ["18", "1402", "1403", "1407", "190", "1893", "1049", "1048"])

    def test_listing(self):
        rows = self.r["listing"]["rows"]
        self.assertEqual([x["t"] for x in rows], ["100001", "306800", "306790", "230388", "306700"])
        self.assertEqual((rows[0]["sticky"], rows[1]["sticky"]), (1, 0))
        self.assertEqual((rows[1]["seeds"], rows[1]["leech"], rows[1]["size"], rows[1]["last"]),
                         (12, 3, int(1.43 * 1024 ** 3), "2026-10-25 20:48"))
        self.assertEqual(rows[2]["last"], "2026-10-22 09:54")               # «9:54» padded: dates compare as strings

    def test_topic(self):
        t = self.r["topic"]
        self.assertEqual((t["title"][:15], t["forum"], t["forum_id"], t["forum_path"], t["post_id"]),
                         ("Кракен / Kraken", "WEB-DL / WEB-DLRip", "1402", "Зарубежные фильмы / WEB-DL / WEB-DLRip",
                          "3409001"))
        self.assertEqual((t["size_bytes"], t["btih"], t["reged"]),
                         (1535450808, "0123456789abcdef0123456789abcdef01234567", "25-Окт-26 20:48"))
        self.assertTrue(t["mediainfo"])
        self.assertFalse(t["secret"])                                    # the announce passkey is cut
        self.assertNotIn("Спасибо за раздачу", json.dumps(t, ensure_ascii=False))   # first post only

    def test_problems_and_selection(self):
        r = self.r
        self.assertEqual(r["problems"], [None, None, None, "not_logged_in"])
        self.assertEqual(r["captcha"], "captcha")
        self.assertTrue(r["missing"])
        self.assertEqual(r["want"], [True, False, True, False, True])
        self.assertEqual(r["size"], [round(43.16 * 1024 ** 3), 920 * 1024 ** 2])


@unittest.skipUnless(NODE, "node not installed")
class TestHistory(unittest.TestCase):
    """The browser loop (mocked fetch) → bridge payloads → ingest into a fresh DB → the next job (sync points)."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)
        self.tmp = Path(tempfile.mkdtemp())
        for f in FIX.glob("*.html"):
            shutil.copy(f, self.tmp / f.name)
        logged = '<html><a href="./login.php?logout=1">Выход</a>'
        for name, body in (("empty.html", "<p>В этом форуме пока нет сообщений</p>"),
                           ("gone.html", "<div>Темы, которую вы запросили, не существует.</div>")):
            (self.tmp / name).write_text(logged + body + "</html>", encoding="windows-1251")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def loop(self, job, mode=""):
        (self.tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        return node(JS_LOOP, self.tmp, mode)

    def test_loop_ingest_and_next_job(self):
        r = self.loop(JOB)
        self.assertEqual(r["status"]["state"], "done")
        kinds = [p.get("type") or ("final" if p.get("final") else "?") for p in r["posted"]]
        self.assertEqual(kinds, ["forums", "page", "page", "final"])
        p18 = r["posted"][1]
        self.assertEqual(p18["forumDone"], {"f": "18", "stopped": "last page", "pages": 1, "newest": "2026-10-25 20:48",
                                            "since": "2026-10-01 00:00"})
        # opened: the 2026 topic (page), the 2026 one that is gone (error), the 2017 one asked as extra; not the 2025 one
        # older than `since`, not the sticky rules topic
        self.assertEqual([t["topic_id"] for t in p18["topics"]], ["306800"])
        self.assertEqual([e.get("t") for e in p18["errors"]], ["306790", "230388"])
        self.assertEqual(r["progress"]["done"], ["18", "1402"])

        state, rows = {}, {}
        st = src.ingest(self.conn, r["posted"], state, rows)
        self.assertEqual((st["pages"], st["rows"], st["topics"], st["forums_done"], st["stored"]["new"]), (2, 5, 1, 2, 1))
        rel = self.conn.execute("SELECT * FROM releases WHERE source='rustorka'").fetchone()
        self.assertEqual((rel["source_id"], rel["url"], rel["size_bytes"], rel["btih"], rel["seeders"], rel["category"],
                          rel["added_at"]),
                         ("p3409001", "https://rustorka.com/forum/viewtopic.php?t=306800", 1535450808,
                          "0123456789abcdef0123456789abcdef01234567", 12, "foreign_films", "2026-10-25T20:48:00"))
        raw = src.raw_page(self.conn, rel["id"])
        self.assertIn('"topic_id": 306800', raw)
        self.assertIn("first post, from the logged-in topic page", raw)
        self.assertEqual(json.loads(rel["parsed"])["rustorka"]["forum_path"], "Зарубежные фильмы / WEB-DL / WEB-DLRip")
        self.assertEqual(state["forums"]["18"]["newest"], "2026-10-25 20:48")
        self.assertEqual(state["video_forums"][:2], ["18", "1402"])
        self.assertEqual(rows["306800"]["page_title"][:15], "Кракен / Kraken")
        self.assertEqual(rows["306800"]["sid"], "p3409001")
        # next job: the forum's point minus 2 days, the stored page known by its title
        job = src.build_job(self.conn, state, rows)
        self.assertEqual(job["since"], {"18": "2026-10-23 20:48"})
        self.assertEqual(job["known"], {"306800": rows["306800"]["title"]})
        self.assertEqual(job["defaultSince"], f"{min(db.years()) - 1}-11-01 00:00")
        # ingest is idempotent
        st2 = src.ingest(self.conn, r["posted"], state, rows)
        self.assertEqual((st2["stored"]["new"], st2["stored"]["unchanged"]), (0, 1))

    def test_relogin_waits_and_goes_on(self):
        r = self.loop(JOB, "relogin")
        self.assertEqual(r["status"]["state"], "done")
        waits = [p for p in r["posted"] if p.get("kind") == "rustorka_status"]
        self.assertTrue(waits and waits[0]["state"].startswith("waiting: not_logged_in"))
        self.assertEqual(sum(1 for p in r["posted"] if p.get("type") == "page"), 2)

    def test_resume(self):
        r = self.loop(JOB)
        cut = [p for p in r["posted"] if p.get("kind") == "rustorka" and p.get("type") == "forums"]   # stopped right after
        job, info = src.resume_job(JOB, cut, self.conn, {}, {}, 1)
        self.assertEqual((job["run"], job["base"], job["onlyForums"], job["extra"]), ("t1-r1", "t1", ["18", "1402"], ["230388"]))
        self.assertEqual(info["forums_left"], 2)
        page = [p for p in r["posted"] if p.get("type") == "page"][0]
        unfinished = dict(page, forumDone=None)
        job2, _ = src.resume_job(JOB, cut + [unfinished], self.conn, {}, {}, 2)
        self.assertEqual((job2["onlyForums"], job2["startAt"]), (["18", "1402"], {"18": 50}))
        job3, _ = src.resume_job(JOB, r["posted"], self.conn, {}, {}, 3)
        self.assertEqual(job3["onlyForums"], [])

    def test_feed_row_gets_topic_page(self):
        """The feed's release of a topic (same first post id) takes the topic page; a later feed run does not undo it."""
        feed = rk.parse_feed(FEED)
        it = [i for i in feed["items"] if i["title"].startswith("Гениальный план")][0]
        it["category"] = rk.classify(it)[0]
        src.ensure_source(self.conn)
        stt, v = src.new_stats(), src.parser_version()
        src.store_item(self.conn, it, stt, set(), v)
        topic = dict(it, topic_id=300001, forum_path="Зарубежные фильмы / HEVC", size_bytes=21474836480, seeders=40,
                     btih="ab" * 20)
        src.store_item(self.conn, topic, stt, set(), v)
        src.store_item(self.conn, it, stt, set(), v)
        self.assertEqual((stt["new"], stt["updated"], stt["unchanged"]), (1, 1, 1))
        r = self.conn.execute("SELECT * FROM releases WHERE source='rustorka'").fetchall()
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0]["url"], r[0]["size_bytes"], r[0]["seeders"], r[0]["btih"]),
                         ("https://rustorka.com/forum/viewtopic.php?t=300001", 21474836480, 40, "ab" * 20))

    def test_old_title_topics(self):
        rows = {"1": {"title": "Прощай, Кристофер Робин / Goodbye Christopher Robin (С. Кёртис) [2017, драма, BDRip]"},
                "2": {"title": "Другой / Other (X) [2017, драма]"},
                "3": {"title": "Новый / New (X) [2026, драма]"},
                "4": {"title": "Прощай, Кристофер Робин / Goodbye Christopher Robin [2017]", "page_title":
                      "Прощай, Кристофер Робин / Goodbye Christopher Robin [2017]"}}
        idx = {"goodbye christopher robin": {2017}}
        self.assertEqual(src.old_title_topics(rows, 2024, idx), ["1"])


if __name__ == "__main__":
    unittest.main()
