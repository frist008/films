"""Hook survey-guard.sh: a survey goes four questions per call, one franchise (one subject) per question (owner,
2026-10-27: «одна франшиза один вопрос», then «Почему вопросы не обеденны в группу из 4 вопросов. Ты же можешь задать 4
вопроса за раз. Опять баг правь»)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "survey-guard.sh"
OPTS = [{"label": "Интересны", "description": "спрошу, что видели"}, {"label": "Не интересны", "description": "скрыть"}]


def denied(questions):
    payload = json.dumps({"tool_name": "AskUserQuestion", "tool_input": {"questions": questions}}, ensure_ascii=False)
    out = subprocess.run(["bash", str(HOOK)], input=payload, capture_output=True, text=True, encoding="utf-8").stdout
    return '"deny"' in out


def q(n, total, text):
    return {"question": f"{n}/{total} · {text}", "header": "Опрос", "multiSelect": False, "options": OPTS}


@unittest.skipUnless(shutil.which("bash"), "bash is needed for the hook")
class FourPerCallTest(unittest.TestCase):
    def test_one_question_while_more_remain_denied(self):
        self.assertTrue(denied([q(1, 9, "Франшиза «Маска»: мультфильмы интересны?")]))

    def test_four_franchises_one_per_question_allowed(self):
        self.assertFalse(denied([q(n, 9, f"Франшиза №{n}: мультфильмы интересны?") for n in range(1, 5)]))

    def test_tail_of_the_survey_allowed(self):
        self.assertFalse(denied([q(9, 9, "Франшиза «Мэдея»: мультфильмы интересны?")]))
        self.assertFalse(denied([q(8, 9, "Франшиза №8"), q(9, 9, "Франшиза №9")]))
        # a re-asked question («Что это?») goes with the tail
        self.assertFalse(denied([q(5, 9, "Франшиза №5 ещё раз"), q(8, 9, "Франшиза №8"), q(9, 9, "Франшиза №9")]))

    def test_short_call_in_the_middle_denied(self):
        self.assertTrue(denied([q(4, 9, "Франшиза №4"), q(5, 9, "Франшиза №5")]))

    def test_spinoffs_without_explanation_denied(self):
        self.assertTrue(denied([q(9, 9, "Франшиза «Пацаны» (+ «Дьявольские», «Поколение V»): мультфильмы интересны?")]))
        self.assertFalse(denied([q(9, 9, "Франшиза «Пацаны» (+ «Дьявольские», «Поколение V» — спин-оффы «Пацанов», тот же мир): "
                                         "мультфильмы интересны?")]))

    def test_plus_in_franchise_name_needs_explanation(self):
        self.assertTrue(denied([q(9, 9, "Франшиза «Легенды «Смертельной битвы» + Мортал Комбат» — фильмы. Что видел?")]))
        self.assertFalse(denied([q(9, 9, "Франшиза «Легенды «Смертельной битвы» + Мортал Комбат» (всё это одна франшиза, "
                                         "тот же мир) — фильмы. Что видел?")]))

    def test_rating_in_every_work_option(self):
        def seen_q(desc):
            return {"question": "9/9 · Франшиза «Cuphead». Что из этого ты видел?", "header": "x", "multiSelect": True,
                    "options": [{"label": "Шоу Чашека! — сезон 1 (2022)", "description": desc},
                                {"label": "Не видел", "description": "Не отмечать"}]}
        self.assertTrue(denied([seen_q("мультсериал · сезон 1, 12 серий")]))
        self.assertFalse(denied([seen_q("мультсериал · сезон 1, 12 серий · IMDb 7.4")]))

    def test_single_question_survey_allowed(self):
        self.assertFalse(denied([q(1, 1, "Расписание переноса — как запускать?")]))


if __name__ == "__main__":
    unittest.main()
