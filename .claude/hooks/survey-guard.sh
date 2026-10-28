#!/usr/bin/env bash
# PreToolUse guard for AskUserQuestion — survey rules the owner had to repeat (2026-10-23 / 2026-10-24):
#  * a series is asked per season only: an option about a series/cartoon series must name the season;
#  * never «all or nothing» about seasons («Видел все» / «Не видел ни одного» / «Только 1-й сезон»);
#  * do not mix films and series in one question;
#  * ratings in questions: IMDb only (no TMDB rating);
#  * the word «ранние части» confused the owner — say «предыдущий фильм серии».
input=$(cat)
printf '%s' "$input" | python -X utf8 -c '
import json, re, sys
d = json.load(sys.stdin)
if d.get("tool_name") != "AskUserQuestion":
    sys.exit(0)
qs = (d.get("tool_input") or {}).get("questions") or []
def deny(why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": why}}, ensure_ascii=False))
    sys.exit(0)
# a survey goes 4 questions per call while questions remain (owner, 2026-10-27: «Почему вопросы не обеденны в группу из 4
# вопросов. Ты же можешь задать 4 вопроса за раз. Опять баг правь» — after «одна франшиза один вопрос» the franchises were
# sent one call each; the rule is one franchise per QUESTION, four questions per call): a call with fewer than 4 questions
# is allowed only when its last question is the last of the survey (N = M) — a re-asked question may go with the tail.
mlast = re.match(r"\s*(\d+)\s*(?:/|из)\s*(\d+)", (qs[-1].get("question", "") if qs else ""))
if mlast and 0 < len(qs) < 4 and int(mlast.group(1)) < int(mlast.group(2)):
    deny(f"Опрос — по 4 вопроса за вызов (владелец, 2026-10-27: «Ты же можешь задать 4 вопроса за раз. Опять баг правь»): "
         f"после вопроса {mlast.group(1)}/{mlast.group(2)} вопросы ещё есть — в вызове нужно 4 вопроса, по одной франшизе / "
         f"одному предмету в каждом.")
for q in qs:
    text = q.get("question", "")
    opts = q.get("options") or []
    # every work option of a «что видел» question carries its rating (owner, 2026-10-27: «И у тебя баг нет оценки. А она
    # при опросе должна быть всегда» — the rating was only in the question text, the season options had none)
    if re.search(r"видел", text, re.I):
        for o in opts:
            lab = o.get("label", "")
            if re.search(r"\((19|20)\d\d\)", lab) and not re.search(r"IMDb", o.get("description", "")):
                deny("У каждого произведения в вопросе — оценка IMDb в описании варианта (владелец, 2026-10-27: «И у тебя баг нет "
                     "оценки. А она при опросе должна быть всегда»): «" + lab + "».")
    # a franchise name with spin-offs «Пацаны (+ Дьявольские, Поколение V)» read as three franchises in one question
    # (owner, 2026-10-27: «Я думал что поколение v это не часть пацанов. Я же не разбираюсь в том что не видел»)
    if re.search(r"\(\s*\+|\S\s\+\s\S", text) and not re.search(r"спин-?офф|ответвлени|т(от|ой) же (мир|вселенн)|часть (франшиз|вселенн)|из того же мира|одна франшиза", text, re.I):
        deny("Франшиза со спин-оффами «X (+ Y, Z)» выглядит как несколько франшиз (владелец, 2026-10-27: «Я думал что поколение v это "
             "не часть пацанов. Я же не разбираюсь в том что не видел»): одной фразой объяснить, что Y, Z — спин-оффы X, тот же мир.")
    blob = text + " " + " ".join(o.get("label", "") + " " + o.get("description", "") for o in opts)
    if re.search(r"https?://|www\.|docs\.google|youtu\.be|youtube\.com", blob, re.I):
        deny("Ссылки в вопросе не кликабельны (владелец с телефона, 2026-10-24): дай ссылку гиперссылкой [текст](url) в итоговом сообщении хода (текст перед вопросом с телефона тоже не виден — «Не вижу ссылки»), а вопрос по ней задай позже.")
    if not re.search(r"^\s*\d+\s*(/|из)\s*\d+", text):
        deny("В каждом вопросе — прогресс «N из M» (владелец, 2026-10-24: «Нарушение правил. Не видно сколько из скольки»): "
             "в начале текста вопроса, напр. «5/22 · …» (скил owner-survey, «Формат»).")
    for o in opts:
        if re.search(r"[×x]\s*\d", o.get("label", "")) or (o.get("description", "").count("→") > 1) or len(re.findall(r"\((19|20)\d\d\)", o.get("label", ""))) > 1:
            deny("Один фильм — один вариант, не группами (владелец, 2026-10-24: «1 фильм один вариант. Что за экономия»).")
    # one media — one question (owner, 2026-10-25: «Опять забыл, что один медиа один вопрос?»): no bundle options
    for o in opts:
        lab = o.get("label", "")
        if re.search(r"\b(за\s+)?(всеми|все|всех)\s+\d+|\bза\s+всеми\b|по\s+каждому|только\s+фильмы|\bвсе\s+(мульт|фильм|сериал|сезон)", lab, re.I):
            deny("Один медиа — один вопрос (владелец, 2026-10-25: «Опять забыл, что один медиа один вопрос?»): без вариантов-пачек "
                 "(«за всеми 16», «спроси по каждому», «только фильмы») — про каждое произведение свой вопрос «Следить / Не следить».")
    if re.search(r"\bещё\s+\d+\s+(мульт|фильм|сериал|произвед|короткометраж)", text, re.I):
        deny("Один медиа — один вопрос (владелец, 2026-10-25): не спрашивать про «ещё N фильмов» разом — по вопросу на каждое произведение.")
    # clarity (owner, 2026-10-25): «база» must say where — on the computer or on GitHub (a prompt hook here stopped the whole
    # turn on every refusal: «Опять остановился. Баг скила»; this check only refuses the question)
    if re.search(r"\bбаз[аеуыо]й?\b", blob, re.I) and not re.search(r"компьютер|GitHub|гитхаб", blob, re.I):
        deny("Слово «база» без уточнения: «база на компьютере» или «копия на GitHub» (владелец, 2026-10-25).")
    if re.search(r"ранни[ех] част", blob, re.I):
        deny("Не писать «ранние части» — владелец не понял термин: «предыдущий фильм серии» + пример (2026-10-24).")
    if re.search(r"TMDB\s*\d|TMDB[^.]{0,20}рейтинг|рейтинг[^.]{0,20}TMDB", blob, re.I):
        deny("В вопросах владельцу — рейтинг только IMDb, не TMDB (2026-10-23).")
    if re.search(r"сезон", blob, re.I) and any(re.fullmatch(r"\s*(Видел все|Не видел ни одного|Только 1-й сезон|Видел всё)\s*", o.get("label", ""), re.I) for o in opts):
        deny("Сезоны — только галочками (multiSelect «что из этого ты видел», по 4 сезона), не «всё или ничего» (владелец, 2026-10-24).")
    cats = set()
    for o in opts:
        m = re.match(r"\s*(мультсериал|сериал|мультфильм|фильм|аниме)\b", o.get("description", ""), re.I)
        if m:
            c = m.group(1).lower()
            cats.add("series" if "сериал" in c else "film")
            if "сериал" in c and not re.search(r"сезон", o.get("label", "") + o.get("description", ""), re.I):
                deny("Сериал спрашивать только по сезонам: в варианте должен быть сезон (владелец, 2026-10-23).")
    if len(cats) > 1:
        deny("Не смешивать фильмы и сериалы в одном вопросе (владелец, 2026-10-23).")
sys.exit(0)
'
exit 0
