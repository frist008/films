"""films/rank.py — release rules of the owner's metadata survey (2026-10-25) and R168 (series voice-over).

Run:  python -X utf8 -m unittest tests/test_rank_release_rules.py
"""
import json
import unittest

from films import rank

CFG = {
    "sort": {"voice_rank": ["dub", "mvo", "dvo", "svo", "avo", "amateur", "original", "none"],
             "source_rank": ["BDRemux", "Blu-ray", "Hybrid", "BDRip", "WEB-DL", "WEB-DLRip", "WEBRip", "HDTV"]},
    "dub_unofficial": {"studios": ["Red Head Sound", "RHS", "Jaskier", "Flarrow Films"]},
    "voice_preference": {"franchises": [{"keys": ["marvel", "dc"], "all_episodes": True,
                                         "studios": ["Flarrow Films", "FlarrowFilms", "Flarrow"]}]},
}


def rel(**kw):
    r = {"id": kw.pop("id", 1), "name": "X / X (2025) WEB-DL 1080p", "best_voice": "dub", "resolution": 1080,
         "source_type": "WEB-DL", "hdr": "[]", "audio": "[]", "h264_equiv_kbps": 8000, "seeders": 10}
    r.update(kw)
    return r


def best(rels, studios=None, ctx=None):
    return min(rels, key=lambda r: rank.release_key(r, CFG, studios, ctx))


class ReleaseKeyTest(unittest.TestCase):
    def test_remaster_first_but_needs_a_voice(self):
        hd = rel(id=1, hdr='["DV", "HDR10"]', source_type="BDRemux", h264_equiv_kbps=30000)
        rem = rel(id=2, remaster="Criterion", source_type="BDRip")
        ctx = {"any_voiced": True, "remaster_res": 1080}
        self.assertEqual(best([hd, rem], ctx=ctx)["id"], 2)       # «максимальный приоритет»: above the same resolution
        uhd = rel(id=5, resolution=2160, hdr='["DV", "HDR10"]', source_type="BDRemux")
        self.assertEqual(best([uhd, rem], ctx=ctx)["id"], 5)      # «того же качества»: a 4K disc is not pushed below
        uhd_mvo = rel(id=6, resolution=2160, hdr='["DV", "HDR10"]', best_voice="mvo")
        self.assertEqual(best([uhd_mvo, rem], ctx=ctx)["id"], 2)  # … and then the voice decides
        mute = rel(id=3, remaster="Remastered", best_voice="none")
        self.assertEqual(best([hd, mute], ctx={"any_voiced": True})["id"], 1)   # a remaster without any voice: no
        fan = rel(id=4, remaster="Remastered", is_ai_upscale=1)
        self.assertEqual(best([hd, fan], ctx={"any_voiced": True})["id"], 1)    # fan AI upscale «remastered»: no

    def test_official_dub_above_unofficial(self):
        rhs = rel(id=1, dub_official=0, resolution=2160, hdr='["DV"]')
        lic = rel(id=2, dub_official=1)
        self.assertEqual(best([rhs, lic])["id"], 2)
        mvo = rel(id=3, best_voice="mvo", resolution=2160, hdr='["DV"]')
        self.assertEqual(best([rhs, mvo])["id"], 1)          # an unofficial dub is still a dub («дубляж важнее качества»)

    def test_ads_localized_colour(self):
        self.assertEqual(best([rel(id=1, has_ads=1, h264_equiv_kbps=20000), rel(id=2)])["id"], 2)
        self.assertEqual(best([rel(id=1, h264_equiv_kbps=20000), rel(id=2, localized_video=1)])["id"], 2)
        # within the same picture class only: a 4K HDR copy stays above a 1080p one with Russian titles
        self.assertEqual(best([rel(id=1, resolution=2160, hdr='["HDR10"]'), rel(id=2, localized_video=1)])["id"], 1)
        self.assertEqual(best([rel(id=1, video_color="bw", resolution=2160), rel(id=2, video_color="colorized")])["id"], 2)
        # a black-and-white film (IMDb): its colorized version first
        self.assertEqual(best([rel(id=1, resolution=2160), rel(id=2, video_color="colorized")], ctx={"title_bw": True})["id"], 2)

    def test_flarrow_above_4k_hdr_then_best_of_its_releases(self):
        st = rank.voice_preference(CFG, {"kind": "series"}, {"marvel"})["studios"]
        rhs = rel(id=1, name="Сорвиголова [S01] (2025) UHD WEB-DL 2160p | 4K | HDR | D | Red Head Sound",
                  resolution=2160, hdr='["DV", "HDR10"]')
        fl_sd = rel(id=2, name="Сорвиголова [S01] (2025) WEB-DLRip | D | Локализованная версия | Flarrow Films", resolution=480,
                    h264_equiv_kbps=1500)
        fl_hd = rel(id=3, name="Сорвиголова [S01] (2025) WEB-DLRip 1080p | D | Локализованная версия | Flarrow Films")
        self.assertEqual(best([rhs, fl_sd, fl_hd], st)["id"], 3)
        self.assertEqual(rank.voice_preference(CFG, {"kind": "movie"}, {"marvel"})["studios"], [])   # series only
        self.assertEqual(rank.voice_preference(CFG, {"kind": "series"}, {"star_wars"})["studios"], [])

    def test_series_without_dub_18plus_professional_first(self):
        mvo = rel(id=1, best_voice="mvo", resolution=2160)
        dvo18 = rel(id=2, best_voice="dvo", voice_18plus=1)
        self.assertEqual(best([mvo, dvo18], ctx={"series_no_dub": True})["id"], 2)
        self.assertEqual(best([mvo, dvo18], ctx={"series_no_dub": False})["id"], 1)


class HelpersTest(unittest.TestCase):
    def test_dub_official(self):
        unoff = {"best_voice": "dub", "audio": [{"lang": "ru", "kind": "dub", "studio": "Red Head Sound"}]}
        self.assertEqual(rank.dub_official(unoff, CFG), 0)
        said = {"best_voice": "dub", "audio": [{"lang": "ru", "kind": "dub", "studio": "Flarrow Films", "official": True}]}
        self.assertEqual(rank.dub_official(said, CFG), 1)                 # «Дублированный (официальный) | Flarrow Films»
        cinema = {"best_voice": "dub", "audio": [{"lang": "ru", "kind": "dub", "studio": "Movie Dubbing"}]}
        self.assertEqual(rank.dub_official(cinema, CFG), 1)
        name_only = {"best_voice": "dub", "audio": [], "name_parsed": {"studios": ["Jaskier"], "licensed": False}}
        self.assertEqual(rank.dub_official(name_only, CFG), 0)
        lic = {"best_voice": "dub", "audio": [], "name_parsed": {"studios": ["Jaskier"], "licensed": True}}
        self.assertEqual(rank.dub_official(lic, CFG), 1)
        self.assertIsNone(rank.dub_official({"best_voice": "mvo", "audio": []}, CFG))

    def test_describe_bw_and_fps(self):
        self.assertEqual(rank.describe(rel(fps=59.94)), "1080 WEB-DL 60 fps dub")
        self.assertEqual(rank.describe(rel(fps=23.976)), "1080 WEB-DL dub")
        self.assertEqual(rank.describe(rel(), title_bw=True), "1080 WEB-DL ч/б dub")
        self.assertEqual(rank.describe(rel(video_color="colorized"), title_bw=True), "1080 WEB-DL dub")
        self.assertTrue(rank.title_is_bw({"color": "Black and White"}))
        self.assertFalse(rank.title_is_bw({"color": "Color | Black and White (some scenes)"}))

    def test_episodes_reached(self):
        self.assertEqual(rank.episodes_reached({"episodes": "1-5"}), 5)
        self.assertEqual(rank.episodes_reached({"episodes": "7"}), 7)
        self.assertIsNone(rank.episodes_reached({"episodes": None}))


class StudioEpisodesTest(unittest.TestCase):
    """R168(б): «серий N из M с этой озвучкой» + an extra row with another voice where more (all) episodes are out."""
    T = {"kind": "series", "episodes_total": 8}

    def setUp(self):
        self.pref = rank.voice_preference(CFG, {"kind": "series"}, {"marvel"})

    def test_partial_flarrow_gets_note_and_extra_row(self):
        fl = rel(id=1, name="Сорвиголова [S02] [01x01-05 из 08] (2026) WEB-DLRip 1080p | D | Flarrow Films", episodes="1-5")
        rhs = rel(id=2, name="Сорвиголова [S02] (2026) UHD WEB-DL 2160p | D | Red Head Sound", episodes="1-8", resolution=2160)
        part = rel(id=3, name="Сорвиголова [S02] (2026) WEB-DL 1080p | P | LostFilm", episodes="1-6")
        note, eps, extra = rank.studio_episodes(self.T, fl, [fl, rhs, part], self.pref)
        self.assertEqual((note, eps, extra["id"]), ("серий 5 из 8 с этой озвучкой", (5, 8), 2))

    def test_complete_flarrow_no_extra_row(self):
        fl = rel(id=1, name="X [S02] (2026) WEB-DLRip 1080p | D | Flarrow Films")          # whole-season pack
        rhs = rel(id=2, name="X [S02] (2026) WEB-DL 1080p | D | Red Head Sound", episodes="1-8")
        self.assertEqual(rank.studio_episodes(self.T, fl, [fl, rhs], self.pref), (None, (8, 8), None))

    def test_not_flarrow_best(self):
        rhs = rel(id=2, name="X [S02] (2026) WEB-DL 1080p | D | Red Head Sound", episodes="1-8")
        self.assertEqual(rank.studio_episodes(self.T, rhs, [rhs], self.pref), (None, None, None))


class MaxQualityTest(unittest.TestCase):
    T = {"kind": "movie"}

    def test_dub_and_best_picture(self):
        uhd = rel(id=1, resolution=2160, hdr='["DV", "HDR10"]')
        self.assertTrue(rank.max_quality(self.T, uhd, [uhd, rel(id=2)], CFG))
        sd = rel(id=2)
        self.assertFalse(rank.max_quality(self.T, sd, [uhd, sd], CFG))       # a better picture exists (any voice)
        mvo = rel(id=3, best_voice="mvo", resolution=2160, hdr='["DV", "HDR10"]')
        self.assertFalse(rank.max_quality(self.T, mvo, [mvo], CFG))          # not a dub → look for a dub
        disc = {"kind": "movie", "upscale_source": "bluray_com"}
        self.assertFalse(rank.max_quality(disc, sd, [sd], CFG))              # blu-ray.com reviewed a 4K disc
        self.assertFalse(rank.max_quality(self.T, {"id": None, "best_voice": "dub"}, [sd], CFG))   # queued torrent row

    def test_flarrow_needs_all_episodes(self):
        pref = rank.voice_preference(CFG, {"kind": "series"}, {"dc"})
        fl = rel(id=1, name="X [S01] (2025) WEB-DLRip 1080p | D | Flarrow Films")
        self.assertTrue(rank.max_quality({"kind": "series"}, fl, [fl], CFG, pref, (8, 8)))
        self.assertFalse(rank.max_quality({"kind": "series"}, fl, [fl], CFG, pref, (5, 8)))
        other = rel(id=2, name="X [S01] (2025) WEB-DL 1080p | D | Red Head Sound")
        self.assertFalse(rank.max_quality({"kind": "series"}, other, [other], CFG, pref, None))


if __name__ == "__main__":
    unittest.main()
