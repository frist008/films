"""Owner's sheet review 2026-11-02: weights, DV profile, English names, the folder, «Новый размер» was hidden."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from films import rank, sheet


class WeightsTest(unittest.TestCase):
    def test_removed_constants(self):
        keys = [k for k, _, _ in sheet.CONSTS]
        for gone in ("wishlist", "hdr.HLG", "hdr.HDR10+"):   # «лишние балы», «за это балов быть не может»
            self.assertNotIn(gone, keys)
        # the hidden coefficient columns follow CONSTS one to one (the formula addresses 'Баллы'!$B$k by position)
        self.assertEqual([h[2:] for h, _ in sheet.MAIN_COLUMNS if h.startswith("f:")], keys)

    def test_owner_values(self):
        w = rank.settings()["sort"]["weights"]
        self.assertEqual((w["owner_want"], w["owner_maybe"], w["owner_possible"]), (50, 10, 0))
        self.assertEqual(w["hdr"], {"DV": 40, "DV*": 40, "HDR10": 35})
        self.assertEqual((w["voice"]["amateur"], w["voice"]["avo"]), (6, 1))
        self.assertNotIn("wishlist", w)

    def test_read_back_by_label(self):
        """An old tab (rows HDR10+, HLG, «в желаемом» still there) must not shift values onto other constants."""
        old = [["Признак", "Баллы"], ["Dolby Vision", "40"], ["HDR10+", "30"], ["DV из HDR10 (статические)", "40"],
               ["HDR10", "35"], ["HLG", "10"], ["в желаемом (Google TV)", "15"], ["4K", "26"]]
        ws = mock.Mock(get_all_values=mock.Mock(return_value=old))
        sh = mock.Mock(worksheet=mock.Mock(return_value=ws))
        written = {"hdr.DV": 40, "hdr.DV*": 40, "hdr.HDR10": 35, "uhd": 25}
        with tempfile.TemporaryDirectory() as d:
            wfile = Path(d) / "w.json"
            wfile.write_text(json.dumps(written), encoding="utf-8")
            cfg = Path(d) / "config"
            cfg.mkdir()
            (cfg / "settings.toml").write_text('[sort.weights]\nhdr = { "DV" = 40, "DV*" = 40, "HDR10" = 35 }\nuhd = 25\n',
                                               encoding="utf-8")
            with mock.patch.object(sheet, "WEIGHTS_WRITTEN", wfile), mock.patch.object(sheet.db, "ROOT", Path(d)):
                stats = sheet.read_back_weights(sh)
            text = (cfg / "settings.toml").read_text(encoding="utf-8")
        self.assertEqual(stats["weights"], 1)   # only «4K» 25 → 26
        self.assertIn("uhd = 26", text)
        self.assertIn('"DV*" = 40', text)


class LabelsTest(unittest.TestCase):
    def test_dv_always_with_profile(self):
        self.assertEqual(rank.hdr_label("DV", 7), "DV P7")
        self.assertEqual(rank.hdr_label("DV", None), "DV P?")
        self.assertEqual(rank.hdr_label("DV*", 8), "DV P8 (статические)")
        self.assertEqual(rank.hdr_label("HDR10"), "HDR10")
        self.assertIn("DV P?", rank.describe({"resolution": 2160, "source_type": "WEB-DL", "hdr": '["DV", "HDR10"]'}))
        why = rank.upgrade_reasons({"resolution": 2160, "hdr": ["HDR10"], "has_ru_uk": True, "voice": "dub"},
                                   {"resolution": 2160, "hdr": '["DV"]', "dv_profile": 8, "best_voice": "dub"},
                                   rank.settings())
        self.assertIn("HDR10→DV P8", why)

    def test_names_english_instead_of_cjk(self):
        self.assertEqual(sheet._names("Без лица", "얼굴", latin="The Ugly"), "Без лица\nThe Ugly")
        self.assertEqual(sheet._names("Охота за тенью", "捕风追影", latin=None), "Охота за тенью")
        self.assertEqual(sheet._names("Дом", "ข้างบ้าน", latin="Our House"), "Дом\nOur House")
        self.assertEqual(sheet._names("Реверсия", "Reversión"), "Реверсия\nReversión")   # accented Latin stays

    def test_franchise_label(self):
        self.assertEqual(sheet._short_franchise("Голодные игры — вся франшиза"), "Голодные игры")
        self.assertEqual(sheet._short_franchise("Final Fantasy (вкл. Compilation of FFVII)"), "Final Fantasy")
        self.assertEqual(sheet._short_franchise("Marvel — всё"), "Marvel")
        cols = {1: ("Бэтмен (Коллекция)", "Batman Collection"), 2: ("“默杀宇宙”系列电影", "Silent Universe Collection")}
        with mock.patch.dict(sheet._FRANCHISE_OF, {"tt29355505": "История игрушек"}, clear=True):
            self.assertEqual(sheet.franchise_label({"imdb_id": "tt29355505", "collection_id": 1}, cols), "История игрушек")
            self.assertEqual(sheet.franchise_label({"imdb_id": "tt1", "collection_id": 1}, cols), "Бэтмен")
            self.assertEqual(sheet.franchise_label({"imdb_id": "tt2", "collection_id": 2}, cols), "Silent Universe")
            self.assertIsNone(sheet.franchise_label({"imdb_id": "tt3"}, cols))
            self.assertIsNone(sheet.franchise_label({"imdb_id": "tt4", "collection_id": 3},
                                                    {3: ("刺殺小說家（系列）", "దేవర Collection")}))

    def test_folder_not_path(self):
        self.assertEqual(rank.file_folder(r"E:\Films\Optional\Hidden Strike (2023) WEB-DL 1080p.mkv"), "Optional")
        self.assertIsNone(rank.file_folder(None))
        self.assertIn(("Папка", 140), sheet.QUALITY_COLUMNS)


class DownloadedTest(unittest.TestCase):
    def test_visible_check_refuses_downloaded(self):
        from films import visible_check
        have = {"title": {"id": 1, "title_ru": "Головоломка 2"}, "have": {"resolution": 2160}}
        queued = {"title": {"id": 2, "title_ru": "Самозванец"}, "have": None, "queued": {"name": "x"}}
        free = {"title": {"id": 3, "title_ru": "Другое"}, "have": None}
        self.assertEqual(len(visible_check.downloaded_shown([have, queued, free])), 2)


class HiddenColumnsTest(unittest.TestCase):
    def test_every_column_state_is_set(self):
        """The tab is reused in place: a visible column must be explicitly unhidden («Новый размер» stayed hidden)."""
        ws = mock.Mock(id=1)
        sh = mock.Mock()
        sh.fetch_sheet_metadata.return_value = {"sheets": []}
        sent = []
        sh.batch_update.side_effect = lambda body: sent.append(body)
        cols = sheet.QUALITY_COLUMNS
        with mock.patch.object(ws, "update"), mock.patch.object(ws, "resize"):
            sheet._write(sh, ws, cols, [], False)
        reqs = [r["updateDimensionProperties"] for b in sent for r in b.get("requests", [])
                if "updateDimensionProperties" in r and "hiddenByUser" in r["updateDimensionProperties"]["fields"]]
        state = {r["range"]["startIndex"]: r["properties"]["hiddenByUser"] for r in reqs}
        self.assertEqual(state, {i: h == "title_id" for i, (h, _) in enumerate(cols)})


if __name__ == "__main__":
    unittest.main()
