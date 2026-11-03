"""Sheet view rules from the owner's review (2026-10-25): outlines of franchise blocks and of a title's variant rows;
«хуже: …» on «Обновить качество»."""
import unittest

from films import rank, sheet


def row(tid, variant=0):
    return {"title": {"id": tid}, "variant_index": variant}


class BlocksTest(unittest.TestCase):
    def test_franchise_and_variants(self):
        # franchise g1: titles 1 (two variants) and 2; title 3 alone; title 4 with two variants, no group
        ordered = [row(1), row(1, 1), row(2), row(3), row(4), row(4, 1)]
        groups = {1: "g1", 2: "g1", 3: "g3"}
        b = sheet._blocks(ordered, groups)
        self.assertIn((0, 2, "franchise"), b)
        self.assertIn((0, 1, "variants"), b)
        self.assertIn((4, 5, "variants"), b)
        self.assertNotIn((3, 3, "franchise"), b)                        # one title is not a franchise block
        self.assertEqual([k for *_, k in b], sorted([k for *_, k in b], key=lambda k: k == "franchise"))  # thick last


class DowngradeTest(unittest.TestCase):
    def test_worse_voice_and_resolution(self):
        have = {"voice": "dub", "has_ru_uk": True, "resolution": 2160, "hdr": ["DV"]}
        best = {"best_voice": "mvo", "resolution": 1080, "hdr": "[]"}
        w = rank.downgrade_reasons(have, best)
        self.assertTrue(any(x.startswith("озвучка mvo") for x in w))
        self.assertTrue(any(x.startswith("разрешение 1080") for x in w))
        self.assertTrue(any("у меня DV" in x for x in w))

    def test_not_worse(self):
        have = {"voice": "mvo", "has_ru_uk": True, "resolution": 1080, "hdr": []}
        best = {"best_voice": "dub", "resolution": 2160, "hdr": '["DV"]'}
        self.assertEqual(rank.downgrade_reasons(have, best), [])


if __name__ == "__main__":
    unittest.main()
