# IMDb: технические характеристики, IMAX, бюджет/сборы, постеры — без браузера

Дата: 2026-10-23. Модуль: `films/sources/imdb_specs.py`, тесты: `tests/test_imdb_specs.py` (33 теста),
сырой кэш: `data/cache/imdb_specs/{tconst}.json.gz`, отчёт последнего прогона: `data/cache/imdb_specs/last_run.json`.

## 1. Доступ: GraphQL IMDb по обычному HTTP

Страницы `www.imdb.com/title/…` для curl закрыты AWS WAF (HTTP 202 + JS-challenge). Но собственный GraphQL API IMDb
(его вызывает сайт и приложения) отвечает на обычный HTTP **без cookies, токенов и challenge** — нужен только
заголовок клиента, который шлёт сайт:

```
POST https://caching.graphql.imdb.com/          (api.graphql.imdb.com и graphql.prod.api.imdb.a2z.com — так же)
Content-Type: application/json
x-imdb-client-name: imdb-web-next               (без него → 403; вместо него хватает и Referer: https://www.imdb.com/)
{"query": "query($ids: [ID!]!) { titles(ids: $ids) { id technicalSpecifications { … } … } }",
 "variables": {"ids": ["tt15239678", …]}}
```

GET с `?query=…` тоже работает. Пакетный запрос `titles(ids: [...])` — проверено до 100 id за запрос (6 с);
модуль берёт по 25, не чаще 1 запроса/с, повтор с экспоненциальной паузой (до 6 попыток). Полный прогон по
~4000 id — ~160 запросов, ~6 минут. Интроспекция схемы закрыта («Unauthorized introspection»), поля подобраны
по сообщениям валидатора. WAF/captcha ничем не обходятся — этот хост их просто не ставит.

**Оговорка IMDb.** В каждом ответе: «Public, commercial, and/or non-private use of the IMDb data provided by this API
is not allowed». Личная база — ок. **Но** по плану (Этап 11) БД уходит в **открытый** GitHub — данные из этого API
(и сырой кэш) туда класть нельзя без решения владельца (кэш уже в `.gitignore` как `data/cache/`, а колонки `titles` —
в `films.db`). Вопрос владельцу.

Что лежит в сыром кэше (весь узел GraphQL, чтобы новые поля доставать без повторных запросов): тех. характеристики
(соотношения сторон, звук, процессы, камеры, плёнка негатива, формат копий, лаборатории, цвет, длина плёнки),
бюджет, сборы (мир / США), премьерный уик-энд, постер, aka (все страны), даты выхода (все страны), ключевые слова,
места съёмок, компании (производство / дистрибуция / VFX / продажи), страны, языки, возрастной рейтинг,
рейтинг+голоса, сезоны, `meta.canonicalId`.

## 2. Запуск

```bash
python -X utf8 -m films.sources.imdb_specs run                       # все тайтлы с IMDb id (кэш до 30 дней)
python -X utf8 -m films.sources.imdb_specs run --limit 50            # первые 50 IMDb id (по titles.id)
python -X utf8 -m films.sources.imdb_specs run --max-age-days 0      # перезапросить всё
python -X utf8 -m films.sources.imdb_specs run --ids tt15239678 …    # только эти
python -X utf8 -m films.sources.imdb_specs run --no-fetch            # только из кэша
python -X utf8 -m films.sources.imdb_specs show tt15239678           # разбор без записи в БД
python -X utf8 -m unittest tests.test_imdb_specs
```

Фильм — `titles.imdb_id`; сезон — `titles.series_imdb_id` (у IMDb нет сущности «сезон»: тех. характеристики,
постер — уровня сериала и пишутся в каждую строку сезона). Пишутся только колонки:
`di_resolution`, `aspect_ratios`, `sound_mix`, `is_imax`, `imax_source` (перезаписываются при каждом прогоне);
`upscale_verdict` / `upscale_source = 'imdb_di'` из `di_resolution` (R32; не трогается, если вердикт дал другой источник —
см. `docs/research-codecs-upscale.md` §2.5);
`poster_url`/`poster_source='imdb'`/`poster_checked_at` — только где `poster_url IS NULL` (нет у IMDb — дальше RT → TMDB →
трекер, см. «Постер» ниже; если у IMDb постера нет —
ставится только `poster_checked_at`; строка-«фильм», чей IMDb id — сериал/эпизод **той же** работы с тем же названием
и годом ±1, получает постер, хотя тех. характеристики ей не пишутся — `poster_same_work`);
`budget_usd`/`box_office_usd` — только где NULL, только USD, только фильмы. `updated_at` не трогается.
Каждая группа строк одного tconst — отдельная короткая транзакция (`BEGIN IMMEDIATE`, busy_timeout из `db.connect()`).

Защиты:
- **Подозрительная связь** (`suspect_link`): IMDb id указывает на эпизод (`tvEpisode`), строка-сериал → не сериал,
  строка-фильм → сериал, или год фильма расходится > 1 → в строку **ничего не пишется**, только в отчёт.
- **Склеенные id** (IMDb объединил дубли): `meta.canonicalId ≠ id` → дозапрашивается канонический, данные берутся
  оттуда; `titles.imdb_id` не меняется, пара попадает в отчёт `redirects`.
- `total` у каждого списка тех. характеристик сверяется с числом элементов (`spec_truncated`) — обрезаний не было.
- Значения, расходящиеся с уже записанными (TMDB) бюджетом/сборами, не перезаписываются — в отчёт `money_disagree`.

## 3. Поля

### `aspect_ratios`, `sound_mix`
JSON-список строк в виде, как их показывает IMDb: `"1.43 : 1 (IMAX 70mm & Dual Laser, some scenes)"`, `"2.39 : 1"`,
`"IMAX 6-Track"`, `"DTS (DTS: X)"`. NULL — у IMDb пусто.

### `di_resolution` — мастер-формат (Digital Intermediate)
Берутся записи процесса «Digital Intermediate». Атрибуты, введённые одной строкой через запятую («4K, 2024 remaster»),
делятся. Значения: `SD` (480p/576p), `HD` (720p/1080p/HD), `2K` (2K…<3.8K, а также «upscaled 4K»),
`4K`, либо литерал выше (`4.5K`, `4.6K`, `6.5K`, `8K`).
Приоритет записей:
1. ремастер/реставрация («2018 remaster», «2025 restored version», «remastered version») — это мастер нынешних
   4K/HD-релизов; Cowboys & Aliens: «2K, master format» + «4K, 2024 remaster» → 4K;
2. записи без ограничений;
3. ограниченные («some scenes», «… scenes only», «IMAX version», «mainland China and Hong Kong versions», «3-D»…)
   — сами по себе → NULL (`partial_only`). The Substance: «2K, master format» + «4K, master format, TV show scenes only» → **2K**.
NULL также если: нет записи DI (`no_di`), DI без разрешения (`di_without_resolution` — Furiosa, Inside Out 2, Companion),
одна запись с двумя классами «2K, 4K» (`ambiguous_entry` — Criminal Minds, 9-1-1), полные записи расходятся (`conflict`).

### `is_imax`, `imax_source` — правило владельца «снят на IMAX-камеры или шёл в IMAX»
`is_imax = 1`, если слово **IMAX** (целым словом, не «non-IMAX») встречается в любом списке тех. характеристик,
после вырезания домашних IMAX-брендов («Disney+ IMAX Enhanced Version», «IMAX Blu-ray & 4K UHD»).
`imax_source` — все сработавшие источники через запятую, от сильного к слабому:

| источник | что значит | примеры текста IMDb |
|---|---|---|
| `imdb_camera` | снят (частично) IMAX-камерами или «Filmed for IMAX»-сертифицированными | `IMAX MSM 9802, …`, `IMAX MKIV … (some scenes)`, `Arri Alexa Mini LF IMAX`, `Arri Alexa 65 IMAX` |
| `imdb_negative` | негатив IMAX | `65 mm (IMAX)` |
| `imdb_process` | процесс IMAX / IMAX DMR | `IMAX (source format)`, `IMAX Digital 3-D`, `Digital Intermediate (… IMAX and Cinity masters)` |
| `imdb_printed` | копии для IMAX-залов | `70 mm (also IMAX DMR blow-up)`, `IMAX Digital`, `IMAX Laser`, `D-Cinema (upscaled 4K DCP, IMAX version)` |
| `imdb_aspect` | отдельный кадр для IMAX | `1.43 : 1 (IMAX 70mm & Dual Laser…)`, `1.90 : 1 (IMAX version)`, `1.85 : 1 (3-D version … including IMAX)` |
| `imdb_sound` | IMAX-сведение звука (бывает только у IMAX-проката) | `IMAX 6-Track`, `IMAX 5.0`, `12-Track Digital Sound (IMAX)`, `Sonics-DDP (IMAX version)` |
| `imdb_lab` | лаборатория печатала IMAX-копии | `… (IMAX prints)` |
| `imdb_company` | (2026-10-25) не тех. характеристики, а «Other companies»: компания IMAX (`co0057495`) с пометкой «optimized for IMAX theatres» — IMAX DMR-ремастер к прокату | Inside Out 2, Frozen II, The Wild Robot, Toy Story 5, Wolfs |
| `imdb_release` | (2026-10-25) дата проката с пометкой `IMAX` / `IMAX version` / `IMAX 3D` не позже года фильма + 1 | Moana 2, Despicable Me 4, Wolf Man, Mad Max: Fury Road |

У мультфильмов (Головоломка 2, Моана 2, Дикий робот…) в тех. характеристиках IMAX нет никогда — его видно только
по `imdb_company` / `imdb_release` из того же ответа GraphQL (`node_imax`). Перевыпуски («IMAX re-release», Матрица
2021, Marvel 2018), фестивальные/фанатские показы, премьеры и IMAX как дистрибьютор перевыпуска — **не считаются**,
только в `imax_notes`. «Зверополис 2», «Элио» — у IMDb нет ни одного признака, `is_imax = 0`.

`is_imax = 0` — у IMDb есть тех. характеристики (кадр/звук/процесс/камеры/негатив/копии), но IMAX нигде нет.
`is_imax = NULL` — тех. характеристик нет вовсе (цвет «Color» не считается) — **неизвестно**, а не «не IMAX».
Не считаются IMAX (только в отчёт `imax_notes`): голые `1.90 : 1` / `1.43 : 1` без слова IMAX (Woody Woodpecker Goes
to Camp, The Damned, 96 Minutes, X, Terrifier…) и «IMAX Enhanced»/«IMAX Blu-ray» без театрального IMAX.

Примеры (проверено на 54 тайтлах, см. §5):
- **IMAX = 1**: Dune: Part Two (`imdb_printed,imdb_aspect,imdb_sound`), Oppenheimer (все 5), Sinners (IMAX 65 mm,
  все 5), The Odyssey 2026 (`imdb_camera,imdb_process,…`), The Fantastic Four: First Steps, Captain America: BNW,
  Venom: The Last Dance (камеры «… LF IMAX»), Superman, F1, Mission: Impossible – The Final Reckoning, Thunderbolts*,
  Weapons, Alien: Romulus (`imdb_aspect,imdb_sound`), Gladiator II, Twisters, Mickey 17 (`imdb_printed,…`),
  Wicked, Deadpool & Wolverine, Furiosa, Civil War, Kingdom of the Planet of the Apes (только `imdb_sound`).
- **IMAX = 0**: The Substance, Anora, Conclave, Heretic, Longlegs, Nosferatu, A Complete Unknown, Bugonia, Hamnet,
  Marty Supreme, Nickel Boys, The Monkey, Materialists, Sorry, Baby, Smile 2; сериалы Shōgun, The Last of Us, The Penguin.
- **Слабое место**: «0» = «IMDb не упоминает IMAX». У Inside Out 2, Moana 2, Zootopia 2, Lilo & Stitch, The Wild Robot,
  Elio — 0, хотя крупные анимационные/диснеевские фильмы часто шли и в IMAX: пользовательские данные IMDb неполны.
  Положительные только по звуку (`imdb_sound`, 93 строки из 208) — самые слабые: например Companion и The Brutalist
  помечены «IMAX 6-Track»; подтвердить вторым источником не удалось.

### Бюджет и мировые сборы
`productionBudget.budget` и `lifetimeGross(boxOfficeArea: WORLDWIDE)`. Мировые сборы у IMDb всегда в USD; бюджет —
в валюте источника (EUR 78, RUR 10, CAD 8, AUD 7, GBP 6, JPY 6, KRW 5, INR 5 …, всего 153 строки) → **не пишется**,
список с суммами в `last_run.json → non_usd`. Сезоны — не заполняются (бюджет/сборы IMDb уровня сериала не описывают
сезон): бюджет есть у 80 строк-сезонов, сборы у 28.

### Постер
`primaryImage.url` из того же ответа, нормализованный `imdb.poster_url()` (`…._V1_.jpg`). Сверено с suggestion-API
(`v3.sg.media-imdb.com/suggestion/x/{tconst}.json`, `imdb.suggest()`): 15/15 случайных — тот же URL, так что
отдельный запрос не нужен. Сезонам ставится постер сериала.

**Порядок источников постера** (владелец, 2026-10-25: «Да, TMDB — когда на IMDb и RT нет; а когда и там нет — с источников
торрента»): **IMDb → Rotten Tomatoes → TMDB → картинка со страницы раздачи**. Код — `films/posters.py`; полный прогон
`imdb_specs run` (без `--ids` / `--limit`) в конце сам вызывает `posters.fill()` (итог — `last_run.json → poster_fallback`);
отдельно: `python -X utf8 -m films.posters run [--ids …] [--no-check] [--dry]`, сводка `python -X utf8 -m films.posters stats`.
- Цели: тайтлы с раздачей (связь не `wrong_link…`, строка не `duplicate_of`), у которых `poster_url IS NULL`, плюс все
  с `poster_source` `tracker` или `tmdb` (появившийся позже постер IMDb / RT заменяет их — страницы RT приходят с
  `films ratings rt`).
- `imdb` — тот же `primaryImage` (кэш GraphQL, с теми же защитами `suspect_link` / `poster_same_work`); `rt` —
  `rt_pages.poster_url` страницы тайтла (сезон → его страница, иначе страница сериала; заполняет `films ratings rt`);
  `tmdb` — `poster_path` (сезон → постер сезона, иначе сериала) как `https://image.tmdb.org/t/p/original/…`;
  `tracker` — первая картинка описания раздачи из `release_raw` (уже скачано; значки рейтингов `s.rutor.info/imdb/*.gif`
  и gif пропускаются, **картинки Кинопоиска не берутся никогда**; ссылка на большую картинку вокруг превью — берётся она;
  у rutracker — `<var class="postImg" title=…>`).
- Ссылки трекеров протухают (картинки живут на imageban / fastpic …), поэтому картинка пишется, только если отвечает
  (HTTP 200, `image/*`); в `titles.poster_release_id` — раздача, чья страница её дала, в `poster_alive_at` — когда она
  отвечала. **Каждое обновление перепроверяет** такие строки: сначала IMDb / RT / TMDB, потом сама картинка; мёртвая —
  берётся заново из кэша страниц других раздач тайтла, затем со свежей страницы rutor (обычный HTTP, только rutor, без
  браузера); не нашлось — `poster_url` очищается (ищется снова в следующий раз), строка — в отчёте `none_list` с причиной.
- `poster_source`: `imdb` / `rt` / `tmdb` / `tracker`.

### Украинское название (только отчёт, `title_uk` не пишется)
aka с `country.id = 'UA'` — см. §4.

### Опрос метаданных (владелец, 2026-10-25): дистрибьютор, цвет, камеры, формат плёнки, места съёмок

Тот же запрос; добавлен псевдоним `distributors: companyCredits(first: 250, filter: { categories: ["distribution"] })` —
только дистрибуция (общий `companyCredits(first: 50)` обрезал её на крупных фильмах), и `filmingLocations(first: 25)`.
Кэш без `distributors` перезапрашивается сам (`REQUIRED_NODE_KEYS`). Колонки `titles` (перезаписываются каждым прогоном,
сезонам — данные сериала):

| колонка | откуда | в таблице |
|---|---|---|
| `distributor`, `distributor_country` | `pick_distributor()` — см. ниже | да, «Дистрибьютор»: «Warner Bros. (США)» |
| `distributors` | JSON всех записей дистрибуции `{name, country, attrs}` | нет |
| `color` | `colorations`: `Color` / `Black and White` / `Color \| Black and White (Infrared)` (как у IMDb, через « \| ») | нет; по нему rank ставит цвет выше ч/б и метку «ч/б» |
| `cameras` | JSON списка камер (`Arri Alexa 65, Panavision …`) | нет |
| `film_format` | JSON `{negative, printed, process}` — форматы негатива, копий, процессы | нет |
| `filming_locations` | JSON до 25 мест съёмок | нет |

**Выбор дистрибьютора** (владелец: «украинский или мировой, если можно понять, иначе американский»). Записи домашнего видео
(DVD / Blu-ray / VHS) пропускаются — это не правообладатель для зрителя. Затем: 1) запись с страной **UA** (сначала
theatrical); 2) **XWW** (весь мир) — только если её держит **одна** компания (оригиналы Netflix / Apple / Amazon); несколько
компаний на XWW — это витрины VOD (у «Дюны 2»: Amazon Prime Video, Apple TV+, HBO Max), такая запись не годится; 3) **US**:
theatrical → TV → VOD → любая; 4) первая theatrical-запись любой страны, иначе первая запись (российские / белорусские —
только если других нет: владелец в Украине). Правило (`ua` / `xww` / `us` /
`first`) возвращается для отчёта; страна пишется в `distributor_country`.

## 4. Результат полного прогона (2026-10-23)

4098 IMDb id, 4538 строк `titles` (2929 фильмов, 1609 сезонов); ошибок запросов 0, не найдено 0, склеенных id 20.

| поле | фильмы (2929) | сезоны (1609) |
|---|---|---|
| `di_resolution` | 872 (30 %) | 434 (27 %) |
| `is_imax` = 1 / 0 / NULL | 208 / 2035 / 686 | 0 / 1106 / 503 |
| `aspect_ratios` | 2094 (71 %) | 953 (59 %) |
| `sound_mix` | 1342 (46 %) | 1003 (62 %) |
| постер (было 0) | 2915 | 1598 |
| бюджет: дописано из IMDb | 117 (+153 не-USD не записаны) | — |
| мировые сборы: дописано из IMDb | 725 | — |

`di_resolution` по значениям: 4K 1085, 2K 208, HD 7, SD 1, выше 4K 5 строк. IMAX у фильмов 2024/2025/2026: 40/40/23
(тех. характеристик нет у 304/258/100 из 1142/904/389).

- Без постера 9 строк (у IMDb нет картинки): Fatal Game, 我的差评女友, Край чудес, Zui Qiang Shengji, Папа, купи пёсика,
  Cosmicrew: Ice Planet, Carrie and Super Kola the School, Otherworldly Evil Monarch, Zhivoy garazh.
- **Подозрительные связи — 16 строк, не записано ничего** (нужна правка сопоставления, вне этого модуля):
  - id эпизода вместо сериала/фильма: Fallout (tt32158505 = эпизод «The Innovator»), Peacemaker (tt18263680),
    The Walking Dead: Dead City ×2 (tt28469154), Chad Powers (tt1449991 = эпизод 1980), The Fantastic Four: First Steps
    (id 1320 → tt1054216, эпизод 1994; правильный tt10676052 уже есть у id 1256), Jason Voorhees: The Hunt / Awaken
    the Darkness (фан-фильмы, IMDb ведёт как эпизоды);
  - год расходится > 1: Vertical (2021 ↔ 2024), Savage (2024 ↔ 2026), Incubus: New Beginnings (2023 ↔ 2025),
    Stay Safe (2023 ↔ 2025), El Jardin (2020 ↔ 2024), Vindication Swim (2022 ↔ 2024), Contagion of Fear (2023 ↔
    «Event Zero» 2017 после склейки id), Over the Garden Wall (2016 ↔ короткометражка-трибьют 2024).
- DI не определён по спорным записям: Criminal Minds, 9-1-1 («2K, 4K»), LOTR: Fellowship, Gone in 60 Seconds,
  The Punisher (только «2K, some scenes»).
- Бюджет IMDb ≠ TMDB у 123 фильмов (медиана отношения 1.01; > 25 % — 53, напр. Deadpool & Wolverine 200M в БД vs
  537M в IMDb), сборы ≠ у 730 (медиана 1.011; > 25 % — 170). Значения БД оставлены; список — `money_disagree`.
- **Украинские названия**: без `title_uk` — 1848 строк; UA-aka на IMDb есть у **105 строк (96 id; 43 фильма,
  62 сезона)**, из них 94 есть и в датасете `imdb_akas` (region UA) — т.е. GraphQL почти ничего не добавляет к датасету.
  Примеры: 얼굴 → «Без обличчя» / «Потворний» (два варианта!), Sew Torn → «Зшити розірване», Broken Rage →
  «Зламана лють», 악마가 이사왔다 → «Диявол переїхав» / «Достатньо божевільна». У части тайтлов несколько UA-aka —
  автоматически выбирать нельзя. Полный список — `last_run.json → uk_candidates_all`.

## 5. Проверка (54 tconst, 55 строк)

Сверены с известными фактами/данными IMDb: IMAX-фильмы (Dune 2, Oppenheimer, Sinners, The Odyssey, F1, Superman,
MI8, Thunderbolts*, Avatar: Fire and Ash, Cap4, F4, OBAA, GxK, Twisters, Gladiator II, Furiosa, Wicked,
D&W, Joker 2, Weapons, Mickey 17, Alien: Romulus, KotPotA, Venom 3, Civil War) — все 1; инди/фестивальные (Anora,
Conclave, Heretic, Longlegs, Nosferatu, Hamnet, Nickel Boys, Sorry, Baby…) — 0; DI: The Substance = 2K (как в
`docs/research-codecs-upscale.md`), Dune 2 / Sinners / Superman … = 4K, Furiosa / Inside Out 2 / Companion / OBAA —
нет разрешения DI (совпадает с исследованием); бюджет Dune 2 = $190M, сборы $714.8M. Постеры: 15/15 = suggestion-API.
Найдено попутно: строка 1320 связана с чужим id (см. §4).

## 6. Предложения (схема / конфиг) — в `films/db.py` не вносил

```sql
ALTER TABLE titles ADD COLUMN imdb_specs_checked_at TEXT;   -- когда проверены тех. характеристики (сейчас — mtime кэша)
ALTER TABLE titles ADD COLUMN imax_evidence TEXT;           -- JSON строк-доказательств (сейчас только в отчёте/кэше)
ALTER TABLE titles ADD COLUMN di_note TEXT;                 -- ok / ok_remaster / no_di / di_without_resolution / partial_only / conflict
ALTER TABLE titles ADD COLUMN budget_currency TEXT;          -- если хранить не-USD бюджеты (153 строки) как есть
ALTER TABLE titles ADD COLUMN budget_raw INTEGER;
-- кэш как таблица вместо файлов (как tmdb_raw): imdb_gql_raw(tconst PK, fetched_at, status, canonical, body BLOB)
```
Конфиг `config/settings.toml [imdb_specs]`: `batch = 25`, `min_interval_s = 1.0`, `max_age_days = 30`.
CLAUDE.md: в таблице источников для IMDb тех. характеристик Chrome больше не нужен — GraphQL по HTTP
(правку не вносил — вне моих файлов).

## 7. Повторяющиеся метаданные IMDb, которых нет в схеме (спросить владельца по каждому отдельно)

Ответы опроса 2026-10-25: камеры и формат плёнки (1, 3, 4) — хранить; цвет (6) — хранить (`titles.color`); дистрибьютор (8) —
в таблицу; места съёмок (9) — хранить; ключевые слова берутся из TMDB (в таблицу); языки (11) — из TMDB, хранить; сборы в США,
дата выхода в Украине, возрастной рейтинг — не нужны.

Доступны в том же ответе (покрытие по 3845 id в кэше):
1. Камеры (37 %) — «Arri Alexa 65», «IMAX MKIV»…
2. Кинопроцесс / исходный формат (33 %) — «ARRIRAW (6.5K, source format)», «Dolby Vision».
3. Формат негатива (28 %) — «Codex», «35 mm», «65 mm».
4. Формат копий (38 %) — «70 mm», «DCP».
5. Лаборатории (19 %).
6. Цвет (99 %) — в т.ч. ч/б (49 тайтлов «Black and White»).
7. Кассовые сборы в США и премьерный уик-энд (21 % / 20 %) — в очереди вопросов уже есть «сборы в США».
8. Дистрибьюторы / производящие компании / VFX-студии / продажи (98 %; 34 787 записей дистрибуции) — «Дистрибьютор» уже в очереди.
9. Ключевые слова (84 %), места съёмок (62 %).
10. Даты выхода по странам (100 %; Украина — 14 %), в т.ч. пометки «IMAX fan event», «premiere».
11. Языки (96 %) — «язык оригинала» уже в очереди; страны происхождения (есть из TMDB).
12. Возрастной рейтинг (58 %) — владелец уже отказался.
