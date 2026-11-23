"""Points of the sort (owner, 2026-11-23): R341 upgrade 0, R342 franchise 75 seen / 20 unseen, R343 one HDR class,
R344 voice weights and one voice, R345 RT only without «новинка RT», R347 FlarrowFilms +100."""
import json
import sqlite3
import unittest

from films import rank, sheet


def _w():
    return rank.settings()["sort"]["weights"]


class WeightsTest(unittest.TestCase):
    def test_owner_values(self):
        w = _w()
        self.assertEqual(w["upgrade"], 0)                                         # R341
        self.assertEqual((w["franchise_seen"], w["franchise_unseen"]), (75, 20))  # R342
        self.assertNotIn("franchise", w)
        self.assertEqual(w["voice"], {"dub": 30, "mvo": 5, "dvo": 0, "svo": 0, "avo": 0, "amateur": 0})   # R344
        self.assertEqual(w["flarrow"], 100)                                       # R347

    def test_rt_max_below_auto_marks(self):
        """R345: «должно давать меньше балов чем они точно» — RT at 100 % below both «новинка с высоким RT»."""
        w = _w()
        top = rank.rt_points(w, 100, auto_given=False)
        self.assertLess(top, w["auto_maybe"])
        self.assertLess(top, w["auto_want"])

    def test_sheet_constants(self):
        keys = [k for k, _, _ in sheet.CONSTS]
        for k in ("franchise_seen", "franchise_unseen", "flarrow"):
            self.assertIn(k, keys)
        self.assertNotIn("franchise", keys)
        self.assertEqual([h[2:] for h, _ in sheet.MAIN_COLUMNS if h.startswith("f:")], keys)
        self.assertEqual(len(set(label for _, label, _ in sheet.CONSTS)), len(sheet.CONSTS))   # read-back is by label
        # every constant has its value in settings (the «Баллы» tab shows it)
        rows = sheet._score_rows()
        for (k, _, _), row in zip(sheet.CONSTS, rows):
            self.assertIn("userEnteredValue", row[1], k)

    def test_set_weight_new_keys(self):
        text = "[sort.weights]\nfranchise_seen = 75\nfranchise_unseen = 20\nflarrow = 100\n\n[x]\nflarrow = 1\n"
        out = sheet._set_weight(sheet._set_weight(text, "franchise_unseen", 25), "flarrow", 90)
        self.assertIn("franchise_unseen = 25", out)
        self.assertIn("franchise_seen = 75", out)
        self.assertIn("flarrow = 90", out)
        self.assertTrue(out.endswith("[x]\nflarrow = 1\n"))   # other sections untouched


class FranchiseTest(unittest.TestCase):
    def setUp(self):
        c = self.conn = sqlite3.connect(":memory:")
        c.executescript("""
            CREATE TABLE titles (id INTEGER PRIMARY KEY, imdb_id TEXT, series_imdb_id TEXT, collection_id INTEGER);
            CREATE TABLE watched (title_id INTEGER, source TEXT);
            CREATE TABLE franchise_members (franchise_key TEXT, imdb_id TEXT, confidence TEXT);
            INSERT INTO titles VALUES (1, 'tt1', NULL, NULL), (2, NULL, 'tt2', NULL), (3, 'tt3', NULL, 10), (4, 'tt4', NULL, NULL);
            INSERT INTO franchise_members VALUES ('seen_f', 'tt1', 'confirmed'), ('seen_f', 'tt9', 'confirmed'),
                ('partial_f', 'tt2', 'confirmed'), ('review_f', 'tt4', 'review'), ('never_f', 'tt8', 'confirmed');
            INSERT INTO watched VALUES (1, 'plex'), (2, 'plex_partial'), (4, 'manual');
        """)
        self.frs = [{"key": "seen_f"}, {"key": "partial_f"}, {"key": "review_f"}, {"key": "never_f"},
                    {"key": "col_f", "tmdb_collections": [10]}]

    def test_seen_keys(self):
        keys = rank.franchise_seen_keys(self.conn, self.frs, watched_cols={10})
        self.assertIn("seen_f", keys)          # a member watched
        self.assertIn("col_f", keys)           # its TMDB collection watched
        self.assertNotIn("partial_f", keys)    # only a partly seen season — not «видел»
        self.assertNotIn("review_f", keys)     # a «review» member is not a member
        self.assertNotIn("never_f", keys)

    def test_points(self):
        w = _w()
        self.assertEqual(rank.franchise_points(w, True, True), 75)
        self.assertEqual(rank.franchise_points(w, True, False), 20)
        self.assertEqual(rank.franchise_points(w, False, True), 0)


class OneOfTest(unittest.TestCase):
    """R343 / R344: one HDR class and one voice per row, never a sum."""

    def test_hdr_one_class(self):
        w = _w()
        cases = [(["DV", "HDR10"], None, 40, "DV"), (["DV", "HDR10+", "HDR10"], None, 40, "DV"),
                 (["DV", "HDR10"], 1, 40, "DV*"), (["HDR10+", "HDR10"], None, 35, "HDR10"),
                 (["HDR10"], None, 35, "HDR10"), (["HLG"], None, 0, "HLG"), ([], None, 0, "SDR")]
        for hdr, static, pts, cls in cases:
            best = {"hdr": json.dumps(hdr), "dv_static_md": static}
            self.assertEqual(rank.hdr_points(w, best), pts, hdr)       # DV + HDR10 = 40, not 40 + 35 (+ 40)
            self.assertEqual(rank.hdr_points_class(best), cls)
            # the sheet's hidden coefficients: at most one hdr.* is 1
            self.assertLessEqual(sum(int(rank.hdr_points_class(best) == k) for k in w["hdr"]), 1)

    def test_voice_one(self):
        w = _w()
        self.assertEqual(rank.voice_points(w, {"best_voice": "dub"}), 30)
        self.assertEqual(rank.voice_points(w, {"best_voice": "mvo"}), 5)
        for v in ("dvo", "svo", "avo", "amateur", "none", None):
            self.assertEqual(rank.voice_points(w, {"best_voice": v}), 0)


class RtAndFlarrowTest(unittest.TestCase):
    def test_rt_only_without_auto_mark(self):
        w = _w()
        self.assertEqual(rank.rt_points(w, 95, auto_given=True), 0)
        self.assertEqual(rank.rt_points(w, 90, auto_given=False), 12.0)
        self.assertEqual(rank.rt_points(w, 40, auto_given=False), 0)
        self.assertEqual(rank.rt_points(w, None, auto_given=False), 0)

    def test_flarrow(self):
        cfg = rank.settings()
        w = cfg["sort"]["weights"]
        series = {"kind": "series", "tmdb_id": 1, "series_tmdb_id": 1}
        for key in ("marvel", "dc"):   # one [voice_preference] rule for Marvel and DC
            pref = rank.voice_preference(cfg, series, {key})
            fl = {"name": "Вижн | FlarrowFilms", "parsed": {"audio": [{"lang": "ru", "studio": "FlarrowFilms"}]}}
            other = {"name": "Вижн | LostFilm", "parsed": {"audio": [{"lang": "ru", "studio": "LostFilm"}]}}
            self.assertEqual(rank.flarrow_points(w, pref, fl), 100, key)
            self.assertEqual(rank.flarrow_points(w, pref, other), 0)
        # a film of the franchise, a series outside it, Rick and Morty's own studio rule — no FlarrowFilms points
        fl = {"name": "x | FlarrowFilms", "parsed": {"audio": []}}
        self.assertEqual(rank.flarrow_points(w, rank.voice_preference(cfg, {"kind": "movie"}, {"marvel"}), fl), 0)
        self.assertEqual(rank.flarrow_points(w, rank.voice_preference(cfg, series, set()), fl), 0)
        rm = rank.voice_preference(cfg, {"kind": "series", "series_tmdb_id": 60625}, set())
        self.assertEqual(rank.flarrow_points(w, rm, {"name": "x | Кубик в Кубе", "parsed": {"audio": []}}), 0)


if __name__ == "__main__":
    unittest.main()
