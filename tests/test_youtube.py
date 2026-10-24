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
