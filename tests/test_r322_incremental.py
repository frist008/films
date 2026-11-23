"""R322 (owner 2026-11-13): «при обновлении данных мы ищем только всё новое для новых дат и новых фильмов и не трогаем
ничего старого на что материал уже найден … отталкиваемся везде от даты последнего синка». Offline guards that the
update steps do not re-process old material by default and do process new material (B2–B6; B1 — RT —
tests/test_ratings_recheck.py), and that each keeps its explicit opt-in for the owner's request.
Run:  python -X utf8 -m unittest tests.test_r322_incremental
"""
import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from films import db

OLD = "2026-01-01 00:00:00"


def mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    db.register_functions(conn)
    return conn


def set_last_sync(c, when="datetime('now', '-1 days')"):
    """The last sync a day ago → db.last_update_since = 3 days ago (2-day margin)."""
    c.execute(f"INSERT OR REPLACE INTO sync_state (source, last_run_at, cursor) VALUES ('youtube_trailers', {when}, {when})")
    c.commit()


class LastUpdateSince(unittest.TestCase):
    def test_reference(self):
        c = mem_db()
        self.assertIsNone(db.last_update_since(c))   # never synced → everything is new
        c.execute("INSERT INTO sync_state (source, cursor) VALUES ('youtube_trailers', '2026-10-29 09:56:37')")
        self.assertEqual(db.last_update_since(c), "2026-10-27 09:56:37")
        c.execute("INSERT INTO runs (kind, started_at, finished_at) VALUES ('update_db', '2026-11-01', '2026-11-05 10:00:00')")
        self.assertEqual(db.last_update_since(c), "2026-11-03 10:00:00")   # the later of the two
        c.execute("INSERT INTO runs (kind, started_at) VALUES ('update_db', '2026-11-12')")   # still open: not a sync
        self.assertEqual(db.last_update_since(c), "2026-11-03 10:00:00")


class ImdbRatingOnlyNewAndRecent(unittest.TestCase):
    """R330 (2026-11-14): «Я просил запоминать рейтинг и когда тайтл старее с момента релиза 3 месяцев больше его не
    обновлять. Тут нарушение скила» — `ratings imdb` rewrote the rating (and ratings_checked_at) of all 12 404 titles."""

    def test_old_released_title_keeps_its_rating(self):
        from films import ratings
        c = mem_db()
        c.execute("INSERT INTO sync_state (source, cursor) VALUES ('youtube_trailers', '2026-10-29 09:56:37')")
        recent = (date.today() - timedelta(days=10)).isoformat()
        c.executemany("INSERT INTO titles (id, kind, title_original, imdb_id, imdb_rating, imdb_votes, release_date, created_at, "
                      "ratings_checked_at) VALUES (?, 'movie', ?, ?, ?, 100, ?, ?, ?)",
                      [(1, "Old", "tt1", 6.0, "2020-01-01", OLD, OLD),            # old, rated → untouched
                       (2, "Recent", "tt2", 6.0, recent, OLD, OLD),              # released 10 days ago → refreshed
                       (3, "NewSinceSync", "tt3", 6.0, "2020-01-01", "2026-11-13 10:00:00", OLD),
                       (4, "NoRating", "tt4", None, "2020-01-01", OLD, OLD)])
        c.executemany("INSERT INTO imdb_ratings (tconst, rating, votes) VALUES (?, 8.0, 999)",
                      [("tt1",), ("tt2",), ("tt3",), ("tt4",)])
        with mock.patch.object(ratings, "_tsv", lambda name: iter(())):
            ratings.imdb_ratings(c)
        got = {r[0]: (r[1], r[2] != OLD) for r in c.execute("SELECT id, imdb_rating, ratings_checked_at FROM titles")}
        self.assertEqual(got, {1: (6.0, False), 2: (8.0, True), 3: (8.0, True), 4: (8.0, True)})
        with mock.patch.object(ratings, "_tsv", lambda name: iter(())):
            ratings.imdb_ratings(c, all_titles=True)   # only on the owner's request
        self.assertEqual(c.execute("SELECT imdb_rating FROM titles WHERE id = 1").fetchone()[0], 8.0)


class TrailerAuditOnlyNew(unittest.TestCase):
    """R322 (2026-11-13): the trailer audit re-watched every visible trailer on each run."""

    def test_new_ids(self):
        from films import trailer_audit
        c = mem_db()
        self.assertIsNone(trailer_audit.new_ids(c))   # never synced → all
        c.executemany("INSERT INTO titles (id, kind, title_original, created_at, trailer_checked_at) VALUES (?, 'movie', ?, ?, ?)",
                      [(1, "Old", OLD, OLD), (2, "NewTitle", "2026-11-13 10:00:00", None),
                       (3, "OldNewTrailer", OLD, "2026-11-13 10:00:00")])
        c.execute("INSERT INTO sync_state (source, cursor) VALUES ('youtube_trailers', '2026-10-29 09:56:37')")
        self.assertEqual(trailer_audit.new_ids(c), {2, 3})

    def test_no_margin_for_own_marks(self):
        """R326 (2026-11-13, «Так можно ты исправишь?»): a trailer checked on the last day of the previous run (inside the
        2-day margin) is not audited again — our own marks count from the sync point itself."""
        from films import trailer_audit
        from films.sources import trailer_lang
        c = mem_db()
        c.executemany("INSERT INTO titles (id, kind, title_original, created_at, trailer_checked_at) VALUES (?, 'movie', ?, ?, ?)",
                      [(1, "LastDayOfPrevRun", "2026-10-28 20:00:00", "2026-10-28 21:00:00"),
                       (2, "AfterSync", "2026-10-30 10:00:00", None)])
        c.execute("INSERT INTO sync_state (source, cursor) VALUES ('youtube_trailers', '2026-10-29 09:56:37')")
        self.assertEqual(trailer_audit.new_ids(c), {2})
        src = (db.ROOT / "films" / "sources" / "trailer_lang.py").read_text(encoding="utf-8")
        self.assertNotIn("yt_mod.new_since(conn)", src)   # unconfirmed / fallbacks / originals: margin 0 too
        self.assertEqual(src.count("last_update_since(conn, margin_days=0)"), 3)
        del trailer_lang


# ---------------------------------------------------------------------------------------------- B2 IMDb specs

class ImdbSpecsNoRefetch(unittest.TestCase):
    def setUp(self):
        from films.sources import imdb_specs as m
        self.m = m
        self.tmp = Path(tempfile.mkdtemp(prefix="r322_specs_"))
        self.patch = mock.patch.object(m, "CACHE_DIR", self.tmp)
        self.patch.start()
        self.calls = []

        def post(ids, log=print, query=None):
            self.calls.append(sorted(ids))
            return {i: {"titleText": {"text": i}, "distributors": {"edges": []}} for i in ids}
        self.post = mock.patch.object(m, "_post", post)
        self.post.start()

    def tearDown(self):
        self.post.stop()
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, t, days_old, node=None):
        rec = {"tconst": t, "fetched_at": OLD, "status": "ok", "canonical": None,
               "node": node or {"titleText": {"text": t}, "distributors": {"edges": []}}}
        self.m._save(rec)
        old = time.time() - days_old * 86400
        os.utime(self.m._cache_path(t), (old, old))

    def test_old_cache_is_not_fetched_again(self):
        self.put("tt0000001", 60)                               # an old title cached 60 days ago → kept
        self.put("tt0000002", 60)                               # released in the last 90 days → fetched again
        self.put("tt0000004", 60, {"titleText": {"text": "x"}})  # cached before `distributors` joined QUERY → fetched
        ids = ["tt0000001", "tt0000002", "tt0000003", "tt0000004"]  # tt…3 never cached (a new title) → fetched
        recs, failures = self.m.ensure(ids, log=lambda *_: None, recent={"tt0000002"})
        self.assertEqual(self.calls, [["tt0000002", "tt0000003", "tt0000004"]])
        self.assertEqual(set(recs), set(ids))
        self.calls.clear()
        self.m.ensure(ids, log=lambda *_: None, recent={"tt0000002"})   # title_meta's `imdb` pass right after
        self.assertEqual(self.calls, [])                                # → no double fetch
        self.m.ensure(["tt0000001"], 0, log=lambda *_: None)            # the owner's --max-age-days 0
        self.assertEqual(self.calls, [["tt0000001"]])

    def test_targets_mark_recent_titles(self):
        c = mem_db()
        today = date.today()
        c.execute("INSERT INTO titles (id, kind, imdb_id, year, release_date) VALUES (1, 'movie', 'tt1', 2024, ?), "
                  "(2, 'movie', 'tt2', 2026, ?), (3, 'movie', 'tt3', ?, NULL)",
                  ((today - timedelta(days=400)).isoformat(), (today - timedelta(days=20)).isoformat(), today.year))
        got = {t: rows[0]["is_recent"] for t, rows in self.m._targets(c).items()}
        self.assertEqual(got, {"tt1": 0, "tt2": 1, "tt3": 1})


# ---------------------------------------------------------------------------------------------- B3 enrich

class EnrichFailedNotRetried(unittest.TestCase):
    def test_failed_titles(self):
        from films import enrich
        c = mem_db()
        set_last_sync(c)
        old_rd = (date.today() - timedelta(days=400)).isoformat()
        new_rd = (date.today() - timedelta(days=10)).isoformat()
        c.executemany("INSERT INTO titles (id, kind, imdb_id, year, release_date, created_at) VALUES (?, 'movie', ?, ?, ?, ?)", [
            (1, "tt1", 2024, old_rd, OLD),                    # old, tried and failed with tt1 → not again
            (2, "tt2new", 2024, old_rd, OLD),                 # old, failed with another id (relinked since) → again
            (3, "tt3", 2024, old_rd, "NEW"),                  # new since the last sync → again
            (4, "tt4", 2026, new_rd, OLD),                    # released 10 days ago → again
            (5, "tt5", 2024, old_rd, OLD),                    # old, never tried → yes
            (6, "tt6", 2024, old_rd, OLD),                    # old 404 issue of before (no key in it) → not again
        ])
        c.execute("UPDATE titles SET created_at = datetime('now') WHERE created_at = 'NEW'")
        for tid, key in ((1, "tt1"), (2, "tt2old"), (3, "tt3"), (4, "tt4")):
            c.execute("INSERT INTO match_issues (problem, candidates, resolved_at) VALUES ('tmdb_no_movie_for_imdb', ?, ?)",
                      (json.dumps({"imdb_id": key, "tmdb_find": {}, "title_id": tid}), OLD if tid == 1 else None))
        c.execute("INSERT INTO match_issues (problem, candidates) VALUES ('tmdb_movie_404', ?)",
                  (json.dumps({"tmdb_id": 9, "title_id": 6}),))
        c.commit()
        asked = []

        def find(conn, key):
            asked.append(key)
            return {}
        with mock.patch.object(enrich.tmdb, "find_by_imdb", find):
            st = enrich.enrich(c, search=False)
            self.assertEqual(sorted(asked), ["tt2new", "tt3", "tt4", "tt5"])
            self.assertEqual(st["titles"], 4)
            asked.clear()
            enrich.enrich(c, search=False)        # the next run: tt5 failed now too, but it is not new → not again
            self.assertEqual(sorted(asked), ["tt3", "tt4"])
            asked.clear()
            enrich.enrich(c, search=False, retry_failed=True)   # the owner's --retry-failed
            self.assertEqual(sorted(asked), ["tt1", "tt2new", "tt3", "tt4", "tt5", "tt6"])
        # the issue stays for the owner; tmdb_checked_at is not set (triage would close the issue as «obsolete»)
        self.assertIsNone(c.execute("SELECT tmdb_checked_at FROM titles WHERE id = 5").fetchone()[0])
        self.assertEqual(c.execute("SELECT count(*) FROM match_issues WHERE json_extract(candidates, '$.title_id') = 5 "
                                   "AND resolved_at IS NULL").fetchone()[0], 1)


# ---------------------------------------------------------------------------------------------- B4 posters

class PostersOnlyNew(unittest.TestCase):
    def test_targets(self):
        from films import posters
        c = mem_db()
        set_last_sync(c)
        c.executemany("INSERT INTO titles (id, kind, title_original, year, created_at) VALUES (?, 'movie', ?, 2024, ?)",
                      [(1, "Old", OLD), (2, "OldNewRelease", OLD), (3, "New", OLD)])
        c.execute("UPDATE titles SET created_at = datetime('now') WHERE id = 3")
        for rid, tid, seen in ((1, 1, OLD), (2, 2, OLD), (3, 2, None), (4, 3, None)):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, category, first_seen_at) "
                      "VALUES (?, 'rutor', ?, 'u', 'n', 'movie', coalesce(?, datetime('now')))", (rid, str(rid), seen))
            c.execute("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, ?, 'imdb_page')", (rid, tid))
        c.commit()
        self.assertEqual([r["id"] for r in posters.targets(c)], [2, 3])          # old title 1 is not touched
        self.assertEqual([r["id"] for r in posters.targets(c, all_titles=True)], [1, 2, 3])   # the owner's --all
        self.assertEqual([r["id"] for r in posters.targets(c, ids=[1])], [1])    # named ids — always
        checked = []
        st = posters.fill(c, alive=lambda u: checked.append(u) or False, fetch_imdb=False, log=lambda *_: None)
        self.assertEqual(st["targets"], 2)


# ---------------------------------------------------------------------------------------------- B5 trailers

class TrailerLangOnlyNewShownFilms(unittest.TestCase):
    def setUp(self):
        self.c = mem_db()
        set_last_sync(self.c)
        rows = [  # id, kind, created old?, trailer_ru_url, trailer_ru_check, trailer_checked_at, trailer_ru_checked_at
            (1, "movie", True, "https://www.youtube.com/watch?v=a", "unknown", OLD, "2026-02-01 00:00:00"),  # checked → no
            (2, "movie", False, "https://www.youtube.com/watch?v=b", None, None, None),       # new, never checked → yes
            (3, "series", False, "https://www.youtube.com/watch?v=c", None, None, None),      # a series → no (R325)
            (4, "movie", False, "https://www.youtube.com/watch?v=d", None, None, None),       # new but not shown → no
            (5, "movie", True, "https://www.youtube.com/watch?v=e", None, None, None),        # old never-checked → no
            (6, "movie", False, "https://www.youtube.com/watch?v=f", "unknown", "now", "now"),  # checked this run → no
        ]
        for tid, kind, old, url, chk, tca, rca in rows:
            self.c.execute(
                "INSERT INTO titles (id, kind, title_original, year, created_at, trailer_ru_url, trailer_ru_check,"
                " trailer_checked_at, trailer_ru_checked_at) VALUES (?, ?, ?, 2025, " + ("?" if old else "datetime('now')")
                + ", ?, ?, " + ("datetime('now')" if tca == "now" else "?") + ", " + ("datetime('now')" if rca == "now" else "?") + ")",
                [tid, kind, f"T{tid}"] + ([OLD] if old else []) + [url, chk] + ([] if tca == "now" else [tca])
                + ([] if rca == "now" else [rca]))
        self.c.commit()
        self.shown = {1: 0, 2: 1, 3: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7}
        self.vis = mock.patch("films.playlist_notes.visible_order", lambda: self.shown)
        self.vis.start()

    def tearDown(self):
        self.vis.stop()

    def test_unconfirmed(self):
        from films.sources import trailer_lang as tl
        self.assertEqual(tl.unconfirmed_titles(self.c), [2])
        self.assertEqual(sorted(tl.unconfirmed_titles(self.c, all_titles=True)), [1, 2, 3, 4, 5, 6])   # owner's --all

    def test_fallbacks(self):
        from films.sources import trailer_lang as tl
        c = self.c
        c.execute("UPDATE titles SET trailer_ru_url = NULL")
        c.execute("INSERT INTO titles (id, kind, title_original, year, created_at) VALUES (7, 'movie', 'OldNoAlt', 2024, ?)",
                  (OLD,))
        c.execute("INSERT INTO titles (id, kind, title_original, year, created_at) VALUES (8, 'movie', 'OldAudited', 2024, ?)",
                  (OLD,))
        c.commit()
        latest = {i: {"uk": [{"id": f"v{i}"}]} for i in range(1, 10)}
        with mock.patch.object(tl.yt_mod, "latest_decisions", lambda: latest), \
                mock.patch.object(tl, "_audit_rejected_ru", lambda: {8}):
            st = tl.fill_fallbacks(c)
        got = {r[0] for r in c.execute("SELECT id FROM titles WHERE trailer_alt_url IS NOT NULL")}
        # new shown films (2, 6 — 6 also checked this run) and the old one whose Russian trailer the audit took away (8);
        # not: old 1, 5, 7 (nothing new), series 3, new 4 not shown
        self.assertEqual(got, {2, 6, 8})
        self.assertEqual(st["titles"], 3)
        with mock.patch.object(tl.yt_mod, "latest_decisions", lambda: latest), \
                mock.patch.object(tl, "_audit_rejected_ru", lambda: set()):
            tl.fill_fallbacks(c, all_titles=True)   # the owner's --all
        self.assertEqual({r[0] for r in c.execute("SELECT id FROM titles WHERE trailer_alt_url IS NOT NULL")},
                         {1, 2, 3, 4, 5, 6, 7, 8})


# ---------------------------------------------------------------------------------------------- B6 match rechecks

class MatchRechecksOnlyNewReleases(unittest.TestCase):
    def setUp(self):
        self.c = c = mem_db()
        set_last_sync(c)
        c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (1, 'movie', 'Film', 2024)")
        for rid, seen in ((1, OLD), (2, None)):
            c.execute("INSERT INTO releases (id, source, source_id, url, name, category, parsed, first_seen_at) "
                      "VALUES (?, 'rutor', ?, 'u', 'Film (2024)', 'movie', ?, coalesce(?, datetime('now')))",
                      (rid, str(rid), db.dump_parsed({"name_parsed": {"episodes": "1-5"}}), seen))
        c.commit()

    def link(self, how):
        self.c.execute("DELETE FROM release_titles")
        self.c.executemany("INSERT INTO release_titles (release_id, title_id, how) VALUES (?, 1, ?)", [(1, how), (2, how)])
        self.c.commit()

    def test_page_links(self):
        from films import match
        self.link("imdb_page")
        since = db.last_update_since(self.c)
        self.assertEqual(match.recheck_page_links(self.c, None, True, lambda *_: None, since=since)["checked"], 1)
        self.assertEqual(match.recheck_page_links(self.c, None, True, lambda *_: None)["checked"], 2)   # --full-recheck

    def test_episode_films(self):
        from films import match
        self.link("title_year")
        since = db.last_update_since(self.c)
        self.assertEqual(match.recheck_episode_films(self.c, dry=True, log=lambda *_: None, since=since)["checked"], 1)
        self.assertEqual(match.recheck_episode_films(self.c, dry=True, log=lambda *_: None)["checked"], 2)

    def test_match_run_passes_the_cursor(self):
        from films import match
        got = {}

        class Stop(Exception):
            pass

        def rec(name):
            def f(conn, dry=False, log=print, since=None):
                got[name] = since
                return {}
            return f
        with mock.patch.object(match, "merge_redirected_titles", lambda *a, **k: {}), \
                mock.patch.object(match, "recheck_series_links", rec("series")), \
                mock.patch.object(match, "recheck_movie_links", rec("movie")), \
                mock.patch.object(match, "recheck_release_data", rec("data")), \
                mock.patch.object(match, "recheck_episode_films", rec("episodes")), \
                mock.patch.object(match, "merge_part_twins", mock.Mock(side_effect=Stop)):
            with self.assertRaises(Stop):
                match._match_releases(self.c)
            self.assertEqual(set(got.values()), {db.last_update_since(self.c)})
            got.clear()
            with self.assertRaises(Stop):
                match._match_releases(self.c, full_recheck=True)
            self.assertEqual(set(got.values()), {None})
            self.assertEqual(len(got), 4)


if __name__ == "__main__":
    unittest.main()
