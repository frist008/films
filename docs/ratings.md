# Рейтинги и постеры: IMDb-датасеты, Rotten Tomatoes, ссылки на постеры

Исследование и реализация от 2026-10-23. Модули: `films/sources/imdb.py`, `films/sources/rt.py`.
Кэш: `data/cache/imdb/`, `data/cache/rt/` (в `.gitignore`).

---

## 1. IMDb — официальные датасеты

Источник: <https://datasets.imdbws.com/> (обновляются ежедневно, ~00:50 UTC). На 2026-10-23 там 7 файлов:
`name.basics`, `title.akas`, `title.basics`, `title.crew`, `title.episode`, `title.principals`, `title.ratings`
(схема не менялась). Используем 4: `title.basics` (229 МБ), `title.episode` (55 МБ), `title.ratings` (9 МБ),
`title.akas` (519 МБ).

### Команды

```bash
python -X utf8 -m films.sources.imdb load                 # скачать (если ETag/размер изменились) и перезалить
python -X utf8 -m films.sources.imdb load --no-download   # перезалить из кэша
python -X utf8 -m films.sources.imdb lookup "Дюна: Часть вторая" 2024 [--kind movie|series] [-n 10]
```

Из кода: `imdb.lookup(conn, title, year=None, kind=None)` → кандидаты со score;
`imdb.resolve(conn, [titles...], year, kind)` → `matched | ambiguous | not_found` + кандидаты;
`imdb.rating(conn, tconst)`; `imdb.suggest(tconst)` → постер (см. §3); `imdb.poster_url(url, height)`.

### Что загружается и почему

* **Типы:** `movie`, `tvMovie`, `video`, `tvSpecial`, `tvSeries`, `tvMiniSeries`.
  * `video` **включён**: так IMDb типизирует direct-to-video полнометражки (много анимации — DC/Warner Animation,
    Lego, продолжения хорроров), они появляются на трекерах. Стоит дёшево (26,7 тыс. строк). Мусор (концерты,
    making-of) отсеется на этапе сопоставления с раздачами — сам по себе в БД он не мешает.
  * `tvSpecial` — включён (стендапы, спецэпизоды, рождественские спецвыпуски сериалов — раздаются как фильмы).
  * **Не включены:** `tvEpisode` (используется только для определения «идущих» сериалов), `short`, `tvShort`,
    `tvPilot`, `videoGame`, `musicVideo`, `podcastSeries/Episode`.
  * `isAdult = 1` — пропускаются (8 435 шт. в нужных годах), счётчик пишется в отчёт загрузки.
* **Годы:** `startYear` 2022–2027 **или** `endYear ≥ 2024` **или** (только сериалы) у сериала есть хотя бы одна
  серия с `startYear ≥ 2024` по `title.episode`. Третье условие важно: у **идущих** сериалов `endYear` пустой,
  и без него потерялось бы всё, что стартовало до 2022 и продолжается (например, «Abbott Elementary», 2021–).
  Так добавлено **8 666** сериалов.
* **Без года вообще** (`startYear` и `endYear` пустые, анонсы/в разработке): 119 933 фильма-подобных записи
  **не** грузятся — у вышедших фильмов год есть. Если понадобятся — убрать условие в `load()`.
* **Рейтинги:** для всех загруженных tconst (у кого есть оценки).
* **akas:** все строки всех регионов/языков загруженных tconst.

### Результат загрузки (2026-10-23)

| таблица | строк | примечание |
|---|---|---|
| `imdb_basics` | 230 447 | movie 102 690 · tvSeries 56 815 · video 26 693 · tvMiniSeries 21 195 · tvSpecial 13 435 · tvMovie 9 619 |
| `imdb_ratings` | 97 143 | из 1 723 954 строк файла; у 133 тыс. загруженных тайтлов оценок нет (свежие/малоизвестные) |
| `imdb_akas` | 1 450 370 | из 59 872 171; у 177 тайтлов нет ни одного aka |

Время: скачивание ~10 с (812 МБ), разбор basics 17 с, episode 7 с, ratings 1 с, akas 76–82 с; **всего ~110 с**.
Повторный прогон: удалено устаревших строк 0 (проверено). Отчёт последней загрузки — `data/cache/imdb/last_load.json`.

**Важно для сопоставления по RU/UK названиям:** из 5 526 тайтлов 2024–2026 с ≥1000 голосов RU-aka есть только
у 3 174 (57 %), UA-aka — у 1 392 (25 %). Для мелких тайтлов ещё меньше. То есть akas IMDb — подспорье, но
основной источник RU/UK названий — TMDB (как и решено).

### Как устроена загрузка («обновить = заменить»)

Потоковый разбор gzip построчно (весь файл в память не читается; в памяти только множество отобранных tconst).
Запись — upsert пачками по 50 000 строк в отдельных транзакциях (таблицы всё время читаемы для параллельных
процессов), akas — удаление+вставка целыми группами tconst. В конце удаляются строки tconst, которых больше нет
в выборке (и рейтинги, не обновлённые этим прогоном). Скачивание — только если ETag/размер на сервере
изменились (`*.meta.json` рядом с файлом).

### lookup

Нормализация: casefold, NFKD без диакритики (ё→е; побочно й→и, ї→і — одинаково с обеих сторон), `&`→`and`,
вся пунктуация → пробел. Индекс «нормализованное название → tconst + откуда (primary/original/aka:регион)»
строится из БД один раз (~9 с) и кэшируется в `data/cache/imdb/title_index.pickle` (дальше ~0,6 с на запуск
процесса, поиск мгновенный); инвалидируется при перезагрузке.

Варианты поиска: точное совпадение → без артикля → часть до «:»/« - » → (если ничего) все слова запроса есть
в названии. Score = совпадение названия × год (0 → 1.0, ±1 → 0.75, ±2 → 0.35, дальше отбрасывается; для сериалов
«год внутри периода показа» = 0) × тип (`movie`: movie 1.0, tvMovie 0.9, video 0.85, tvSpecial 0.6; `series`:
tvSeries/tvMiniSeries 1.0) + крошечный тай-брейк по числу голосов.

`resolve()` принимает решение только если лучший ≥ 0.8 и второй ниже на ≥ 0.15; либо при равенстве
второй совпал **только** через aka (рабочее название и т.п.) и у лучшего в 20+ раз больше голосов; либо у
близких конкурентов вообще нет голосов, а у лучшего ≥ 1000. Иначе `ambiguous` с кандидатами — в ручную очередь.
Примеры: «Wicked» 2024 — второй кандидат «Nefarious» совпал по GB-aka «Wicked» → решено с пояснением;
«The Odyssey» 2026 — три фильма с таким названием и годом → `ambiguous` (разрешится через TMDB → imdb_id).

---

## 2. Rotten Tomatoes

### Команды

```bash
python -X utf8 -m films.sources.rt search "Dune Part Two" [--algolia]
python -X utf8 -m films.sources.rt fetch https://www.rottentomatoes.com/tv/the_last_of_us [--season-pages] [--refresh]
python -X utf8 -m films.sources.rt find 2024 movie "Ainda Estou Aqui" "I'm Still Here" "Я все еще здесь" --people "Walter Salles"
python -X utf8 -m films.sources.rt find 2024 series "Shōgun" --series-start 2024
```

### Доступ и вежливость

Обычный HTTPS с браузерным User-Agent — работает, 0 блокировок/ретраев за ~200 запросов. ≥ 1,2 с между
запросами (глобально в процессе), до 5 попыток с экспоненциальной паузой (2-4-8-16 с, `Retry-After` уважается)
на 403/429/5xx/сетевые ошибки. Сырые ответы кэшируются в `data/cache/rt/` (HTML страниц, поиска, JSON Algolia);
404 тоже кэшируется («страницы нет»). Свежесть кэша 7 дней, `refresh=True` — принудительно.

### Поиск

* `search(query)` — страница `/search?search=…`: элементы `<search-page-media-row>` в двух секциях
  (`type="movie"` и `type="tvSeries"`), **максимум 10 фильмов + 10 сериалов** (больше страница не отдаёт без JS).
  Атрибуты у фильмов с дефисом (`release-year`, `tomatometer-score`), у сериалов — без (`startyear`, `tomatometerscore`); учитываются оба варианта.
* `search_algolia(query)` — **второй канал**: та же поисковая система, что у поля поиска на сайте
  (Algolia, публичный search-only ключ сайт сам встраивает в страницу поиска; модуль берёт его оттуда же, не хардкодит).
  Даёт до 20+ результатов, **альтернативные названия (`aka`)**, режиссёра, год. Используется в `find()` только
  если поиск по странице не дал уверенного совпадения. Именно он нашёл «Ne Zha 2» (`/m/ne_zha_ii`),
  а «Буратино» (на RT — «Pinocchio» 2026, реж. Игорь Волошин) с подтверждением по режиссёру.

### Страница тайтла — `fetch(url)`

Всё из встроенного JSON, без хрупкой вёрстки:

| поле | откуда |
|---|---|
| `critics` (Tomatometer %), `critics_reviews`, `critics_average`, `critics_certified`, `critics_top` | `<script id="media-scorecard-json">` → `criticsScore`, `overlay.criticsTop` |
| `audience` (Popcornmeter %), `audience_type` (`VERIFIED`/`ALL` — что показывает сайт), `audience_ratings` (точное число оценок), `audience_banded` («5,000+ Verified Ratings»), `audience_all`, `audience_verified` | то же → `audienceScore`, `overlay.audienceAll/audienceVerified` |
| `critics_liked/not_liked`, `audience_liked/not_liked`, `*_hidden` | то же; RT **скрывает %** при малом числе отзывов (видели: 8 рецензий критиков у «Pushpa 2»; < 50 оценок зрителей) — счётчики сохраняем как есть, процент не выдумываем |
| `poster_url` | `ld+json.image` (оригинал без ресайза), запасной — `media-hero-json.posterSrc` |
| `title`, `kind`, `ems_id`, `lifecycle`, `parent` | `<script data-json="vanity">` |
| `rt_id` | `curation-json` |
| `year` | дата «Release Date (Theaters)» / «Release Date» / `ld+json.dateCreated` — это **год выхода в США**; у фестивальных/иностранных фильмов он на 1–3 года позже мировой премьеры (Brutalist 2025, «Мастер и Маргарита» 2027). Для сопоставления используется год из поиска (он «оригинальный») |
| `genres`, `info` | `ld+json.genre`; блок media info целиком как словарь (Director, Producer, Screenwriter, Distributor, Production Co, Rating, Genre, Original Language, Release Date (Theaters/Streaming), Box Office (Gross USA), Runtime, Sound Mix, Aspect Ratio; у сериалов Creator, Network) |
| `seasons` (сериалы) | плитки `<tile-season>`: номер, ссылка, **Tomatometer сезона**, месяц/год выхода. Popcornmeter сезона на странице сериала нет → `season_pages=True` (или список номеров) открывает страницу каждого сезона (`/tv/x/s02`): там полный scorecard — оба процента, число рецензий и оценок, постер сезона, дата |

Для сериалов на странице сериала оба процента — **средние по сезонам** («Avg. Tomatometer», «Avg. Popcornmeter»).
Пример: The Last of Us — сериал 94 % / 62 %, а сезоны S1 96/86, S2 92/37. Для таблицы, видимо, нужен
**сезон, вышедший в наш год** — вопрос владельцу (см. ниже).

### Сопоставление — `find(titles, year, kind, people=None, series_start=None)`

Пробует каждое название (порядок = приоритет: оригинальное, латиница/английское, RU, UK) сначала через страницу
поиска, затем (если не уверен) через Algolia по всем названиям; кандидаты с одним URL из обоих каналов
**сливаются** (каст, режиссёр, aka). Оценка = совпадение названия (точное 1.0; без артикля 0.95; похоже ≥ 92 % 0.85;
«Название: подзаголовок» 0.7) × год (фильм: 0 → 1.0, ±1 → 0.85, дальше — отброс; сериал: с `series_start` —
сравнивается год старта ±1, без него — год должен попадать в период показа, а открытый сериал, начатый > 10 лет
назад, получает 0.7) × люди (`people` — режиссёр/актёры из TMDB: совпал +0.1; не совпал при известном режиссёре ×0.75,
при известных только 3 актёрах из поиска ×0.9).
**Принимается**, только если score ≥ 0.80 и следующий кандидат ниже на ≥ 0.15. Иначе — `ambiguous` (с кандидатами,
для ручной очереди `match_issues`) или `not_found`. Возвращает `status, match, confidence, candidates, tried`.

### Тест на 68 реальных тайтлах 2024–2026

Набор (названия взяты из IMDb-датасетов так, как их передаст конвейер: original, primary, US/GB-aka, RU-aka, UA-aka):
16 крупных, 10 небольших, 12 иноязычных, 10 анимации (+ Arcane), 14 сериалов, 5 российских/украинских
(вероятно без страницы на RT).

| | первый прогон | после исправлений, с `series_start` |
|---|---|---|
| matched (все проверены глазами — верные) | 64 | 66 |
| ambiguous | 4 | 1 («The Odyssey» 2026 — на RT три `/m/the_odyssey_2026*`; с `people=["Christopher Nolan","Matt Damon"]` → matched верно) |
| not_found | 0 | 1 («Ёлки 11» — страницы на RT действительно нет; есть только «Yolki»/«Christmas Trees» 1–8) |
| ложных совпадений | 0 | 0 |

Итого **66/68 автоматически (97 %), 67/68 с людьми из TMDB, 0 ошибочных**; единственный «не найден» —
действительно отсутствует на RT. Из 66 найденных: оба процента есть у 59; у «Pushpa 2» Tomatometer скрыт
(8 рецензий), Popcornmeter есть; у 6 страница есть, но процентов нет — «Avengers: Doomsday» (не вышел),
«Мастер и Маргарита» (на RT «coming soon» 2027, 0 рецензий), «Мавка. Справжній міф», «Буратино» (на RT «Pinocchio» 2026),
«Сто лет тому вперёд», «Чебурашка 2». Постер на странице RT — у 64/66 (нет у «Мастер и Маргарита», «Сто лет тому
вперёд»); у IMDb (suggestion-JSON) — 68/68.

Каналы: 56 нашлись первым же запросом (оригинальное название) по странице поиска; 8 — со второго названия
(английское вместо оригинального: Ainda Estou Aqui, Dane-ye anjir-e ma'abed, Affeksjonsverdi, Yek tasadof-e sadeh,
Eo-jjeol su-ga eop-da, Master i Margarita, Straume, Ojing-eo geim); 2 — только через Algolia (Ne Zha 2 — `/m/ne_zha_ii`;
Буратино — `/m/pinocchio_2026`). **RU/UK названия на RT не помогли ни разу** (RT их не знает), но перебираются, как
требует правило. Сериалы: 29 сезонов, у всех оба процента со страниц сезонов. Время: 237 с на 68 тайтлов с нуля
(193 запроса, включая 29 страниц сезонов), повтор из кэша — 31 с.

Замечание: «Буратино» принят без подтверждения людьми (точное «Pinocchio», год ±1 → 0.85) — совпадение верное
(реж. Игорь Волошин), но это пограничный случай: для общих названий конвейер обязан передавать `people`.

### Режимы отказа (что увидели и как обработано)

1. **Одноимённые тайтлы одного года** (The Odyssey ×3; «Shōgun» 2024 vs старый «Shogun» на RT с годом 2013) →
   `ambiguous`, лечится `people` и `series_start`. Конвейер должен **всегда** передавать режиссёра/актёров из TMDB
   и год старта сериала.
2. **Иноязычные фильмы** — на RT под английским названием; оригинальное не находится → важно передавать и
   английское (TMDB `title` / IMDb primary).
3. **Год на RT ≠ год премьеры** (+1…+3 для фестивальных/иностранных) — допускается ±1 со штрафом; при большем
   расхождении год из поиска (release-year) обычно всё равно «оригинальный».
4. **Страница есть, процентов нет** (не вышел / мало рецензий / пустая заглушка для российских фильмов) — `critics=None`,
   `*_hidden`, счётчики сохраняются. Надо хранить `rt_url` + `rt_checked_at`, и перепроверять позже.
5. **Нет страницы вовсе** — `not_found`, кандидаты пустые; фиксировать `rt_checked_at` без `rt_url`.
6. **Поиск страницы ограничен 10+10** — популярные слова («Flow», «Wicked») могут вытеснить нужный тайтл; закрыто
   вторым каналом (Algolia, фильтрация по `isEmsSearchable`, `aka`).
7. **Algolia `titles`** содержит обрезанные префиксы («The Last o», «Christmas») — давали ложных кандидатов,
   исключены; используется только `aka`.

---

## 3. Постеры: какую ссылку хранить (`titles.poster_url`)

### Что проверено

| | IMDb (`m.media-amazon.com/images/M/…`) | RT (`resizing.flixster.com/…`) |
|---|---|---|
| Открывается напрямую во вкладке | да, `image/jpeg`, без `Content-Disposition` | да, `image/jpeg` |
| Нужны cookies / Referer | нет (curl без заголовков, с чужим Referer — 200) | нет (то же) |
| Кэш-заголовки CDN | `Cache-Control: max-age=630720000` (20 лет), `Expires: 2045`, `Last-Modified` 2010 у старого постера | `max-age=93312000` (≈3 года) |
| Можно построить URL нужного размера | да: `…._V1_.jpg` — оригинал, `…._V1_UY600_.jpg` — высота 600, `SX300` — ширина 300 (проверено) | нет: URL **подписан** (HMAC-префикс `xxxx=/`), подмена подписи → 400; ресайз только через уже подписанные варианты |
| Старые домены | `ia.media-imdb.com`, `images-na.ssl-images-amazon.com` — **до сих пор работают** (200) | `content6.flixster.com` (старый хост постеров RT) — **домен не резолвится** |
| Как получить без браузера | `v3.sg.media-imdb.com/suggestion/x/{tconst}.json` → `i.imageUrl` (HTTP, без WAF) — `imdb.suggest()` | со страницы RT (`fetch()`) |
| Покрытие на тест-наборе 68 | 68/68 | 64/68 (нет страницы/заглушка) |

**Проверка «старых» ссылок.** 86 постеров IMDb, сохранённых в 2017–2020 гг. (архив OMDb-ответов в открытом
репозитории StoDevX/sga-weekly-movies, три разных домена IMDb-картинок): **82 живы (95 %)**, 4 → 404 (постер
заменён и старый файл удалён). Пример: постер «Тёмного рыцаря» с `Last-Modified 2010` отдаётся с любого из трёх
доменов.
Для RT: снимок страницы «Parasite» в Wayback Machine от 2020-01-01 — 63 ссылки на картинки (постер, фото,
актёры, трейлеры). Сегодня живы **58 (92 %)**; 5 мёртвых (404) — все старого формата `…/v1.<base64>`; все ссылки
формата `v2/…` (обёртка над `flxt.tmsimg.com`, `statcdn.fandango.com`, `ems.…`) живы. Сам постер 2020 года
(`og:image`, формат v1) — жив. Старый хост постеров RT `content6.flixster.com` — домен больше не резолвится.
(Массовую проверку 10+10 страниц за 2019/2021/2022 провести не удалось: Wayback Machine в этот день то
«Temporarily Offline», то отвечал по 1–5 минут на снимок; проверено то, что удалось скачать.)
Оговорка: выборки небольшие (86 IMDb-ссылок, 63 RT-ссылки с одной страницы), так что 95 % vs 92 % — не
доказательство, что IMDb надёжнее; решают остальные пункты ниже.

### Рекомендация: **IMDb**

1. Ссылки живут годами (95 % через 6–9 лет), CDN обещает кэш на 20 лет, все три исторических домена IMDb работают
   (у RT старый домен `content6.flixster.com` мёртв, старый формат v1 частично 404).
2. Ссылку можно нормализовать к стабильной форме и размеру без подписи: хранить `…._V1_.jpg` (оригинал)
   или `…._V1_UY1000_.jpg` (удобный для вкладки) — `imdb.poster_url(url, height)`.
3. Получается **без браузера** через suggestion-JSON по tconst (1 запрос на тайтл, кэш), а tconst у нас будет у всех
   тайтлов; RT-страницы есть не у всех (у российских/укр. тайтлов часто нет).
4. RT-ссылки подписаны сервером ресайза — при ротации ключа или смене формата (уже был переход
   `content6.flixster.com` → `v1.base64` → `v2/` → `ems.`) все сохранённые ссылки разом умрут.

Риск IMDb: при замене постера старый файл иногда удаляется (4/86). Митигировать: при каждом «обнови базу»
(или раз в N дней) делать HEAD по `poster_url`; на 404 — перечитать suggestion и обновить ссылку (+ событие).
Запасной вариант — постер RT, если у IMDb нет картинки.

---

## 4. Предлагаемые изменения схемы (в `films/db.py` не вносил)

Миграция 3 (предложение):

```sql
-- akas: сейчас теряются колонки types / attributes / isOriginalTitle датасета.
-- types ('working', 'festival', 'imdbDisplay', 'alternative', 'original') нужен, чтобы не путать рабочие названия
-- (пример: «Wicked» как GB-aka у «Nefarious»); attributes — «literal title», «transliterated title» и т.п.
ALTER TABLE imdb_akas ADD COLUMN types TEXT;
ALTER TABLE imdb_akas ADD COLUMN attributes TEXT;
ALTER TABLE imdb_akas ADD COLUMN is_original INTEGER;
-- нормализованное название для поиска прямо в SQL (сейчас — индекс в pickle-кэше)
CREATE TABLE imdb_title_norm (norm TEXT NOT NULL, tconst TEXT NOT NULL, source TEXT, PRIMARY KEY (norm, tconst, source));
-- basics: isAdult (сейчас такие строки просто не грузятся)
ALTER TABLE imdb_basics ADD COLUMN is_adult INTEGER;

-- RT: сырые результаты, как imdb_*/store_items (заполняются до сопоставления, привязываются позже)
CREATE TABLE rt_pages (
    url            TEXT PRIMARY KEY,           -- https://www.rottentomatoes.com/m/... | /tv/... | /tv/.../s02
    kind           TEXT,                       -- movie / series / season
    parent_url     TEXT,                       -- для сезона
    season         INTEGER,
    title          TEXT,
    year           INTEGER,                    -- год RT (выход в США)
    critics        INTEGER, critics_reviews INTEGER, critics_liked INTEGER, critics_not_liked INTEGER,
    critics_certified INTEGER, critics_top INTEGER,
    audience       INTEGER, audience_type TEXT, audience_ratings INTEGER, audience_banded TEXT,
    audience_liked INTEGER, audience_not_liked INTEGER, audience_all INTEGER, audience_verified INTEGER,
    poster_url     TEXT,
    ems_id         TEXT, rt_id TEXT,
    info           TEXT,                       -- JSON media-info
    fetched_at     TEXT NOT NULL,
    title_id       INTEGER REFERENCES titles(id)
);
-- titles: дополнительно
ALTER TABLE titles ADD COLUMN rt_critics_reviews INTEGER;    -- сколько рецензий за Tomatometer
ALTER TABLE titles ADD COLUMN rt_audience_ratings INTEGER;   -- сколько оценок за Popcornmeter
ALTER TABLE titles ADD COLUMN rt_audience_type TEXT;         -- VERIFIED / ALL
ALTER TABLE titles ADD COLUMN poster_source TEXT;            -- imdb / rt
ALTER TABLE titles ADD COLUMN poster_checked_at TEXT;
```

Конфиг (предложение, `config/settings.toml`): `[rt] min_interval_s = 1.2, cache_days = 7, accept = 0.80, margin = 0.15`,
`[imdb] title_types = [...]`, `year_from = 2022`, `year_to = 2027` — сейчас это константы в модулях.

## 5. Повторяющиеся метаданные, которых нет в схеме (вопрос владельцу — по каждому отдельно)

Встречались стабильно на страницах RT / в датасетах IMDb:

1. **Рейтинг сериала: весь сериал или сезон?** RT даёт и средний по сезонам, и по каждому сезону (оба процента).
   Что показывать в таблице для сериала — сезон, вышедший в 2024–2026 (раздача), или среднее?
2. **Число рецензий / оценок** за процентами RT (и голоса IMDb уже есть) — нужно ли в таблице («92 % (466)»)?
3. **Certified Fresh / Verified Hot** — значок качества RT.
4. **Top Critics %** — отдельный Tomatometer топ-критиков.
5. **Оригинальный язык** (RT «Original Language», TMDB тоже даёт) — полезно для фильтра «озвучка не нужна»?
6. **Дата цифрового релиза** («Release Date (Streaming)») — когда стоит ждать WEB-DL.
7. **Дистрибьютор** (RT «Distributor»; студии у нас уже есть, но это другое).
8. **Сборы в США** («Box Office (Gross USA)») — у нас мировые сборы, есть ли смысл в американских?
9. **Звук в кинотеатре** («Sound Mix»: Dolby Atmos, IMAX 6-Track…) и **соотношение сторон** («Aspect Ratio») —
   может помогать для IMAX/Open Matte.
10. **Телеканал/платформа** сериала («Network»: HBO, Apple TV+…).
11. **Создатели сериала** («Creator») — как отдельная роль в `credits`?
12. **Статус проката** (RT lifecycle: COMING_SOON / IN_THEATERS / OUT_OF_THEATERS / NOT_AIRING).
