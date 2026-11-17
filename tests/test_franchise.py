"""Tests for films/franchise.py (offline: no network, no data/films.db).

Covers the pure helpers: wikitext section / link extraction (en / ru / uk, interlanguage templates, navboxes),
fandom category filter, title cleaning, kind and confidence rules, candidate merging, the class classifier
(with the class closure stubbed) and the franchise_members upsert / stale marking on an in-memory database.
Run:  python -X utf8 -m unittest tests.test_franchise
"""
import json
import sqlite3
import unittest

from films import db
from films import franchise as F


EN_ARTICLE = """Intro mentions [[Keanu Reeves]] and [[The Matrix]].
== Films ==
{{Main|John Wick (film)}}
=== ''[[John Wick: Chapter 2]]'' (2017) ===
Reeves filmed it after [[The Matrix Resurrections]].
* [[Ballerina (2025 film)|Ballerina]]
=== Other potential projects ===
* [[Nobody (2021 film)|Nobody]] crossover
== Television ==
{| class="wikitable"
| ''[[The Continental: From the World of John Wick]]'' || 2023
|}
== Video games ==
* [[John Wick Hex]]
== Cast and characters ==
| [[Ian McShane]]
== See also ==
* [[Atomic Blonde]]
"""

RU_ARTICLE = """== Фильмы ==
=== Спин-оффы ===
* [[Балерина (фильм, 2025)|Балерина]]
* {{Нп3|Каин (фильм)|Каин||Caine (film)}}
== Видеоигры ==
* [[John Wick Hex]]
== Сериалы ==
* [[Континенталь (мини-сериал)]]
"""


class TestWikitext(unittest.TestCase):
    def test_sections_en(self):
        secs = F.film_sections(EN_ARTICLE, "en")
        paths = [p for p, _ in secs]
        self.assertIn("Films", paths)
        self.assertIn("Television", paths)
        self.assertNotIn("Video games", paths)
        self.assertNotIn("Cast and characters", paths)
        self.assertNotIn("See also", paths)
        self.assertNotIn("Films / Other potential projects", paths)

    def test_list_lines_drop_prose(self):
        links = []
        for _, body in F.film_sections(EN_ARTICLE, "en"):
            links += [t for _, t in F.wikitext_links(F.list_lines(body), "en")]
        self.assertIn("John Wick (film)", links)            # {{Main}}
        self.assertIn("John Wick: Chapter 2", links)        # heading
        self.assertIn("Ballerina (2025 film)", links)       # bullet
        self.assertIn("The Continental: From the World of John Wick", links)   # table row
        self.assertNotIn("The Matrix Resurrections", links)  # prose
        self.assertNotIn("Nobody (2021 film)", links)        # «potential projects»
        self.assertNotIn("John Wick Hex", links)             # games section
        self.assertNotIn("Keanu Reeves", links)              # lead

    def test_sections_ru_and_interlanguage(self):
        links = []
        for _, body in F.film_sections(RU_ARTICLE, "ru"):
            links += F.wikitext_links(F.list_lines(body), "ru")
        self.assertIn(("ru", "Балерина (фильм, 2025)"), links)
        self.assertIn(("ru", "Каин (фильм)"), links)
        self.assertIn(("en", "Caine (film)"), links)          # untranslated → the English article
        self.assertIn(("ru", "Континенталь (мини-сериал)"), links)
        self.assertNotIn(("ru", "John Wick Hex"), links)

    def test_whole_page_keeps_lead_drops_tail(self):
        paths = [p for p, _ in F.film_sections(EN_ARTICLE, "en", whole=True)]
        self.assertIn("(lead)", paths)
        self.assertIn("Video games", paths)
        self.assertNotIn("See also", paths)

    def test_links_skip_namespaces_and_lang_prefix(self):
        links = F.wikitext_links("[[File:x.jpg]] [[Category:Films]] [[:en:Alien (film)|Alien]] [[alien 3#Plot|x]]",
                                 "ru")
        self.assertEqual(links, [("en", "Alien (film)"), ("ru", "Alien 3")])

    def test_navbox_skips_game_groups(self):
        nav = """{{Navbox
| title = [[John Wick]]
| group1 = Films
| list1 = * [[John Wick (film)|John Wick]] * [[Ballerina (2025 film)|Ballerina]]
| group2 = Video games
| list2 = * [[John Wick Hex]]
| group3 = Television series
| list3 = * [[The Continental: From the World of John Wick]]
}}"""
        got = [(t, g) for _, t, g in F.navbox_links(nav, "en")]
        self.assertIn(("John Wick (film)", "Films"), got)
        self.assertIn(("The Continental: From the World of John Wick", "Television series"), got)
        self.assertNotIn("John Wick Hex", [t for t, _ in got])


class TestFandomAndTitles(unittest.TestCase):
    def test_fandom_categories(self):
        for ok in ("Films", "Movies", "TV Series", "Television series", "Live-action films", "Animated series",
                   "Alien films", "Disney+ series", "Upcoming Movies", "Short films", "One-Shots"):
            self.assertTrue(F.fandom_category_ok(ok), ok)
        for bad in ("Fictional films", "Film characters", "Fantastic Four (2005) crew", "Episodes", "Video games",
                    "Canceled films", "Film soundtracks", "Images from Star Wars films", "LEGO films",
                    "Film novelizations", "Documentaries", "Non-Terminator films", "Screenshots of films"):
            self.assertFalse(F.fandom_category_ok(bad), bad)

    def test_studio_catalogue_seeds(self):
        for bad in ("Walt Disney Animation Studios feature film", "Studio Ghibli Feature Films", "Pixar universe",
                    "DreamWorks universe", "Warner Bros. Animation film universe"):
            self.assertTrue(F.STUDIO_CATALOGUE.search(bad), bad)
        for ok in ("Kung Fu Panda universe", "Marvel Cinematic Universe", "Star Wars", "Toy Story"):
            self.assertFalse(F.STUDIO_CATALOGUE.search(ok), ok)

    def test_clean_work_title(self):
        self.assertEqual(F.clean_work_title("Halloween (2007 film)"), ("Halloween", 2007, "movie"))
        self.assertEqual(F.clean_work_title("Daredevil (TV series)"), ("Daredevil", None, "tv"))
        self.assertEqual(F.clean_work_title("Dune (2021)"), ("Dune", 2021, None))
        self.assertEqual(F.clean_work_title("Mercury (planet)"), ("Mercury (planet)", None, None))
        self.assertEqual(F.clean_work_title("John Wick (film series)")[2], "collection")
        self.assertEqual(F.clean_work_title("Джон Уик (фильм)"), ("Джон Уик", None, "movie"))

    def test_norm_title(self):
        self.assertEqual(F.norm_title("The Lord of the Rings: The Fellowship of the Ring"),
                         F.norm_title("Lord of the Rings — The Fellowship of the Ring"))
        self.assertEqual(F.norm_title("Ёлки"), F.norm_title("елки"))


class TestRules(unittest.TestCase):
    def test_container_work(self):
        # 2026-10-25 audit: walked containers that are works of their own become members
        roze = {"P31": ["Q29982285"], "P345": ["tt13592980"], "P4983": ["255107"], "P8345": ["Q207981"]}
        self.assertEqual(F.container_work(roze, {"animated", "series"}), "screen")
        sinners = {"P31": ["Q13593818"], "P345": ["tt10443844"], "P4947": ["510242"]}
        self.assertEqual(F.container_work(sinners, set()), "ids")
        self.assertIsNone(F.container_work({"P31": ["Q24856"], "P4947": ["1"]}, set()))      # no IMDb id
        self.assertIsNone(F.container_work({"P31": ["Q196600"], "P345": ["tt1"]}, set()))    # IMDb only, no TMDB
        self.assertIsNone(F.container_work({"P31": ["Q196600"]}, set()))                     # a franchise item

    def test_kind_of(self):
        self.assertEqual(F.kind_of({"film"}, set(), "movie", set(), 120), "film")
        self.assertEqual(F.kind_of({"film"}, {"Q202866"}, "movie", set(), 90), "animated film")
        self.assertEqual(F.kind_of({"film"}, set(), "movie", {16}, 90), "animated film")
        self.assertEqual(F.kind_of({"series"}, set(), "tv", {16}, None), "animated series")
        self.assertEqual(F.kind_of({"series"}, set(), "tv", set(), None), "series")
        self.assertEqual(F.kind_of({"special"}, set(), "tv", set(), None), "special")
        self.assertEqual(F.kind_of({"film"}, set(), "movie", set(), 12), "short")
        self.assertEqual(F.kind_of({"film", "documentary"}, set(), "movie", {99}, 95), "documentary")
        self.assertEqual(F.kind_of(set(), set(), "tv", set(), None), "series")     # TMDB-only
        self.assertEqual(F.kind_of({"tvprog"}, set(), "movie", set(), 100), "film")

    def test_confidence(self):
        c = F.Cand(key="tt1", sources={"wikidata": [], "wikipedia:en": []})
        self.assertEqual(F.confidence(c), "confirmed")
        c = F.Cand(key="tt1", sources={"wikipedia:en": [], "wikipedia:ru": [], "wikipedia:uk": []})
        self.assertEqual(F.confidence(c), "single_source")           # three languages = one family
        c = F.Cand(key="tt1", sources={"fandom:a": [], "fandom:b": []})
        self.assertEqual(F.confidence(c), "single_source")
        c = F.Cand(key="tt1", sources={"wikidata": [], "fandom:a": []}, flags={"imprint"})
        self.assertEqual(F.confidence(c), "review")

    def test_classify_with_stubbed_closure(self):
        orig = F.class_roots
        F.class_roots = lambda classes: {c: {"Q100": ["Q11424", "Q202866"], "Q200": ["Q21191270", "Q15416"],
                                             "Q300": ["Q24856"], "Q400": []}.get(c, []) for c in classes}
        try:
            c = F.classify(["Q100"])
            self.assertEqual(c["screen"], {"film", "animated"})
            self.assertFalse(c["exclude"])
            c = F.classify(["Q200"])
            self.assertEqual(c["exclude"], {"episode"})
            self.assertTrue(F.classify(["Q300"])["series_like"])
            self.assertEqual(F.classify(["Q400"])["screen"], set())
            self.assertEqual(F.classify(["Q5398426"])["screen"], {"series"})    # a root itself
        finally:
            F.class_roots = orig


class TestCandidates(unittest.TestCase):
    def test_imdb_only_candidate_folds_into_qid(self):
        r = F.Resolver({"key": "x", "name": "X"}, conn=None, log=lambda *_: None)
        r.add(imdb="tt7181546", source="fandom:johnwick", evidence="Movies: Ballerina", flags=("_title_matched",))
        r.add(qid="Q84713105", source="wikidata", evidence="P8345", strength=F.STRONG)
        r._get(qid="Q84713105", imdb="tt7181546")
        self.assertEqual(list(r.cands), ["Q84713105"])
        c = r.cands["Q84713105"]
        self.assertEqual(c.imdb, "tt7181546")
        self.assertEqual(set(c.sources), {"fandom:johnwick", "wikidata"})

    def test_rekey_imdb_candidate(self):
        r = F.Resolver({"key": "x", "name": "X"}, conn=None, log=lambda *_: None)
        r.add(imdb="tt1", source="tmdb_collection", evidence="c")
        r._get(qid="Q1", imdb="tt1")
        self.assertEqual(list(r.cands), ["Q1"])


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.migrate(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def member(imdb, title, conf="confirmed"):
        return {"imdb_id": imdb, "qid": "Q1", "tmdb_id": 1, "tmdb_type": "movie", "kind": "film", "title": title,
                "title_ru": None, "year": 2020, "sources": ["wikidata", "wikipedia:en"], "confidence": conf,
                "flags": []}

    def test_upsert_and_stale(self):
        w = F.write_members(self.conn, "k", [self.member("tt1", "A"), self.member("tt2", "B")])
        self.assertEqual(w, {"written": 2, "stale": 0, "new": 2})
        w = F.write_members(self.conn, "k", [self.member("tt1", "A2")])
        self.assertEqual(w["stale"], 1)
        rows = {r["imdb_id"]: r for r in self.conn.execute("SELECT * FROM franchise_members")}
        self.assertEqual(rows["tt1"]["title"], "A2")
        self.assertEqual(rows["tt2"]["confidence"], "review")             # kept, not deleted
        self.assertTrue(rows["tt2"]["note"].startswith("stale:"))
        self.assertEqual(json.loads(rows["tt1"]["sources"]), ["wikidata", "wikipedia:en"])
        F.write_members(self.conn, "k", [self.member("tt1", "A2")])
        note = self.conn.execute("SELECT note FROM franchise_members WHERE imdb_id='tt2'").fetchone()[0]
        self.assertEqual(note.count("stale:"), 1)                         # not prefixed twice

    def test_hand_added_never_stale(self):
        # a special added by an owner's rule / by hand is not resolve's to find (One Piece specials, 2026-10-25)
        m = dict(self.member("tt3", "Special"), sources=["owner_rule_2026-10-24"])
        F.write_members(self.conn, "k", [self.member("tt1", "A"), m])
        w = F.write_members(self.conn, "k", [self.member("tt1", "A")])
        self.assertEqual(w["stale"], 0)
        row = self.conn.execute("SELECT confidence, note FROM franchise_members WHERE imdb_id='tt3'").fetchone()
        self.assertEqual(row["confidence"], "confirmed")
        self.assertFalse((row["note"] or "").startswith("stale:"))


class TestHttpHelpers(unittest.TestCase):
    def test_cache_key_stable(self):
        a = F.cache_key("https://x/api.php", {"b": 1, "a": 2}, False)
        b = F.cache_key("https://x/api.php", {"a": 2, "b": 1}, False)
        self.assertEqual(a, b)
        self.assertNotEqual(a, F.cache_key("https://x/api.php", {"a": 2, "b": 1}, True))

    def test_buckets(self):
        self.assertEqual(F._bucket("en.wikipedia.org")[0], "wikimedia")
        self.assertEqual(F._bucket("www.wikidata.org")[0], "wikimedia")
        self.assertEqual(F._bucket("query.wikidata.org")[0], "query.wikidata.org")
        self.assertEqual(F._bucket("johnwick.fandom.com")[0], "johnwick.fandom.com")

    def test_fandom_api(self):
        self.assertEqual(F.fandom_api("harrypotter"), "https://harrypotter.fandom.com/api.php")
        self.assertEqual(F.fandom_api("ru.marvel"), "https://marvel.fandom.com/ru/api.php")


if __name__ == "__main__":
    unittest.main()
