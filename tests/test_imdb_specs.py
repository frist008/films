"""Tests for films/sources/imdb_specs.py.

Offline tests feed hand-built GraphQL technicalSpecifications blocks (shapes copied from real responses) to
the pure parsers.  TestCache checks real titles through the raw cache data/cache/imdb_specs/ (skipped when a
tconst is not cached yet -- run `python -X utf8 -m films.sources.imdb_specs run` first); expected values were
checked against IMDb's technical-specifications data and common knowledge of the films.
Run:  python -X utf8 -m unittest tests.test_imdb_specs
"""
import unittest

from films.sources import imdb_specs as m


def block(field, *items):
    """items: (value, [attrs]) -> GraphQL list block."""
    return {"total": len(items), "items": [{field: v, "attributes": [{"text": a} for a in attrs]} for v, attrs in items]}


def specs(**lists):
    out = {}
    for key, items in lists.items():
        out[key] = block(m.SPEC_KEYS[key], *items)
    return out


class TestDisplay(unittest.TestCase):
    def test_display(self):
        self.assertEqual(m.display("2.39 : 1", []), "2.39 : 1")
        self.assertEqual(m.display("DTS", ["DTS: X"]), "DTS (DTS: X)")
        self.assertEqual(m.display("1.43 : 1", ["IMAX 70mm & Dual Laser", "some scenes"]),
                         "1.43 : 1 (IMAX 70mm & Dual Laser, some scenes)")


class TestDI(unittest.TestCase):
    def di(self, *processes):
        return m.di_resolution(specs(processes=list(processes)))

    def test_4k(self):
        self.assertEqual(self.di(("ARRIRAW", ["6.5K", "4.5K", "source format"]),
                                 ("Digital Intermediate", ["4K", "master format"]),
                                 ("Dolby Vision", [])), ("4K", "ok"))

    def test_2k(self):
        self.assertEqual(self.di(("Digital Intermediate", ["2K", "master format"])), ("2K", "ok"))

    def test_sub_4k_classes(self):
        self.assertEqual(self.di(("Digital Intermediate", ["HD", "master format"]))[0], "HD")
        self.assertEqual(self.di(("Digital Intermediate", ["1080p", "master format"]))[0], "HD")
        self.assertEqual(self.di(("Digital Intermediate", ["480p", "master format"]))[0], "SD")
        self.assertEqual(self.di(("Digital Intermediate", ["3.2K"]))[0], "2K")
        self.assertEqual(self.di(("Digital Intermediate", ["2K/25fps", "master format"]))[0], "2K")
        self.assertEqual(self.di(("Digital Intermediate", ["4K", "Master Format"]))[0], "4K")

    def test_upscaled_4k_is_2k(self):
        self.assertEqual(self.di(("Digital Intermediate", ["upscaled 4K", "master format"]))[0], "2K")

    def test_remaster_wins(self):
        self.assertEqual(self.di(("Digital Intermediate", ["2K", "master format"]),
                                 ("Digital Intermediate", ["4K", "2018 remaster"])), ("4K", "ok_remaster"))
        self.assertEqual(self.di(("Digital Intermediate", ["4K", "master format", "2018 restoration"])),
                         ("4K", "ok_remaster"))
        # comma-joined attribute text (Cowboys & Aliens)
        self.assertEqual(self.di(("Digital Intermediate", ["2K", "master format"]),
                                 ("Digital Intermediate", ["4K, 2024 remaster"])), ("4K", "ok_remaster"))
        self.assertEqual(self.di(("Digital Intermediate", ["4K", "master format", "remastered version"]))[0], "4K")
        self.assertEqual(self.di(("Digital Intermediate", ["4K", "master format", '2007 "Final Cut" release',
                                                          "2017 remaster"]))[0], "4K")
        # a restored version beats a partial 2K entry (Princess Mononoke)
        self.assertEqual(self.di(("Digital Intermediate", ["2K", "master format", "some sequences"]),
                                 ("Digital Intermediate", ["4K", "master format", "2025 restored version"]))[0], "4K")

    def test_regional_versions_are_partial(self):
        v, note = self.di(("Digital Intermediate", ["2K", "master format", "mainland China and Hong Kong versions"]))
        self.assertIsNone(v)
        self.assertTrue(note.startswith("partial_only"))
        v, note = self.di(("Digital Intermediate", ["upscaled 4K", "master format", "Cinity and IMAX versions"]))
        self.assertIsNone(v)

    def test_ambiguous_entry(self):
        v, note = self.di(("Digital Intermediate", ["2K", "4K", "master format"]))
        self.assertIsNone(v)
        self.assertTrue(note.startswith("ambiguous_entry_full"))

    def test_above_4k_kept_literal(self):
        self.assertEqual(self.di(("Digital Intermediate", ["8K", "master format"]))[0], "8K")
        self.assertEqual(self.di(("Digital Intermediate", ["4.5K"]))[0], "4.5K")

    def test_substance_partial_ignored(self):
        # The Substance (2024): 2K master + 4K for the TV-show scenes only -> 2K
        self.assertEqual(self.di(("Digital Intermediate", ["2K", "master format"]),
                                 ("Digital Intermediate", ["4K", "master format", "TV show scenes only"])),
                         ("2K", "ok"))

    def test_conflict(self):
        v, note = self.di(("Digital Intermediate", ["2K", "master format"]),
                          ("Digital Intermediate", ["4K", "master format"]))
        self.assertIsNone(v)
        self.assertTrue(note.startswith("conflict_full"))

    def test_partial_only(self):
        v, note = self.di(("Digital Intermediate", ["4K", "some scenes"]))
        self.assertIsNone(v)
        self.assertTrue(note.startswith("partial_only"))

    def test_no_resolution_and_absent(self):
        self.assertEqual(self.di(("Digital Intermediate", ["master format"])), (None, "di_without_resolution"))
        self.assertEqual(self.di(("ARRIRAW", ["4.5K", "source format"])), (None, "no_di"))
        self.assertEqual(m.di_resolution({}), (None, "no_di"))
        self.assertEqual(m.di_resolution(None), (None, "no_di"))

    def test_two_full_4k_entries_keep_highest(self):
        self.assertEqual(self.di(("Digital Intermediate", ["4K", "master format"]),
                                 ("Digital Intermediate", ["8K", "master format"]))[0], "8K")


class TestImax(unittest.TestCase):
    def test_dune_part_two(self):
        s = specs(
            aspectRatios=[("1.43 : 1", ["IMAX 70mm & Dual Laser", "some scenes"]), ("1.90 : 1", ["IMAX Xenon & Single Laser"]),
                          ("2.20 : 1", ["70 mm prints"]), ("2.39 : 1", [])],
            soundMixes=[("Dolby Atmos", []), ("IMAX 6-Track", []), ("DTS", ["DTS: X"])],
            printedFormats=[("70 mm", ["also horizontal", "also IMAX DMR blow-up", "Kodak Vision 2383"]),
                            ("DCP Digital Cinema Package", [])],
            cameras=[("Arri Alexa 65, Petzval Heroes Look", [])],
            processes=[("Digital Intermediate", ["4K", "master format"])])
        is_imax, sources, evidence, notes = m.imax_verdict(s)
        self.assertEqual(is_imax, 1)
        self.assertEqual(sources, ["imdb_printed", "imdb_aspect", "imdb_sound"])
        self.assertEqual(len(evidence), 4)

    def test_imax_camera_first(self):
        s = specs(cameras=[("IMAX MSM 9802, Panavision Lenses", [])], aspectRatios=[("1.43 : 1", ["IMAX"])])
        self.assertEqual(m.imax_verdict(s)[1], ["imdb_camera", "imdb_aspect"])

    def test_filmed_for_imax_camera_attribute(self):
        s = specs(cameras=[("Sony Venice 2, Panavision Lenses", ["Filmed for IMAX"])])
        self.assertEqual(m.imax_verdict(s)[:2], (1, ["imdb_camera"]))

    def test_imax_dmr_process(self):
        s = specs(processes=[("IMAX DMR", ["Digital Media Remastering"])])
        self.assertEqual(m.imax_verdict(s)[:2], (1, ["imdb_process"]))

    def test_sound_only(self):
        self.assertEqual(m.imax_verdict(specs(soundMixes=[("IMAX 12-Track", [])]))[:2], (1, ["imdb_sound"]))

    def test_negative(self):
        s = specs(aspectRatios=[("2.39 : 1", [])], soundMixes=[("Dolby Atmos", [])],
                  cameras=[("Arri Alexa 35", [])])
        self.assertEqual(m.imax_verdict(s), (0, [], [], []))

    def test_non_imax_is_not_imax(self):
        s = specs(aspectRatios=[("2.39 : 1", ["non-IMAX scenes"]), ("2.39 : 1", ["non IMAX version"])])
        self.assertEqual(m.imax_verdict(s)[0], 0)

    def test_imax_home_video_cut_out(self):
        s = specs(aspectRatios=[("1.78 : 1", ["IMAX Blu-ray & 4K UHD", "some scenes"])])
        self.assertEqual(m.imax_verdict(s)[0], 0)
        s = specs(aspectRatios=[("1.90 : 1", ["IMAX Xenon, Single Laser & Disney+ IMAX Enhanced Version"])])
        self.assertEqual(m.imax_verdict(s)[:2], (1, ["imdb_aspect"]))

    def test_lab_imax_prints(self):
        s = specs(laboratories=[("Consolidated Film Industries (CFI), Hollywood (CA), USA", ["IMAX prints"])])
        self.assertEqual(m.imax_verdict(s)[:2], (1, ["imdb_lab"]))

    def test_imax_enhanced_is_noted_not_counted(self):
        s = specs(aspectRatios=[("1.90 : 1", ["IMAX Enhanced", "Disney+"]), ("2.39 : 1", [])])
        is_imax, sources, _, notes = m.imax_verdict(s)
        self.assertEqual(is_imax, 0)
        self.assertTrue(any(n.startswith("imax_enhanced_only") for n in notes))

    def test_bare_143_noted_not_counted(self):
        s = specs(aspectRatios=[("1.43 : 1", []), ("2.39 : 1", [])])
        is_imax, _, _, notes = m.imax_verdict(s)
        self.assertEqual(is_imax, 0)
        self.assertEqual(notes, ["bare_1.43: 1.43 : 1"])

    def test_unknown_when_no_specs(self):
        self.assertEqual(m.imax_verdict({})[0], None)
        self.assertEqual(m.imax_verdict(None)[0], None)
        # coloration alone is not evidence
        self.assertEqual(m.imax_verdict(specs(colorations=[("Color", [])]))[0], None)

    def test_word_boundary(self):
        # "IMAXIMUM"-like strings and words containing imax are not IMAX
        self.assertEqual(m.imax_verdict(specs(cameras=[("Maximax Lens", [])]))[0], 0)


class TestMoneyPosterAkas(unittest.TestCase):
    node = {
        "titleText": {"text": "X"},
        "productionBudget": {"budget": {"amount": 190000000, "currency": "USD"}},
        "lifetimeGross_ww": {"total": {"amount": 714844358, "currency": "USD"}},
        "primaryImage": {"url": "https://m.media-amazon.com/images/M/MV5Babc@._V1_.jpg", "width": 1, "height": 1},
        "akas": {"total": 2, "edges": [
            {"node": {"text": "Дюна: Частина друга", "country": {"id": "UA"}, "language": None, "attributes": []}},
            {"node": {"text": "Дюна: Часть вторая", "country": {"id": "RU"}, "language": None, "attributes": []}}]},
    }

    def test_money(self):
        self.assertEqual(m.money(self.node), {"budget": 190000000, "budget_currency": "USD",
                                              "gross_ww": 714844358, "gross_ww_currency": "USD"})
        self.assertEqual(m.money({"productionBudget": None}),
                         {"budget": None, "budget_currency": None, "gross_ww": None, "gross_ww_currency": None})
        self.assertEqual(m.money({"productionBudget": {"budget": {"amount": 5000000, "currency": "EUR"}}})["budget_currency"], "EUR")

    def test_poster(self):
        self.assertEqual(m.poster(self.node), "https://m.media-amazon.com/images/M/MV5Babc@._V1_.jpg")
        self.assertIsNone(m.poster({"primaryImage": None}))

    def test_ua_akas(self):
        self.assertEqual([a["text"] for a in m.akas_for(self.node, "UA")], ["Дюна: Частина друга"])


class TestSuspectLink(unittest.TestCase):
    def test_suspect_link(self):
        self.assertIsNone(m.suspect_link("movie", 2024, "movie", 2024))
        self.assertIsNone(m.suspect_link("movie", 2025, "movie", 2024))     # festival year vs release year
        self.assertIsNone(m.suspect_link("movie", 2024, "tvMovie", 2024))
        self.assertIsNone(m.suspect_link("series", 2025, "tvSeries", 2019))  # a later season of an old show
        # title 1320 "The Fantastic Four: First Steps" (2025) linked to tt1054216, a 1994 TV episode
        self.assertEqual(m.suspect_link("movie", 2025, "tvEpisode", 1994), "imdb_is_episode")
        self.assertEqual(m.suspect_link("movie", 2025, "movie", 1994), "year_differs_by_more_than_1")
        self.assertEqual(m.suspect_link("series", 2025, "movie", 2025), "series_row_but_imdb_movie")

    def test_poster_same_work(self):
        def row(title, year, kind="movie", latin=None):
            return {"kind": kind, "year": year, "title_original": title, "title_latin": latin}
        s = {"title": "Batman: The Animated Series", "year": 1992, "original_title": None}
        self.assertTrue(m.poster_same_work(row("Batman: The Animated Series", 1992), s, "movie_row_but_imdb_tvSeries"))
        s = {"title": "Something, Something, Something, Dark Side", "year": 2009, "original_title": None}
        self.assertTrue(m.poster_same_work(row("Family Guy Presents: Something, Something, Something, Dark Side", 2009),
                                           s, "imdb_is_episode"))
        s = {"title": "Attack on Titan", "year": 2013, "original_title": "Shingeki no kyojin"}
        self.assertTrue(m.poster_same_work(row("Shingeki no Kyojin", 2013), s, "movie_row_but_imdb_tvSeries"))
        # a different work with a longer title (the movie vs the "Chronicles" series) -> no
        s = {"title": "Justice League: Gods and Monsters Chronicles", "year": 2015, "original_title": None}
        self.assertFalse(m.poster_same_work(row("Justice League: Gods and Monsters", 2015), s, "movie_row_but_imdb_tvSeries"))
        # a one-word tail is not enough; a year gap is not the same work; a year mismatch reason never is
        s = {"title": "Naruto", "year": 2002, "original_title": None}
        self.assertFalse(m.poster_same_work(row("Naruto + Naruto Shippuuden", 2002), s, "movie_row_but_imdb_tvSeries"))
        s = {"title": "Ocean's Eleven", "year": 1960, "original_title": None}
        self.assertFalse(m.poster_same_work(row("Ocean's Eleven", 2001), s, "year_differs_by_more_than_1"))
        s = {"title": "Jimmy Neutron: Boy Genius", "year": 2001, "original_title": None}
        self.assertFalse(m.poster_same_work(row("The Adventures of Jimmy Neutron: Boy Genius", None, "series"),
                                            s, "series_row_but_imdb_movie"))


def edges(*nodes):
    return {"total": len(nodes), "edges": [{"node": n} for n in nodes]}


class TestNodeImax(unittest.TestCase):
    """IMAX evidence outside the technical specs (animated features: Inside Out 2, Moana 2 …)."""

    def company(self, name, cat, *attrs, cid=m.IMAX_COMPANY_ID):
        return {"company": {"id": cid, "companyText": {"text": name}}, "category": {"id": cat},
                "countries": [], "attributes": [{"text": a} for a in attrs]}

    def release(self, year, country, *attrs):
        return {"day": 1, "month": 6, "year": year, "country": {"id": country}, "attributes": [{"text": a} for a in attrs]}

    def test_company_optimized_for_imax(self):
        node = {"releaseYear": {"year": 2024},
                "companyCredits": edges(self.company("IMAX", "miscellaneous", "IMAX Theatres", "optimized for"))}
        s = specs(soundMixes=[("Dolby Atmos", [])])
        self.assertEqual(m.imax_verdict(s, node)[:2], (1, ["imdb_company"]))
        self.assertEqual(m.imax_verdict(s)[:2], (0, []))         # without the node: specs only

    def test_release_in_imax(self):
        node = {"releaseYear": {"year": 2024}, "releaseDates": edges(self.release(2024, "US", "IMAX"))}
        self.assertEqual(m.imax_verdict(specs(soundMixes=[("Dolby Atmos", [])]), node)[:2], (1, ["imdb_release"]))
        self.assertEqual(m.imax_verdict({}, node)[0], 1)          # evidence without tech specs

    def test_rereleases_and_screenings_not_counted(self):
        node = {"releaseYear": {"year": 1999}, "releaseDates": edges(
            self.release(2021, "SG", "re-release", "IMAX"),            # The Matrix 2021
            self.release(1999, "US", "IMAX version", "re-release"),
            self.release(2000, "CN", "IMAX version", "Beijing International Film Festival"),
            self.release(2000, "GB", "Bristol IMAX Screening", "premiere")),
            "distributors": edges({"company": {"id": m.IMAX_COMPANY_ID, "companyText": {"text": "IMAX"}},
                                   "countries": [{"id": "US"}], "attributes": [{"text": "theatrical"}]})}
        is_imax, sources, _, notes = m.imax_verdict(specs(soundMixes=[("Dolby Digital", [])]), node)
        self.assertEqual((is_imax, sources), (0, []))
        self.assertEqual(len(notes), 5)

    def test_other_company_and_home_video_ignored(self):
        node = {"releaseYear": {"year": 2024},
                "companyCredits": edges(self.company("Animax", "distribution", "TV", cid="co0127153")),
                "releaseDates": edges(self.release(2024, "US", "IMAX Enhanced"))}
        self.assertEqual(m.imax_verdict(specs(soundMixes=[("Dolby Digital", [])]), node), (0, [], [], []))


class TestImaxRules2026(unittest.TestCase):
    """2026-10-25: the IMAX credit beyond the first 50 company credits (Zootopia 2) and pre-DMR re-releases."""

    def imax_company(self, *attrs):
        return {"company": {"id": m.IMAX_COMPANY_ID, "companyText": {"text": "IMAX"}},
                "category": {"id": "miscellaneous"}, "countries": [], "attributes": [{"text": a} for a in attrs]}

    def test_other_companies_list_counts(self):
        # companyCredits(first: 50) cut short; the IMAX credit only in the full "Other companies" list
        node = {"releaseYear": {"year": 2025},
                "companyCredits": {"total": 108, "edges": []},
                "otherCompanies": edges(self.imax_company("optimize for"))}
        self.assertEqual(m.imax_verdict(specs(soundMixes=[("Dolby Atmos", [])]), node)[:2], (1, ["imdb_company"]))

    def test_bare_acknowledgement_or_footage_not_counted(self):
        for attrs in (("acknowledgement",), ("specialty cameras",), ("thanks",),
                      ("IMAX Corporation", 'courtesy of: "Space Station" © 2018 Lockheed Corporation and IMAX Corporation')):
            node = {"releaseYear": {"year": 2014}, "otherCompanies": edges(self.imax_company(*attrs))}
            is_imax, _, _, notes = m.imax_verdict(specs(soundMixes=[("Dolby Digital", [])]), node)
            self.assertEqual(is_imax, 0, attrs)
            self.assertTrue(any("imax_company_not_counted" in n for n in notes))
        for attrs in (("specially formatted in",), ("filmed for",), ("camera & lenses by",), ("optimize for",)):
            node = {"releaseYear": {"year": 2014}, "otherCompanies": edges(self.imax_company(*attrs))}
            self.assertEqual(m.imax_verdict({}, node)[0], 1, attrs)

    def test_same_credit_in_both_lists_once(self):
        c = self.imax_company("optimized for")
        node = {"releaseYear": {"year": 2025}, "companyCredits": edges(c), "otherCompanies": edges(c)}
        self.assertEqual(len(m.imax_verdict({}, node)[2]), 1)

    def test_sound_only_counts(self):
        node = {"releaseYear": {"year": 2017}}
        self.assertEqual(m.imax_verdict(specs(soundMixes=[("IMAX 6-Track", [])]), node)[:2], (1, ["imdb_sound"]))

    def test_pre_dmr_rerelease_not_counted(self):
        node = {"releaseYear": {"year": 1981}}           # Raiders of the Lost Ark: IMAX version = 2023 re-release
        s = specs(soundMixes=[("IMAX 6-Track", ["IMAX version"]), ("Dolby Digital", [])])
        is_imax, sources, _, notes = m.imax_verdict(s, node)
        self.assertEqual((is_imax, sources), (0, []))
        self.assertTrue(any(n.startswith("imax_rerelease_pre_dmr") for n in notes))
        node97 = {"releaseYear": {"year": 1997}}         # Titanic: IMAX print of the 2012 3D re-release
        self.assertEqual(m.imax_verdict(specs(printedFormats=[("D-Cinema", ["IMAX"])]), node97)[0], 0)

    def test_pre_dmr_shot_on_imax_or_dated_release_counts(self):
        node = {"releaseYear": {"year": 1999}, "releaseDates": edges(
            {"day": 1, "month": 1, "year": 2000, "country": {"id": "US"}, "attributes": [{"text": "IMAX"}]})}
        self.assertEqual(m.imax_verdict(specs(printedFormats=[("70 mm", ["IMAX"])]), node)[0], 1)  # Fantasia 2000
        cam = {"releaseYear": {"year": 1998}}
        self.assertEqual(m.imax_verdict(specs(cameras=[("IMAX MSM 9802", [])]), cam)[0], 1)
        self.assertEqual(m.imax_verdict(specs(printedFormats=[("D-Cinema", ["IMAX"])]), {"releaseYear": {"year": 2002}})[0], 1)


class TestUkTitle(unittest.TestCase):
    """Ukrainian-title fallback from IMDb akas (owner, 2026-10-25)."""

    def aka(self, text, *attrs, lang=None):
        return {"text": text, "language": lang, "attributes": list(attrs)}

    def test_main_title_preferred_over_alternative(self):
        self.assertEqual(m.pick_uk_title([self.aka("Перша ніч з принцом", "Alternative Title"),
                                          self.aka("Я вкрала першу ніч головного героя!")]),
                         ("Я вкрала першу ніч головного героя!", ""))

    def test_new_title_before_alternative(self):
        self.assertEqual(m.pick_uk_title([self.aka("Б", "Alternative Title"), self.aka("Без лиця", "new title")]),
                         ("Без лиця", "new title"))

    def test_skips_working_festival_literal_and_season_titles(self):
        self.assertEqual(m.pick_uk_title([self.aka("Робоча", "working title"), self.aka("Фест", "festival title"),
                                          self.aka("Дослівна", "literal title"),
                                          self.aka("Сезон два", "second season title")]), (None, None))
        self.assertEqual(m.pick_uk_title([self.aka("Фантазійна соната", "literal title"),
                                          self.aka("Пісня кохання для ілюзії")])[0], "Пісня кохання для ілюзії")

    def test_latin_and_russian_rejected(self):
        self.assertEqual(m.pick_uk_title([self.aka("One Piece"), self.aka("Ван Піс", "Alternative Title")]),
                         ("Ван Піс", "Alternative Title"))
        self.assertEqual(m.pick_uk_title([self.aka("Крылья")]), (None, None))                  # «ы» is Russian
        self.assertEqual(m.pick_uk_title([self.aka("Тетрадь смерти", lang="ru")]), (None, None))
        self.assertEqual(m.pick_uk_title([self.aka("Dune", lang="en")]), (None, None))
        self.assertEqual(m.pick_uk_title([self.aka("1917")]), ("1917", ""))

    def test_latin_i_normalized(self):
        self.assertEqual(m.pick_uk_title([self.aka("Покемон Фiльм: Бiле")])[0], "Покемон Фільм: Біле")

    def test_uk_title_for_sources(self):
        rec = {"node": {"akas": edges({"text": "Дюна: Частина друга", "country": {"id": "UA"}, "language": None,
                                       "attributes": []})}}
        self.assertEqual(m.uk_title_for(rec), ("Дюна: Частина друга", "imdb_aka"))
        self.assertEqual(m.uk_title_for(None, [{"title": "Дамбо", "language": None, "types": None, "attributes": None}]),
                         ("Дамбо", "imdb_dataset_aka"))
        self.assertEqual(m.uk_title_for({"node": {"akas": {"edges": []}}}, []), (None, None))


class TestCache(unittest.TestCase):
    """Real titles from the raw cache (no network)."""

    def get(self, tconst):
        rec = m.load_cached(tconst)
        if rec is None:
            self.skipTest(f"{tconst} not cached")
        return m.summarize(rec)

    def test_dune_part_two(self):
        s = self.get("tt15239678")
        self.assertEqual((s["di_resolution"], s["is_imax"]), ("4K", 1))
        self.assertIn("1.43 : 1 (IMAX 70mm & Dual Laser, some scenes)", s["aspect_ratios"])
        self.assertIn("IMAX 6-Track", s["sound_mix"])
        self.assertEqual((s["budget"], s["budget_currency"]), (190000000, "USD"))

    def test_the_substance_2k(self):
        s = self.get("tt17526714")
        self.assertEqual(s["di_resolution"], "2K")


if __name__ == "__main__":
    unittest.main()
