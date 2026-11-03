"""seen_survey.chunks: one category per question, 4 works (each option one work, its franchise named) (owner, 2026-10-27: «одна франшиза один
вопрос», «В самом вопросе одна франшиза, но ты же можешь их объединять» — four such questions go in one call)."""
import unittest

from films import seen_survey


def item(fr, cat, n):
    return {"name": fr}, {"category": cat, "imdb_id": f"tt{n}", "title": f"t{n}", "year": 2000}


class ChunksTest(unittest.TestCase):
    def test_four_works_per_question_one_category(self):
        # owner 2026-10-27: «по 4 вопроса с 4 вариантами … итого 16 медиа»; films and series never in one question
        q = [item("A", "фильм", 1), item("B", "фильм", 2), item("A", "фильм", 3), item("A", "сериал", 4)]
        q += [item("C", "фильм", n) for n in range(10, 16)]
        parts = seen_survey.chunks(q)
        for p in parts:
            self.assertEqual(len({w["category"] for _, w in p}), 1)
            self.assertLessEqual(len(p), 4)
        self.assertEqual(sum(len(p) for p in parts), len(q))
        self.assertEqual([len(p) for p in parts], [4, 4, 1, 1])


if __name__ == "__main__":
    unittest.main()
