"""franchise_seen hide rules (review 2026-10-28): an animated short is a cartoon by its title's genres even when the member
kind is «short» (R84: «Бегущий по лезвию: Блэкаут 2022»), and one franchise's rule does not hide a work that another
followed franchise keeps («Паук внутри»: Marvel «animation only MCU» vs the owner's Spider-Verse)."""
import unittest
from unittest import mock

from films import franchise_seen as fs

BR = {"key": "blade_runner", "name": "Бегущий по лезвию", "scope": "all", "animation": "hide"}
MARVEL = {"key": "marvel", "name": "Marvel — всё", "scope": "all", "animation_only": ["tt10168312"]}
SPIDER = {"key": "spider_verse", "name": "Spider-Verse", "scope": "all"}


class HideRulesTest(unittest.TestCase):
    def test_animated_short_by_title_genre(self):
        m = {"imdb_id": "tt7428594", "kind": "short", "year": 2017}
        with mock.patch.object(fs, "_ANIM_TITLES", set()):
            self.assertFalse(fs._too_old(BR, m))
        with mock.patch.object(fs, "_ANIM_TITLES", {"tt7428594"}):
            self.assertTrue(fs._too_old(BR, m))

    def test_other_followed_franchise_keeps_it(self):
        m = {"imdb_id": "tt27369002", "kind": "short", "year": 2023}
        in_frs = {"tt27369002": [dict(m, franchise_key="marvel"), dict(m, franchise_key="spider_verse")]}
        followed = {"marvel": MARVEL, "spider_verse": SPIDER}
        with mock.patch.object(fs, "_ANIM_TITLES", {"tt27369002"}):
            self.assertTrue(fs._too_old(MARVEL, m))                                   # Marvel alone would hide it
            self.assertTrue(fs.kept_elsewhere(MARVEL, "tt27369002", followed, in_frs))  # Spider-Verse keeps it
            self.assertFalse(fs.kept_elsewhere(MARVEL, "tt27369002", {"marvel": MARVEL}, in_frs))


if __name__ == "__main__":
    unittest.main()
