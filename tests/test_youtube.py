"""Tests for films/sources/youtube.py (offline: parsing, matching rules, decisions).

The video titles below are real YouTube search results seen during the 2026-10-23 tuning runs.
Run:  python -X utf8 -m unittest tests.test_youtube
"""
import json
import unittest

from films.sources import youtube as yt

FETCHED = "2026-10-23T10:00:00+00:00"


def ctx(**kw):
    base = {"id": 1, "kind": "movie", "season_number": None, "seasons_total": None, "title_ru": "Нежить",
            "title_original": "In a Violent Nature", "title_latin": None, "title_uk": "Природа насильства",
            "year": 2024, "release_date": "2024-01-22", "is_animation": 0, "series_key": None,
            "homonyms_db": [], "season_dates": {}}
    base.update(kw)
    return base


def video(title, channel="Kinoman", length="1:37", published="2 г. назад", views=100000, snippet="", pos=0,
          vid=None):
    return {"id": vid or title[:11], "pos": pos, "title": title, "channel": channel, "length_text": length,
            "length": yt.parse_length(length), "published": published, "views": views, "badges": [],
            "owner_badges": [], "snippet": snippet}


def ev(title, c=None, **kw):
    return yt.evaluate(video(title, **kw), c or ctx(), FETCHED)


class TestParsing(unittest.TestCase):
    def test_length(self):
        self.assertEqual(yt.parse_length("1:27"), 87)
        self.assertEqual(yt.parse_length("1:22:22"), 4942)
        self.assertIsNone(yt.parse_length(""))
        self.assertIsNone(yt.parse_length("LIVE"))
        # review 2026-10-28: the search gives seconds already — `trailer_lang originals` crashed on `int.strip`
        self.assertEqual(yt.parse_length(87), 87)
        self.assertEqual(yt.parse_length(yt.parse_length("1:27")), 87)

    def test_originals_on_parsed_search(self):
        import sqlite3
        from unittest import mock
        from films import db
        from films.sources import trailer_lang
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        db.migrate(c)
        c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (1, 'movie', 'Dune', 2021)")
        c.execute("INSERT INTO releases (id, source, source_id, url, name) VALUES (1, 'rutor', '1', 'u', 'n')")
        c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (1, 1)")
        fake = mock.Mock()
        fake.search.return_value = {"videos": [{"id": "abc", "title": "Dune Official Trailer", "length": 150}]}
        with mock.patch.object(trailer_lang.yt_mod, "YouTube", return_value=fake):
            st = trailer_lang.search_originals(c, log=lambda *_: None)
        self.assertEqual(st["found"], 1)
        # review round 9: a watched title gets no search; a find no longer accepted is taken back on --recheck
        c.execute("INSERT INTO titles (id, kind, title_original, year) VALUES (2, 'movie', 'Doubt', 2008)")
        c.execute("INSERT INTO release_titles (release_id, title_id) VALUES (1, 2)")
        c.execute("INSERT INTO watched (title_id, source) VALUES (2, 'plex')")
        c.execute("UPDATE titles SET trailer_alt_url = 'https://www.youtube.com/watch?v=old' WHERE id = 1")
        fake.search.return_value = {"videos": [{"id": "x", "title": "Dune: Part Two Official Trailer 2024", "length": 150}]}
        with mock.patch.object(trailer_lang.yt_mod, "YouTube", return_value=fake):
            st = trailer_lang.search_originals(c, recheck=True, log=lambda *_: None)
        self.assertEqual((st["titles"], st["taken_back"]), (1, 1))
        self.assertIsNone(c.execute("SELECT trailer_alt_url FROM titles WHERE id = 1").fetchone()[0])

    def test_original_fits(self):
        from films.sources.trailer_lang import original_fits as fits
        self.assertTrue(fits("Doubt", 2008, "movie", None, "DOUBT | Official Trailer (2008)"))
        self.assertTrue(fits("Doubt", 2008, "movie", None, "Official Trailer: Doubt"))
        self.assertFalse(fits("Doubt", 2008, "movie", None, "Reasonable Doubt Official Trailer"))
        self.assertFalse(fits("Jumanji", 1995, "movie", None, "Jumanji: Open World Trailer 2026"))
        self.assertFalse(fits("Dogtown 2", 2025, "movie", None, "Lords of Dogtown (2005) Trailer"))
        self.assertFalse(fits("Blood & Sweat", 2026, "series", 1, "What the Hell Happened to Blood, Sweat & Tears? | TRAILER"))
        self.assertFalse(fits("Arafta", 2026, "series", 1, "Arafta Season 2 - Official Trailer"))
        self.assertTrue(fits("Arafta", 2026, "series", 2, "Arafta Season 2 - Official Trailer"))
        self.assertFalse(fits("Güneşin Doğduğu Yer", 2026, "series", 1, "Güneşin Doğduğu Yer 7. Bölüm Fragman"))
        # review round 10: a name with its own «:»
        self.assertTrue(fits("Red Days 2: Cecilia", 2026, "movie", None, "Red Days 2: Cecilia Trailer"))
        self.assertFalse(fits("Jumanji", 1995, "movie", None, "Jumanji: Open World Trailer"))
        # review round 11: tags, full-width marks, another script first, a subtitle that is a trailer word …
        for name, vt in (("Lost in Paradise", "【DRAMA SERIES】Lost in Paradise｜Trailer"),
                         ("Lost in Paradise", "Lost in Paradise: Original Trailer"),
                         ("Red Days", "红色日子 Red Days 预告"), ("Face à face", "Face à face : bande-annonce"),
                         ("Angel Flight", "Angel Flight: The Movie Trailer"), ("Doubt", "[ENG SUB] Doubt Trailer"),
                         ("Stuntman", "Stuntman (2024) 武替道 - Movie Trailer"),
                         ("Missing", "Missing (2024) Japanese Movie Trailer English Subtitles")):
            self.assertTrue(fits(name, 2024, "movie", None, vt), vt)
        # … and a name that only opens another title
        for name, vt in (("The Company", "The Company You Keep Official Trailer"), ("Dream", "Dream Scenario Trailer"),
                         ("102", "102 Dalmatians Trailer"), ("Klara", "Klara and the Sun Trailer"),
                         ("Daddy", "Daddy's Head Trailer"), ("Pati", "Pati 2 | zwiastun")):
            self.assertFalse(fits(name, None, "movie", None, vt), vt)
        # review round 12: a series' own season number, other languages' words, a bracketed name, a year in the name …
        for args in (("Pati", 2025, "series", 2, "Pati 2 | zwiastun", "pl"),
                     ("Rüzgarlı Tepe", 2025, "series", 2, "Rüzgarlı Tepe 2. Sezon 1. Tanıtım", "tr"),
                     ("STHLM Blackout", 2024, "series", 1, "STHLM Blackout Säsong 1 - Officiell Trailer", "sv"),
                     ("Sygeplejeskolen", 2025, "series", 6, "Trailer: Sygeplejeskolen sæson 6", "da"),
                     ("Estado de fuga 1986", 2025, "series", 1, "Estado de fuga 1986 | Tráiler oficial | Netflix", "es"),
                     ("New Biography of Xishi", 2025, "series", 1, "|【西施新传 New Biography of Xishi】| Trailer", "zh"),
                     ("Le Mystère de la chambre jaune", 2025, "movie", None, "Bande-annonce Le Mystère de la chambre jaune TF1", "fr"),
                     ("Informant – Angst über der Stadt", 2024, "series", 1, "Informant – Angst über der Stadt (ARD Trailer)", "de"),
                     ("The New Dragon Gate Inn", 2024, "movie", None, "《新龍門客棧 The New Dragon Gate Inn》 3分鐘預告", "zh"),
                     ("Angel Flight THE MOVIE", 2025, "movie", None, "Angel Flight: The Movie | Official Trailer", "ja"),
                     ("Kaboul", 2024, "series", 1, "Kaboul - Bande annonce VF - France TV", "fr"),
                     ("102", 2025, "movie", None, "102 | ТРЕЙЛЕР | Режиссер Эрнест Абдыжапаров", "ky"),
                     ("Мастер Витя и Мотор", 2024, "series", 1, "Тизер новой серии / Мастер Витя и Мотор", None),
                     ("Билет из рая", 2025, "movie", None, 'Официальный трейлер комедийной драмы "Билет из Рая".', None),
                     ("Mater's Tall Tales", 2008, "series", 1, "Cars Toon: Mater's Tall Tales - Trailer", None),
                     # review round 13
                     ("Vefa Sultan", 2024, "series", 1, "Vefa Sultan İlk Tanıtım", "tr"),
                     ("Vicdansız", 2023, "series", 1, "Vicdansız Dizisi Tanıtım Fragmanı", "tr"),
                     ("L'École des espions", 2025, "series", 1, "Bande-annonce de l'École des espions", "fr"),
                     ("The Ainsley McGregor Mysteries: A Case for the Winemaker", 2024, "movie", None,
                      '"Ainsley McGregor Mysteries: A Case for the Winemaker" | Official Trailer', "en"),
                     ("Naari Naari Naduma Murari", 2026, "movie", None, "Naari Naari Naduma Murari Tamil Trailer", "te")):
            self.assertTrue(fits(*args), args[4])
        # … and another work's trailer: the name after «Other name:», a one-word name of a non-English title in an
        # English video, another season as «Series 3», the series' film, a Cyrillic name going on
        for args in (("Prophecy", 2024, "movie", None, "Dune: Prophecy | Official Series Trailer | Max", "it"),
                     ("2099", 2026, "movie", None, "BLADE RUNNER: 2099 Teaser Trailer (2026)", "zh"),
                     ("Doppio gioco", 2025, "series", 1, "Black Bag: Doppio Gioco | Trailer Ufficiale", "it"),
                     ("Patriot", 2025, "movie", None, "Patriot - Official Trailer | Prime Video", "ml"),
                     ("Surface", 2025, "series", 1, "Surface Season 1 Trailer | Rotten Tomatoes TV", "fr"),
                     ("Mandy", 2025, "series", 4, "Mandy Series 3 OFFICIAL Trailer – BBC", "en"),
                     ("No. 1 Sentai Gozyuger", 2025, "series", 1, "No. 1 Sentai Gozyuger THE MOVIE FINAL TRAILER", "ja"),
                     ("Друзі", 2025, "series", 1, "Друзі та сусіди | Apple TV+", "uk"),
                     ("Мошенники", 2025, "series", 1, "Телефонные мошенники - Русский трейлер", None),
                     # review round 13: an alt name in brackets is no separator; no «A» dropped
                     ("Sword of Coming", 2024, "series", 1, "Sword of Coming (Movie) | Official Trailer", "zh"),
                     ("A Family Thanksgiving", 2025, "movie", None, "Family Thanksgiving | Official Trailer", "en"),
                     ("Pati", 2025, "movie", None, "Pati 2 | zwiastun", "pl")):
            self.assertFalse(fits(*args), args[4])
        # review round 14: «Short» only for a short film; another Indian language is a dub; «The» left out — two words
        # or the year
        self.assertTrue(fits("Hawaiian Vacation", 2011, "movie", None, "Toy Story - Hawaiian Vacation Short Official Trailer",
                             runtime=6))
        self.assertFalse(fits("Dark Romance", 2025, "movie", None, "Dark Romance Short Film Trailer", runtime=104))
        self.assertTrue(fits("Ayalaan", 2024, "movie", None, "Ayalaan - Official Tamil Trailer", orig_lang="ta"))
        self.assertFalse(fits("Ayalaan", 2024, "movie", None, "Ayalaan - Official Telugu Trailer", orig_lang="ta"))
        self.assertFalse(fits("Naari Naari Naduma Murari", 2026, "movie", None, "Naari Naari Naduma Murari Tamil Trailer",
                              orig_lang="te"))
        self.assertTrue(fits("Pushpa 2", 2024, "movie", None, "Pushpa 2 Official Trailer", orig_lang="te"))
        self.assertFalse(fits("The Shadow Dancer", 2005, "movie", None, "Shadow Dancer Official Trailer #1 (2012)"))
        self.assertFalse(fits("The Shadow Dancer", 2005, "movie", None, "Shadow Dancer Official Trailer"))
        self.assertTrue(fits("The Shadow Dancer", 2005, "movie", None, "Shadow Dancer (2005) Trailer"))
        self.assertTrue(fits("The Ainsley McGregor Mysteries", 2024, "movie", None, "Ainsley McGregor Mysteries | Trailer"))

    def test_any_is_still_a_trailer(self):
        from films.sources.trailer_lang import _ANY_NOT, _ANY_TRAILER
        for t in ("Paolo Temp.1 2026 | Trailer Oficial HD HBO", "Haysiyet 1. Bölüm Fragmanı", "Kardeşlerim Yeni Sezon Tanıtım",
                  "映画『X』予告"):
            self.assertTrue(_ANY_TRAILER.search(t) and not _ANY_NOT.search(t), t)
        for t in ("Kid-E-Cats Cartoon", "Dune Opening Scene", "Dune Ending Explained", "X - Deleted Scene"):
            self.assertFalse(_ANY_TRAILER.search(t) and not _ANY_NOT.search(t), t)

    def test_views(self):
        self.assertEqual(yt.parse_views("74\xa0215 просмотров"), 74215)
        self.assertEqual(yt.parse_views("Нет просмотров"), 0)

    def test_ago(self):
        self.assertEqual(yt.parse_ago("2 г. назад"), (2 * 365.25, 3 * 365.25))
        self.assertEqual(yt.parse_ago("8 мес. назад")[0], 8 * 30.44)
        self.assertEqual(yt.parse_ago("3 дня назад"), (3, 4))
        self.assertEqual(yt.parse_ago("Трансляция закончилась 5 лет назад")[0], 5 * 365.25)
        self.assertIsNone(yt.parse_ago(None))

    def test_parse_results(self):
        data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {"sectionListRenderer": {
            "contents": [{"itemSectionRenderer": {"contents": [
                {"videoRenderer": {
                    "videoId": "igY2n2_qpwk", "title": {"runs": [{"text": "Нежить - Русский трейлер"}]},
                    "ownerText": {"runs": [{"text": "World Pictures", "navigationEndpoint": {"browseEndpoint": {
                        "browseId": "UCc40FDyPGFmvrGw7ndPlmWw", "canonicalBaseUrl": "/@worldpicturesrus"}}}]},
                    "publishedTimeText": {"simpleText": "2 г. назад"},
                    "lengthText": {"simpleText": "1:27"}, "viewCountText": {"simpleText": "74 215 просмотров"},
                    "ownerBadges": [{"metadataBadgeRenderer": {"style": "BADGE_STYLE_TYPE_VERIFIED"}}],
                    "detailedMetadataSnippets": [{"snippetText": {"runs": [{"text": "Премьера 4 июля 2024"}]}}]}},
                {"shortsLockupViewModel": {"x": 1}},
            ]}}]}}}}}
        html = "<script>var ytInitialData = " + json.dumps(data, ensure_ascii=False) + ";</script>"
        vs = yt.parse_results(html)
        self.assertEqual(len(vs), 1)
        v = vs[0]
        self.assertEqual((v["id"], v["channel"], v["length"], v["views"]), ("igY2n2_qpwk", "World Pictures", 87, 74215))
        self.assertEqual(v["channel_url"], "/@worldpicturesrus")
        self.assertEqual(v["owner_badges"], ["BADGE_STYLE_TYPE_VERIFIED"])
        self.assertEqual(v["snippet"], "Премьера 4 июля 2024")
        self.assertEqual(yt.parse_results("<html>no data</html>"), [])


class TestWatch(unittest.TestCase):
    def test_audio_language(self):
        # YouTube AI auto-dubs present: the original (not auto-dubbed) track decides
        tracks = [{"id": "en-US.10", "auto_dub": True}, {"id": "ru.4", "auto_dub": False}]
        self.assertEqual(yt.audio_language(tracks, [{"lang": "en-US", "kind": "asr"}, {"lang": "ru", "kind": "asr"}]),
                         ("ru", "audio_track"))
        tracks = [{"id": "de-DE.10", "auto_dub": True}, {"id": "en.4", "auto_dub": False}]
        self.assertEqual(yt.audio_language(tracks, [{"lang": "en", "kind": "asr"}])[0], "en")
        # no dubs: the single automatic caption track (spoken language)
        self.assertEqual(yt.audio_language([], [{"lang": "ru", "kind": "asr"}]), ("ru", "asr_captions"))
        self.assertEqual(yt.audio_language([], [{"lang": "en", "kind": None}, {"lang": "ru", "kind": None}]),
                         (None, "no_signal"))

    def test_parse_watch(self):
        pr = {"playabilityStatus": {"status": "OK"},
              "videoDetails": {"title": "The Whisper Man | Official Trailer", "author": "Moviefone",
                               "lengthSeconds": "151", "viewCount": "1000", "shortDescription": "x"},
              "microformat": {"playerMicroformatRenderer": {"publishDate": "2026-01-01", "availableCountries": ["UA"]}},
              "streamingData": {"adaptiveFormats": [
                  {"audioTrack": {"id": "en-US.4", "audioIsDefault": True}},
                  {"audioTrack": {"id": "ru.10", "isAutoDubbed": True}}]},
              "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [{"languageCode": "en", "kind": "asr"}]}}}
        html = "<script>var ytInitialPlayerResponse = " + json.dumps(pr) + ";</script>"
        w = yt.parse_watch(html, "MVouNFpQFRw")
        self.assertEqual((w["status"], w["title"], w["audio_lang"], w["length"], w["available_in_ua"]),
                         ("OK", "The Whisper Man | Official Trailer", "en", 151, True))


class TestText(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(yt.norm("Ёлки: «Последние»!"), "елки последние")
        self.assertEqual(yt.find_phrase(yt.tokens("Нежить — Русский трейлер"), ["нежить"]), [0])

    def test_season_mentions(self):
        self.assertEqual(yt.season_mentions("Король Талсы (2 сезон) — Русский трейлер (2024)"), {2})
        self.assertEqual(yt.season_mentions("ДЖЕНТЛЬМЕНЫ Сезон 1 - Русский трейлер"), {1})
        self.assertEqual(yt.season_mentions("Чужестранка: второй сезон | трейлер"), {2})
        self.assertEqual(yt.season_mentions("The Bear Season 3 | Official Trailer"), {3})
        self.assertEqual(yt.season_mentions("Пацаны 4-й сезон"), {4})
        self.assertEqual(yt.season_mentions("Основание — Русский трейлер (2021)"), set())


class TestEvaluate(unittest.TestCase):
    def test_good_trailer(self):
        c = ev("Нежить — Русский трейлер (2024)", channel="топКино")
        self.assertTrue(c["ok"], c)
        self.assertIn("year_match", c["flags"])

    def test_other_film_same_word(self):
        c = ev("Нежить. Нападение тигров - Русский трейлер (2022)", channel="TrailerOK")
        self.assertFalse(c["ok"])

    def test_sequel(self):
        self.assertFalse(ev("Нежить 2 — Русский трейлер (2025)")["ok"])

    def test_name_missing(self):
        self.assertFalse(ev("Нечисть — Русский трейлер (2024)")["ok"])

    def test_full_movie_rejected(self):
        self.assertFalse(ev("Нежить (2024) — русский трейлер", length="1:22:22")["ok"])

    def test_review_rejected(self):
        self.assertFalse(ev("Нежить — обзор трейлера (2024)")["ok"])
        self.assertFalse(ev("Нежить — реакция на трейлер")["ok"])

    def test_subtitled_is_finding(self):
        c = ev("Нежить — трейлер с русскими субтитрами (2024)")
        self.assertFalse(c["ok"])
        self.assertEqual(c["lang"], "subtitled")

    def test_ukrainian_is_finding(self):
        c = ev("Природа насильства — офіційний трейлер (українською)")
        self.assertFalse(c["ok"])
        self.assertEqual(c["lang"], "uk")

    def test_no_trailer_word(self):
        self.assertFalse(ev("Нежить (2024)")["ok"])

    def test_too_old(self):
        self.assertFalse(ev("Нежить — Русский трейлер", published="6 лет назад")["ok"])

    def test_movie_vs_series(self):
        self.assertFalse(ev("Нежить (сериал, 1 сезон) — Русский трейлер")["ok"])

    def test_name_continues(self):
        c0 = ctx(title_ru="Проклятые", title_original="I dannati")
        c = ev("Проклятые воды — Русский трейлер (Дубляж, 2024)", c0, published="1 г. назад")
        self.assertIn("name_extended", c["flags"])
        self.assertFalse(ev("Проклятые — Официальный трейлер (2024)", c0)["flags"].count("name_extended"))
        # a separator between the name and the next word is fine
        self.assertNotIn("name_extended", ev("Нежить. Русский трейлер", published="2 г. назад")["flags"])

    def test_word_inside_the_name_is_not_a_reject_word(self):
        c0 = ctx(kind="series", season_number=1, seasons_total=1, title_ru="Мир Юрского периода: Теория хаоса",
                 title_original="Jurassic World: Chaos Theory", year=2024)
        c = ev("Мир юрского периода Теория хаоса 1 сезон Русский трейлер 2024", c0)
        self.assertTrue(c["ok"], c)

    def test_name_without_conjunction(self):
        c0 = ctx(kind="series", season_number=4, seasons_total=4, title_ru="Любовь, смерть и роботы",
                 title_original="Love, Death & Robots", year=2025)
        c = ev("Любовь. Смерть. Роботы (4 Сезон) - Русский трейлер 2 (Дубляж, 2025)", c0, published="1 г. назад")
        self.assertTrue(c["ok"], c)

    def test_years_ago_short_form(self):
        self.assertEqual(yt.parse_ago("6 л. назад")[0], 6 * 365.25)
        c0 = ctx(kind="series", season_number=1, seasons_total=1, title_ru="Тьма", title_original="The Dark",
                 year=2026)
        self.assertFalse(ev("Тьма (Dark) || 1 season || Трейлер", c0, published="6 л. назад")["ok"])

    def test_kazakh_is_not_ukrainian(self):
        c0 = ctx(title_ru="Борец", title_original="Qajymuqan", title_uk="Қажымұқан", year=2024)
        self.assertEqual(ev("Қажымұқан — ресми трейлер (2024)", c0)["lang"], "other")
        # 'Келесі аялдама' has only the letter і, shared with Ukrainian: stays an (unconfirmed) uk finding
        c1 = ctx(title_ru="Арман", title_original="Armand", year=2024)
        self.assertFalse(yt.uk_confirmed(ev("Трейлер фильма: Келесі аялдама - Арман (2024)", c1)))

    def test_ukrainian_original_language_upload(self):
        c0 = ctx(title_ru="Поздняя слава", title_original="Late Fame", title_uk="Пізня слава", year=2025)
        c = ev("Пізня слава. Трейлер ( мовою оригіналу )", c0, published="3 мес. назад")
        self.assertEqual(c["lang"], "uk")
        self.assertTrue(c["reject"])

    def test_year_before(self):
        c0 = ctx(title_ru="Она танцует", title_original="She Dances", year=2025, release_date="2025-06-05")
        c = ev("Она танцует - Трейлер 2024", c0, channel="Киноман", published="2 г. назад")
        self.assertIn("year_before", c["flags"])
        self.assertEqual(yt.decide([c], c0)["status"], "ambiguous")

    def test_subtitled_channel(self):
        c = ev("[RUS] Нежить (2024) | Русский трейлер", channel="it's just a cat RUS SUB")
        self.assertEqual(c["lang"], "subtitled")

    def test_original_name_only(self):
        c = ev("In a Violent Nature — русский трейлер (2024)")
        self.assertTrue(c["ok"], c)


class TestSeasons(unittest.TestCase):
    def season(self, n, total=3, **kw):
        return ctx(kind="series", season_number=n, seasons_total=total, title_ru="Чужестранка",
                   title_original="Outlander", title_uk=None, year=2026, release_date="2026-03-06",
                   series_key="imdb:tt3006802", **kw)

    def test_right_season(self):
        self.assertTrue(ev("Чужестранка (8 сезон) — Русский трейлер (2026)", self.season(8))["ok"])

    def test_wrong_season(self):
        self.assertFalse(ev("Чужестранка (7 сезон) — Русский трейлер", self.season(8))["ok"])

    def test_no_season_number_for_later_season(self):
        self.assertFalse(ev("Чужестранка — Русский трейлер", self.season(8))["ok"])

    def test_season_one_unstated(self):
        c = ev("Чужестранка — Русский трейлер (2026)", self.season(1, total=1), published="7 мес. назад")
        self.assertTrue(c["ok"], c)


class TestDecide(unittest.TestCase):
    def test_accept_and_prefer_trailer(self):
        c0 = ctx()
        cands = [ev("Нежить — тизер (2024)", channel="топКино", views=10 ** 6, vid="teaser"),
                 ev("Нежить — Русский трейлер (2024)", channel="Kinoman", views=10 ** 5, vid="trailer")]
        d = yt.decide(cands, c0)
        self.assertEqual(d["status"], "accepted")
        self.assertEqual(d["best"]["id"], "trailer")

    def test_homonym_without_year_is_ambiguous(self):
        c0 = ctx(title_ru="В ловушке", title_original="Prisonnière", year=2025,
                 homonyms_db=[{"id": 9, "year": 2024, "kind": "movie"}])
        d = yt.decide([ev("В ловушке — Русский трейлер", c0, views=10 ** 6)], c0)
        self.assertEqual(d["status"], "ambiguous")

    def test_subtitled_siblings_need_a_dub_claim(self):
        c0 = ctx(title_ru="Покойся с миром", title_original="Descansar en paz", year=2024)
        plain = ev("Покойся с миром (2024) | Официальный трейлер | Нетфликс", c0, channel="Трейлеры Netflix", vid="a")
        sub = ev("Покойся с миром - русский трейлер (субтитры) | фильм 2024 | Netflix", c0, channel="LandFM", vid="b")
        d = yt.decide([plain, sub], c0)
        self.assertEqual(d["status"], "ambiguous")
        dub = ev("Покойся с миром — Русский трейлер (Дубляж, 2024)", c0, vid="c")
        d = yt.decide([plain, sub, dub], c0)
        self.assertEqual((d["status"], d["best"]["id"]), ("accepted", "c"))

    def test_cast_names_explain_words(self):
        c0 = ctx(title_ru="На грани войны", title_original="The Brink of War", year=2026,
                 cast_tokens={"jeff", "daniels", "jared", "harris", "simmons", "джефф", "даниелс", "джаред",
                              "харрис", "симмонс"})
        c = ev("«На грани войны»: официальный трейлер (2026) Джефф Дэниелс, Джаред Харрис, Дж. К. Симмонс", c0,
               published="6 мес. назад")
        self.assertIn("cast_in_title", c["flags"])
        self.assertNotIn("many_other_words", c["flags"])
        self.assertEqual(yt.translit("Daniels"), "даниелс")

    def test_longer_name_confirmed_by_season_and_original(self):
        c0 = ctx(kind="series", season_number=4, seasons_total=4, title_ru="Ричер", title_original="Reacher",
                 year=2026, release_date="2026-09-01")
        c = ev("Джек Ричер (4 сезон) — Русский трейлер (2026)", c0, published="2 мес. назад",
               snippet="Reacher season 4")
        self.assertIn("name_extended", c["flags"])
        self.assertEqual(yt.decide([c], c0)["status"], "accepted")
        # without the original-title confirmation it stays ambiguous
        c = ev("Джек Ричер (4 сезон) — Русский трейлер (2026)", c0, published="2 мес. назад")
        self.assertEqual(yt.decide([c], c0)["status"], "ambiguous")

    def test_cast_fuzzy_is_strict(self):
        # «Черный лебедь. Тайна»: one fuzzy hit on the actor Tapia is not enough
        c0 = ctx(title_ru="Лебедь", title_original="The Swan", year=2023, cast_tokens={"tapia", "тапиа"})
        self.assertNotIn("cast_in_title", ev("Черный лебедь. Тайна — Русский трейлер (2023)", c0,
                                             published="3 г. назад")["flags"])
        # an exact first name + a fuzzy surname is
        c1 = ctx(title_ru="Неукротимый", title_original="Unstoppable", year=2025,
                 cast_tokens={"jennifer", "lopez", "дженнифер", "лопез"})
        self.assertIn("cast_in_title", ev("Неукротимый Трейлер (2025) Дженнифер Лопес", c1,
                                          published="1 г. назад")["flags"])

    def test_partial_name_needs_more_than_a_close_year(self):
        c0 = ctx(title_ru="Код Гиас: Восставший Лелуш — Император", title_original="コードギアス", year=2018,
                 release_date="2018-02-10")
        c = ev("Код Гиас: Лелуш Воскресший — Русский трейлер (2019)", c0, channel="Kinoman", published="7 л. назад")
        self.assertIn("partial_name", c["flags"])
        self.assertEqual(yt.decide([c], c0)["status"], "ambiguous")

    def test_fan_trailer(self):
        self.assertFalse(ev("Фан трейлер Нежить (2024)")["ok"])

    def test_nothing(self):
        self.assertEqual(yt.decide([ev("Нечисть — трейлер")], ctx())["status"], "not_found")


class TestQueries(unittest.TestCase):
    def test_movie(self):
        qs = yt.queries_for(ctx())
        self.assertEqual(qs[0], "Нежить 2024 трейлер на русском")
        self.assertIn("In a Violent Nature 2024 русский трейлер", qs)

    def test_season(self):
        qs = yt.queries_for(ctx(kind="series", season_number=2, title_ru="Док", title_original="Doc", year=2025))
        self.assertEqual(qs[0], "Док 2 сезон трейлер на русском")
        self.assertTrue(all("2 сезон" in q for q in qs))


if __name__ == "__main__":
    unittest.main()
