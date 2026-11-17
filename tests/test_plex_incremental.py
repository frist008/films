"""films/sources/plex.py — only what changed since the last sync (owner, 2026-11-13: «"пересканирование Plex" целиком чтоли?
зачем целиком? тебе надо только обновления же смотреть», «с последней даты синка»)."""
import gc
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from films import db
from films.sources import plex as P


def ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())


class FakePlex:
    """Two library items; a server history of 3 views (newest first) paged by 1; plex.tv history of 3 nodes paged by 1."""

    def __init__(self, listing):
        self.listing = listing
        self.detail_keys, self.history_pages, self.tv_pages = [], 0, 0
        self.views = [ts("2026-11-12 20:00:00"), ts("2026-11-01 10:00:00"), ts("2026-06-01 10:00:00")]
        self.tv = ["2026-11-12T20:00:00Z", "2026-11-01T10:00:00Z", "2026-06-01T10:00:00Z"]

    def get(self, path, params=None):
        params = params or {}
        if path == "/library/sections":
            return {"MediaContainer": {"Directory": [{"key": "1", "title": "Фильмы", "agent": "tv.plex.agents.movie"}]}}
        if path.startswith("/library/sections/1/all"):
            return {"MediaContainer": {"Metadata": self.listing, "totalSize": len(self.listing)}}
        if path.startswith("/library/metadata/"):
            keys = path.rsplit("/", 1)[1].split(",")
            self.detail_keys += keys
            return {"MediaContainer": {"Metadata": [dict(m, Guid=[{"id": "imdb://tt1"}]) for m in self.listing
                                                    if m["ratingKey"] in keys]}}
        if path == "/status/sessions/history/all":
            i = params["X-Plex-Container-Start"]
            self.history_pages += 1
            md = [{"historyKey": f"/status/sessions/history/{i + 1}", "title": f"v{i}", "viewedAt": self.views[i],
                   "accountID": 1, "type": "movie"}] if i < len(self.views) else []
            return {"MediaContainer": {"Metadata": md, "totalSize": len(self.views)}}
        if path.endswith("/api/v2/user"):
            return {"username": P.OWNER_USERNAME, "uuid": "u"}
        raise AssertionError(path)

    def graphql(self, query, variables, op):
        if op == "GetUserStats":
            return {"userByUsername": {"watchStats": {}}}
        i = int(variables["after"] or 0)
        self.tv_pages += 1
        node = {"id": f"a{i}", "date": self.tv[i], "metadataItem": {"id": f"m{i}", "type": "MOVIE", "title": f"t{i}"}}
        return {"user": {"watchHistory": {"nodes": [node], "pageInfo": {"hasNextPage": i + 1 < len(self.tv),
                                                                         "endCursor": str(i + 1)}}}}


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.conn = db.connect(Path(d.name) / "t.db")
        db.migrate(self.conn)
        self.addCleanup(lambda: (self.conn.close(), gc.collect()))
        for p in (mock.patch.object(P, "_read_pms_db", lambda acc: None),
                  mock.patch.object(P, "_resolve_plex_guids", lambda px, g: {}),
                  mock.patch.object(P.personal, "names", lambda: set())):
            p.start()
            self.addCleanup(p.stop)

    def listing(self, updated_b):
        return [{"ratingKey": "10", "title": "A", "guid": "plex://movie/a", "updatedAt": ts("2026-10-01 00:00:00"),
                 "viewCount": 1, "lastViewedAt": ts("2026-10-02 00:00:00")},
                {"ratingKey": "11", "title": "B", "guid": "plex://movie/b", "updatedAt": updated_b}]

    def test_library_rereads_only_new_and_changed(self):
        px = FakePlex(self.listing(ts("2026-10-01 00:00:00")))
        first = P.scan_libraries(self.conn, px, log=lambda *a: None)
        self.assertEqual(sorted(px.detail_keys), ["10", "11"])         # nothing known yet → everything
        self.assertEqual(first["unchanged"], 0)
        # B got a new file after the last scan, A untouched; C is new
        listing = self.listing(ts("2099-01-01 00:00:00")) + [{"ratingKey": "12", "title": "C", "guid": "plex://movie/c",
                                                             "updatedAt": ts("2026-10-01 00:00:00")}]
        px = FakePlex(listing)
        second = P.scan_libraries(self.conn, px, log=lambda *a: None)
        self.assertEqual(sorted(px.detail_keys), ["11", "12"])
        self.assertEqual((second["unchanged"], second["items"]), (1, 3))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM plex_items WHERE present = 1").fetchone()[0], 3)
        # a new view of A is a change too
        listing[0]["viewCount"] = 2
        px = FakePlex(listing)
        P.scan_libraries(self.conn, px, log=lambda *a: None)
        self.assertIn("10", px.detail_keys)
        # full=True reads every item
        px = FakePlex(listing)
        P.scan_libraries(self.conn, px, log=lambda *a: None, full=True)
        self.assertEqual(sorted(set(px.detail_keys)), ["10", "11", "12"])

    def test_history_only_since_last_sync(self):
        self.conn.executemany("INSERT INTO plex_history (source, plex_guid, guids, kind, title, viewed_at) VALUES "
                              "(?, 'g', '[]', 'movie', 't', ?)",
                              [("library", "2026-11-03 00:00:00"), ("plextv", "2026-11-03 00:00:00")])
        self.conn.commit()
        px = FakePlex([])
        st = P.fetch_history(self.conn, px, log=lambda *a: None)
        self.assertEqual(st["since_library"], "2026-11-01 00:00:00")      # last view minus 2 days
        self.assertEqual(st["api_rows"], 2)                              # 11-12 and 11-01 (2-day margin), not 06-01
        self.assertEqual(px.history_pages, 3)                            # stopped at the first older page
        self.assertEqual(st["plextv_nodes"], 2)
        self.assertEqual(px.tv_pages, 3)
        cur = dict(self.conn.execute("SELECT source, cursor FROM sync_state").fetchall())
        self.assertEqual(cur["plex_history"], "2026-11-12 20:00:00")
        self.assertEqual(cur["plextv_history"], "2026-11-12 20:00:00")
        px = FakePlex([])
        st = P.fetch_history(self.conn, px, log=lambda *a: None, full=True)
        self.assertEqual((st["api_rows"], st["plextv_nodes"]), (3, 3))


if __name__ == "__main__":
    unittest.main()
