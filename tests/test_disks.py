"""films.disks name parsing — the pitfalls met in the first full scan (R85)."""
import unittest

from films import disks


class EpisodeOf(unittest.TestCase):
    def test_markers(self):
        self.assertEqual(disks.episode_of("Agents.of.S.H.I.E.L.D.S03E06.WEB-DL.1080p")[:2], (3, 6))
        self.assertEqual(disks.episode_of("Terminator Zero - 01 (WEBRip 1920x1080 x264 AC3 Rus + Jap + Eng)")[:2], (None, 1))
        self.assertEqual(disks.episode_of("One_Piece_[720]_[Nazel]_[AniMedia.TV]")[:2], (None, 720))   # not 720p
        self.assertEqual(disks.episode_of("[AniDub]_To_Aru_Majutsu_no_Index_TV_[01_of_24]_[RUS_JAP]")[:2], (None, 1))
        self.assertEqual(disks.episode_of("1 серия Синий экзорцист- Сага")[:2], (None, 1))

    def test_not_episodes(self):
        self.assertEqual(disks.episode_of("Movie.2024.1080p.x264")[:2], (None, None))
        self.assertEqual(disks.episode_of("Pacific Rim 2160p x265")[:2], (None, None))


class Parse(unittest.TestCase):
    def test_roman_season(self):
        self.assertEqual(disks._parse("Toaru Majutsu no Index II ")[0::2], ("Toaru Majutsu no Index", 2))
        self.assertEqual(disks._parse("[AniDub]_Seikon_no_Qwaser_II_BD_")[0::2], ("Seikon no Qwaser", 2))

    def test_numbered_needs_only_tags_after(self):
        self.assertTrue(disks._only_tags(".HDTV.1080.Dub"))
        self.assertFalse(disks._only_tags(".Tsumi.to.Bachi.WEBRip"))


class Privacy(unittest.TestCase):
    def test_profile_files_are_masked(self):
        p, folder = disks._profile_personal("C:\\Users\\u\\Мой диск (a.b@example.com)\\Лекции\\x.mp4", 10)
        self.assertNotIn("a.b@", p)
        self.assertNotIn("Лекции", p)
        self.assertIsNone(disks._profile_personal("C:\\Users\\u\\Downloads\\Film.2024.mkv", 2 * 2 ** 30))


if __name__ == "__main__":
    unittest.main()
