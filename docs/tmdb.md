# TMDB: метаданные, RU/UK названия, трейлеры, коллекции

Реализация от 2026-10-23. Модуль: `films/sources/tmdb.py`, тесты: `tests/test_tmdb.py`.
Кэш: таблица `tmdb_raw` в `data/films.db` (миграция 5), сжатый zlib JSON.

## Доступ

- TMDB API v3, `https://api.themoviedb.org/3`, обычный HTTPS (`requests`), без браузера.
- Ключ — `TMDB_API_KEY` из `.env` (или переменной окружения), читается при запуске, передаётся параметром
  `api_key`. Нигде не печатается и не пишется: в кэше хранится только тело ответа (проверено — 0 строк из 8773
  содержат ключ), тексты ошибок очищаются от ключа (`***`).
- Скорость: не больше 20 запросов/с на процесс (общий лимитер, потокобезопасный; TMDB допускает ~40–50/с).
  429 и 5xx, сетевые ошибки — до 6 попыток с экспоненциальной паузой, `Retry-After` учитывается.
- 401 / 422 и прочие 4xx (ошибка нашего запроса) — исключение `TmdbError`, в кэш не пишутся.

## Кэш `tmdb_raw`

| kind | key | lang | запрос |
|---|---|---|---|
| `find` | IMDb id | `''` | `/find/{imdb}?external_source=imdb_id` |
| `movie` | tmdb id | `ru-RU` | `/movie/{id}` + `append_to_response=credits,release_dates,videos,keywords,external_ids,translations,alternative_titles`, `include_video_language=ru,uk,en` |
| `tv` | tmdb id | `ru-RU` | `/tv/{id}` + `external_ids,keywords,translations,videos,alternative_titles` |
| `season` | `tv_id/N` | `ru-RU` | `/tv/{id}/season/{N}` + `aggregate_credits,videos,external_ids` |
| `collection` | id | `ru-RU` | `/collection/{id}` + `translations` |
| `person` | id | `ru-RU` | `/person/{id}` + `external_ids,translations` |
| `search` | `movie|год|запрос` / `tv|год|запрос` | `ru-RU` / `en-US` | `/search/movie`, `/search/tv` |
| `config` | `countries` | `ru-RU` | `/configuration/countries` (русские названия стран) |

- Каждый ответ 200 и 404 пишется в кэш (404 — чтобы не повторять вслепую) и сначала читается оттуда.
- `max_age_days` (по умолчанию 30; 0 = перезапросить) — когда строка кэша устарела. Кэшированный ответ без
  какого-то из добавленных `append_to_response` (если список в коде расширили) считается устаревшим сам.
- `tmdb.OFFLINE = True` / `--offline` — только кэш, промах = `TmdbError`.
- Запись: одна короткая транзакция на ответ. Если вызывающий уже открыл транзакцию на этом соединении, модуль
  не коммитит за него.
- Размер: ~12 КБ на фильм или сезон (переводы и все даты релизов — тяжёлые части) → на ~8000 тайтлов
  ~100–120 МБ в `films.db`.

**Один запрос на фильм**: uk/en названия берутся из `translations`, поэтому отдельные запросы `uk-UA`/`en-US`
не нужны; русские жанры, русские имена людей (`name` в ru-RU ответе — перевод, `original_name` — оригинал) —
из того же ru-RU ответа. Сериал: 1 запрос на сериал + 1 на каждый сезон.

## API модуля

Все функции принимают `conn` (`films.db.connect()`), возвращают обычные dict; в `titles`/`people`/`credits`
модуль сам ничего не пишет.

- `find_by_imdb(conn, imdb_id)` → `{imdb_id, movie, tv, tv_episode, tv_season, warnings, movie_title, movie_year,
  tv_title, tv_year}`; `warnings`: «not on TMDB», «several results», «both a movie and a tv result».
- `search(conn, titles, year, kind='movie'|'tv', hints=None)` → `{status: matched|ambiguous|not_found, reason,
  best, match, candidates, queries, years_tried}` (`match` = `best`, как ключ в `imdb.resolve`).
- `movie(conn, tmdb_id)` → dict (ниже), `None` при 404. `movie_row(m)` → колонки `titles`.
- `tv(conn, tmdb_id)` → dict сериала; `season(conn, tv_id, n, series=None)` → dict сезона, `None` если такого
  сезона на TMDB нет; `season_row(series, season)` → колонки `titles` для строки-сезона.
- `collection(conn, id)` → `{id, name, name_original, name_ru, name_uk, name_en, parts:[{tmdb_id, title_ru,
  title_original, release_date, year}]}` (части по дате, без даты — в конце).
- `person(conn, id)` → `{tmdb_id, name (оригинал), name_ru, name_ru_source, imdb_id, known_for_department}`
  — 1 запрос на человека.
- `countries_ru(conn)` → ISO → русское название. `pick_trailers(videos)` — выбор трейлеров (см. ниже).
- `movie_raw` / `tv_raw` / `season_raw` / `get(...)` — сырые ответы (через кэш).

CLI (общие опции — **до** подкоманды: `--max-age`, `--offline`, `--full`):
```
python -X utf8 -m films.sources.tmdb find tt15239678
python -X utf8 -m films.sources.tmdb movie 693134
python -X utf8 -m films.sources.tmdb tv 1399 --season 2
python -X utf8 -m films.sources.tmdb search "Дюна: Часть вторая" 2024 movie ["Dune: Part Two" ...] [--director ...] [--country ...]
python -X utf8 -m films.sources.tmdb collection 726871
python -X utf8 -m films.sources.tmdb person 1190668
```

### Поля фильма (`movie()`)

| Поле | Откуда / решение |
|---|---|
| `title_ru`, `title_uk`, `title_en` (+ `*_source`) | `translations` (ru-RU / uk-UA / en-US|GB), иначе `alternative_titles` страны RU/UA/US, иначе `None`. Если язык оригинала = ru/uk/en — оригинальное название. **Не** берём `title` ru-RU-ответа: при отсутствии перевода TMDB подставляет туда англ./оригинальное название |
| `title_original`, `title_latin` | `title_latin` только если в оригинале есть нелатинские буквы: en-перевод, иначе латинский romanized/US/GB alternative title |
| `release_date`, `release_date_kind` | мировая премьера = самая ранняя дата типа 1–3 (премьера / ограниченный / широкий прокат) из `release_dates`; если таких нет — самая ранняя любая (цифровой и т. п.), `release_date_kind` говорит какая, напр. `premiere:MX`, `digital:US` |
| `year` | год `release_date` (совпадает с годом трекера в 96%; год «основной» даты TMDB — в 87%) → `year_primary` |
| `release_date_primary`, `release_date_ru`, `digital_release_date`, `release_dates` | основная дата TMDB, кинопрокат РФ, первая цифровая, все даты (страна, тип, заметка) |
| `countries`, `country_codes` | `production_countries`: **русские названия** из `/configuration/countries?language=ru-RU` + короткие формы как на трекерах (США, Южная Корея, Северная Корея, Гонконг, Северная Македония…); ISO-коды отдельно |
| `genres`, `main_genre`, `is_animation` | русские названия ru-RU в порядке TMDB; `main_genre` — первый; `is_animation` — жанр 16 |
| `runtime_min`, `budget_usd`, `box_office_usd` | 0 у TMDB = «неизвестно» → `None`; сборы = `revenue` (мировые) |
| `studios` | `production_companies` (имена, id — в `studio_ids`) |
| `imdb_id`, `external_ids` | IMDb, Wikidata, соцсети |
| `collection` | `belongs_to_collection` {id, name} — для франшиз (R34) |
| `keywords` | англ. ключевые слова TMDB |
| `directors` | crew job Director / Co-Director: `{tmdb_id, name (оригинал), name_ru, jobs}` |
| `writers` | отдел Writing, только авторские job (Screenplay, Writer, Story, Novel, Characters, Teleplay, Book…); job сохраняется. Остальное из Writing (Story Editor, Script Consultant…) — `writing_other` |
| `cast` | весь состав по порядку: `{tmdb_id, name, name_ru, character, order}` |
| `trailer_ru`, `trailer_ru_url`, `trailers_ru`, `trailers_uk`, `trailer_en_url`, `videos_ru_other` | см. «Русский трейлер» |
| `poster_path`, `original_language`, `status`, `origin_country` | — |

IMDb id людей в credits TMDB нет: для каждого человека нужен отдельный запрос `person()` (кэшируется на
180 дней). Русские имена людей приходят бесплатно в credits (если у TMDB есть перевод).

### Сериал (`tv()`) и сезон (`season()`)

Сериал: названия (как у фильма), `first_air_date`, `last_air_date`, `countries`, `genres` (TV-жанры TMDB:
«НФ и Фэнтези», «Боевик и Приключения»…), `networks`, `studios`, `created_by`, `number_of_seasons`,
`number_of_episodes`, `seasons` (номер, дата, число серий), `status` → `series_status`
(returning / ended / canceled / in_production / planned / pilot), `tv_type` (Scripted / Miniseries / Reality…),
`imdb_id`, `keywords`, `original_language`, трейлеры уровня сериала.

Сезон: `air_date`, `year`, `last_episode_date`, `episode_count`, `episodes_aired`, `runtime_min` (медиана
длительности серий) и `runtime_min_range`, `directors` / `writers` / `cast` этого сезона из
`aggregate_credits` (с числом серий у каждого), внешние id сезона (TVDB, Wikidata), русский трейлер сезона.
У IMDb нет id сезона → `series_imdb_id` + `season_number`, `series_key = 'imdb:tt…'` (или `tmdb:…`).

## Поиск (`search`)

- Каждый переданный заголовок (оригинал, латиница, RU, UK) × `ru-RU` и `en-US`. Фильмы — с `year`; сериалы —
  с `first_air_date_year` **и без года** (год сезона ≠ году старта сериала). Если с годом ничего — ещё год±1.
- Оценка = совпадение имени (точно 1.0 / без артикля 0.9 / до подзаголовка 0.75 / нечётко ≤ 0.8) × год
  (разница 0 → 1.0, 1 → 0.75, 2 → 0.35) × подсказки × «покрытие».
- Правдоподобные кандидаты (имя ≥ 0.75 и топ-2 каждого запроса TMDB) перепроверяются по полным данным —
  все переводы и альтернативные названия; год фильма = год премьеры / основной даты (прочие страны и
  перевыпуски — только 0.6); год сериала = год любого сезона. Эти запросы — те же, что `movie()`/`tv()`,
  так что для найденного тайтла лишних запросов нет.
- «Покрытие»: кандидат, совпавший с меньшим числом переданных названий, чем лучший по покрытию, × 0.7
  (два фильма «Соседи» 2024 — выигрывает тот, что совпал и с «Kaimynai»).
- `hints={'directors': [...], 'countries': [...]}` (из описания раздачи): режиссёр сравнивается между
  письменностями (транслитерация + фонетический «скелет» фамилии: «Мэдиган» = «Madigan»); несовпадение
  режиссёра × 0.9, страны × 0.95 (данные трекера шумные); совпадение используется для разрешения ничьих.
- Решение (как `imdb.resolve`): `matched` — лучший ≥ 0.8 и все остальные ниже ≥ 0.15; или близкие соперники
  совпали лишь с частью названий лучшего; или только у лучшего режиссёр трекера; или только у лучшего страна
  трекера (а у соперников явно другая); или соперники без голосов TMDB при ≥ 20 у лучшего; или год отличается
  на 1, но имя и режиссёр совпали. Иначе `ambiguous` с кандидатами (в очередь `match_issues`), `not_found` —
  ничего / только чужие названия / только другие годы. Причина всегда в `reason`.

## Русский трейлер

Источник — только TMDB `videos` (YouTube), языки `ru,uk,en` в том же запросе.

Фильм: `trailer_ru` = лучший ролик с `iso_639_1 = ru`, типа Trailer/Teaser:
1. не «клип/эпизод/отрывок/обзор/реакция» по названию (такие помечаются `not_trailer` и **никогда** не
   становятся `trailer_ru`, остаются в списке);
2. Trailer раньше Teaser;
3. без пометки «субтитры / sub / [ENG / оригинал» (субтитрованные остаются запасным вариантом, флаг `subtitled`);
4. официальный раньше неофициального;
5. новее раньше старее.

`trailers_ru` / `trailers_uk` — все ru/uk трейлеры в этом порядке; `trailer_en_url` — лучший английский
(только для справки, в `trailer_ru_url` не попадает); `videos_ru_other` — ru-ролики других типов.

Сезон (`trailer_ru_source`):
1. `season_videos` — ролики самого сезона;
2. `series_videos:name_mentions_season` — ролик сериала, в названии которого этот сезон («2 сезон», «Сезон 2»,
   «1-сезон», «Season 2», «S2»);
3. `series_videos:published_near_season_premiere` — ролик сериала без номера сезона, опубликован позже
   премьеры прошлого сезона + 30 дней, не позже премьеры этого + 60 дней, раньше премьеры следующего − 90 дней,
   и не раньше чем за 400 дней (1-й сезон — 730) до премьеры;
4. иначе `None`, а ролики сериала, которые не удалось отнести к сезону, — в `series_trailers_ru_unassigned`.
Трейлер уровня сериала (`tv()['trailer_ru']`) для строк-сезонов **не** используется.

Наличие ролика на YouTube (удалён/приватный) модуль не проверяет — только TMDB API (по заданию). Проверка —
`youtube.com/oembed` в модуле трейлеров (PLAN, этап 4).

## Проверка 2026-10-23

Прогон по **всем** IMDb id из `releases.parsed` (2688 пар «IMDb id + сезон»: 1652 фильма, 802 сериала,
234 мультипликации), 8 потоков, кэш пуст: 6051 запрос за 303 с (упор в лимит 20/с), 0 повторов, 0 ошибок;
средняя задержка запроса 0.18 с.

`find_by_imdb`: 2623 найдено, **65 нет на TMDB** по этому IMDb id (неверный id в описании раздачи —
напр. «Фантастическая четвёрка: Первые шаги» с tt1054216 вместо tt10676052; или китайские/корейские
сериалы и малые фильмы без IMDb-связи на TMDB) → для них `search()`; 2 id дают и фильм, и сериал.

Фильмы (1727):

| поле | есть |
|---|---|
| RU название | 97% (1671; 56 — малые фильмы без перевода, RU берём с трекера) |
| UK название | 53% |
| латиница для нелатинского оригинала | 193 из 194 |
| год = году трекера | 96% (±1 — 55; больше — неверный IMDb id на трекере) |
| страны / жанры / длительность / студии | 97 / 100 / 98 / 96% |
| режиссёр / сценарист / актёры | 100 / 98 / 99% |
| бюджет / сборы | 29 / 36% (2026: 33% бюджет) |
| коллекция | 14% (235) |
| ключевые слова | 75% |
| **RU трейлер** | **24%** (413; 2024 — 28%, 2025 — 24%, 2026 — 18%; из них 23 только субтитрованные); RU или UK — 37%; EN — 85% |
| IMDb id совпадает | 100% |

Проверено отдельно на 25 случайных фильмах без RU трейлера: отдельный `/videos` запрос тоже не даёт ru-роликов
— у TMDB их просто нет; нужен второй источник (поиск на YouTube, PLAN этап 4).

Сериалы (896) / сезоны (1163): RU название 94%, UK 65%, страны 96%, сети 99%, создатели 83%; сезон: год /
серии / длительность 100 / 100 / 99%, режиссёры 85%, сценаристы 83%, актёры 98%, **RU трейлер сезона 11%**
(127: свой ролик сезона 17, по номеру в названии 50, по дате 60); у 190 сезонов есть ru-трейлеры сериала,
отнесённые к другим сезонам (это правильно: «Пацаны» S5 не получает трейлер 1-го сезона). Год сезона ≠ году
трекера в 24% — почти всё раздачи «S01-S05», где трекер пишет год старта сериала. **10 сезонов нет на TMDB**:
нумерация TMDB другая («Дандадан» 2 → у TMDB один сезон, «Футурама» 13 → 11, «Клиника» 10, «Террор» 3, «Шоу 90-х» 3,
«Гарри Уайлд» 6, «Кэт и Кит» 2, «Беби Борн» 3…) → `season()` = `None`, в `match_issues`.

`search()` на 400 случайных тайтлах (оригинал + RU название + год + режиссёр/страны с трекера), эталон —
`find_by_imdb`: из 384 с эталоном **371 matched, все верные (0 ложных)**, 8 ambiguous, 5 not_found
(пиньинь/корейская романизация, которых TMDB не знает, и нет RU перевода); ещё 9 matched без эталона (IMDb id
трекера не связан на TMDB) — по названию/году/стране правдоподобны. В среднем 5.7 поисковых запросов + 1–3
запроса деталей на тайтл.

### Время полного прогона (~8000 тайтлов)

- Есть IMDb id: ~2.25 запроса на тайтл (find + movie, или find + tv + сезон) → ~18 000 запросов →
  **~15 мин** при 20/с (8 потоков); в один поток ~55 мин (задержка ~0.18 с).
- Нет IMDb id → `search`: ~7–9 запросов → на каждые 1000 таких тайтлов ещё ~7 мин.
- Повторный прогон в пределах `max_age_days` — секунды (только кэш).
