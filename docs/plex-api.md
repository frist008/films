# Plex API — заметки и рецепты

Исследовано 2026-10-23 на PMS 1.43.5.11029 (Windows), Plex Web 4.162.1. Модуль: `films/sources/plex.py`.

## Доступ

- Сервер: `http://127.0.0.1:32400`, `machineIdentifier = <plex.machine_id>`.
- Токен: реестр `HKCU\Software\Plex, Inc.\Plex Media Server\PlexOnlineToken` (через `winreg`). Передавать
  **только заголовком** `X-Plex-Token` (не в URL — иначе попадёт в логи/исключения). Токен = аккаунт
  `<plex.user>` (plex.tv id 13412341, uuid `334254da41a3da33`); на этом сервере он же `accountID = 1`.
- Заголовки: `Accept: application/json`, `X-Plex-Client-Identifier: <любой постоянный>`, `X-Plex-Product`.
- Ответы декодировать как `bytes.decode('utf-8', errors='replace')` → `json.loads`. Во внутренней БД сервера
  часть названий лежит в cp1251 (не UTF-8) — читать с fallback на cp1251.

## Библиотеки (26 шт., все типа `movie`)

- 8 библиотек с агентом **Plex Movie** (`tv.plex.agents.movie`): Все, ДляРамала ДляСани, БезСани,
  ДляСани/БезСтаса, Стас Хочет, Яны, Без Кости Без Сани, Без Кости с Саней. Элементы имеют
  `guid = plex://movie/<id>` и `Guid[] = imdb:// tmdb:// tvdb://` (только с `includeGuids=1`).
- 18 библиотек с агентом **none** (Plex Video Files Scanner): сериалы/аниме, сложенные как «фильмы».
  `guid = com.plexapp.agents.none://<hash>`, внешних id нет, `year` = дата файла (не год выхода).
  Названия папок вводят в заблуждение (в библиотеке «X-Men» папка `Marvels.The.Punisher.S02…`, внутри X-Men '97).
- Локации библиотек пересекаются (одни и те же папки подключены в 2–3 библиотеки) →
  один файл = несколько `ratingKey`. На 2026-10-23: 973 элемента, 910 уникальных файлов, 63 файла в ≥2 библиотеках.

### Чтение

| Что | Запрос |
|---|---|
| список библиотек | `GET /library/sections` |
| элементы | `GET /library/sections/{id}/all?includeGuids=1&X-Plex-Container-Start=0&X-Plex-Container-Size=200` (`totalSize` в ответе) |
| потоки (видео/аудио/субтитры) | `GET /library/metadata/{k1},{k2},…?includeGuids=1` — несколько ratingKey через запятую (пачки по 40 работают) |
| поиск в библиотеке по guid | `GET /library/all?guid=plex://movie/<id>` (работает); `guid=imdb://tt…` — **не работает** (0 результатов) |

Поля качества в `Media/Part/Stream` (streamType 1 = видео): `codec`, `profile`, `bitDepth`, `width/height`,
`bitrate`, `colorTrc` (`smpte2084` = PQ/HDR10, `arib-std-b67` = HLG), `colorPrimaries`, `DOVIPresent`,
`DOVIProfile`, `DOVILevel`, `DOVIBLCompatID` (1 = HDR10-база, 4 = HLG, 0 = нет фолбэка → Profile 5),
`DOVIELPresent`, `HDR10PlusPresent`, `displayTitle` («4K DoVi/HDR10+»). Аудио (streamType 2): `languageTag`,
`codec`, `profile` (`dolby digital plus + dolby atmos`, `ma`), `channels`, `audioChannelLayout`, `bitrate`,
`title` (здесь студия озвучки: «Дубляж, Red Head Sound», «MVO | TVShows»), `original`. Субтитры
(streamType 3): `forced`, `title`. У элемента: `viewCount`, `lastViewedAt` — **только для владельца токена**,
`editionTitle` (сейчас ни у одного элемента нет).

## История просмотров

### (а) история сервера

`GET /status/sessions/history/all?accountID=1&sort=viewedAt:desc&X-Plex-Container-Start=…&X-Plex-Container-Size=500`

Отдаёт только `historyKey, librarySectionID, title, type, originallyAvailableAt, viewedAt, accountID, deviceID` —
**без ratingKey и guid** (параметры `includeGuids` и т.п. не помогают). Владелец удаляет просмотренные файлы,
поэтому большинство записей указывает на уже несуществующие элементы. API также **не отдаёт**:
- отметки «просмотрено» без проигрывания (`view_type = 1`, 82 записи на 2026-10-23);
- просмотры трейлеров (`iva://`, metadata_type 12).

Поэтому модуль дополнительно читает внутреннюю БД сервера **только на чтение** (`sqlite3 …?mode=ro`):
`%LOCALAPPDATA%\Plex Media Server\Plug-in Support\Databases\com.plexapp.plugins.library.db`
- `metadata_item_views` (id = номер из `historyKey`): `guid` (plex://movie/…), `view_type` (0 = проиграно,
  1 = отмечено), `grandparent_title`, `parent_index`, `index`, `viewed_at`.
- `metadata_item_settings` (`view_count > 0`): состояние «просмотрено» по guid, в т.ч. синхронизированное с
  plex.tv без записи в истории (15 записей).

`plex://…` guid → внешние id: `GET https://metadata.provider.plex.tv/library/metadata/{id1},{id2},…`
(до 20 id через запятую) → `Guid[]`.

### (б) история plex.tv (профиль `/u/<plex.user>`)

Веб-клиент берёт её из GraphQL `POST https://community.plex.tv/api` (тело JSON `{query, variables, operationName}`,
заголовок `X-Plex-Token`). Запрос из бандла Plex Web (`GetWatchHistoryHub`):

```graphql
query GetWatchHistoryHub($uuid: ID = "", $first: PaginationInt!, $after: String) {
  user(id: $uuid) {
    watchHistory(first: $first, after: $after) {
      nodes { id date metadataItem { id title type year originallyAvailableAt
              userState { viewCount viewedLeafCount } grandparent { id title } } }
      pageInfo { hasNextPage endCursor }
    }
  }
}
```
`uuid` — из `GET https://plex.tv/api/v2/user` (поле `uuid`). Пагинация `first=100` + `endCursor`.
`metadataItem.id` = id в `plex://<type>/<id>`. Статистика профиля: `GetUserStats(username)` →
`userByUsername.watchStats {movieAmount episodeAmount showAmount}`.

Сериал в истории идёт одной записью `SHOW`; просмотренные серии: 
`GET https://discover.provider.plex.tv/library/metadata/{showId}/children?includeUserState=1` (сезоны,
`viewedLeafCount`) → `…/{seasonId}/children?includeUserState=1` (серии, `viewCount`, `lastViewedAt`).
**Постранично!** Без `X-Plex-Container-Size` Discover отдаёт детей по 20 (сериал из 37 серий, 30 сезонов «Южного
парка»), а размер 1000 даёт 403. Всегда через `films.sources.plex.discover_children` (страницы по 200 до `totalSize`).
Отметка серии: Discover id серии = `…/{seasonId}/children` → `index`; `films.watched.mark_episode`.

«25 серий» профиля — это `watchStats.episodeAmount`, а не счётчик всей истории (был 75 при 542 записях, 4 при 4473;
ревью 2026-10-28). С 25.10 `watchHistory` отдаёт серии поштучно (3142 серии, 1174 фильма, 167 сезонов); целым сериалом
осталась только Agatha All Along, и её 9 из 9 серий раскрыты — недостающих серий нет.

**Перечитано 2026-10-25:** `watchHistory` теперь отдаёт и отдельные серии (`EPISODE` с `parent.index` = сезон,
`index` = серия) и сезоны (`SEASON` — отметки «сезон целиком», в «видел» не считаются): 4473 записи владельца
(3132 серии, 1173 фильма, 167 сезонов, 1 сериал). **`activityFeed(types:[WATCH_HISTORY])` — это лента вместе с друзьями**:
у каждой записи `userV2 { id username }`, из 6261 записи владельцу принадлежат те же 4473, остальные 1788 — чужие.
Поэтому `data/cache/plex_watch_history_all.json` (выгрузка ленты 2026-10-23, без `userV2`) смешивает владельца с
друзьями: например, серии Death Note E3/E5/E7/E18 2024 года там — просмотры другого пользователя, не владельца.
Историю владельца брать только из `watchHistory` (`fetch_history`), ленту — только с фильтром по `userV2.username`.
С 2026-10-25 `plex_history.season` / `episode` заполняются номерами Plex (сервер: `metadata_item_views.parent_index /
index`; состояние без истории и plex.tv — `parentIndex` / `index` провайдера метаданных), а `films link plex` считает
серию один раз по её IMDb id → TMDB (`find`), иначе по номерам Plex (абсолютная нумерация One Piece раскладывается по
сезонам TMDB), иначе по номеру из имени файла.

## Как отметить просмотренным (НЕ выполнялось — только исследование)

Ничего не отмечалось; рецепты взяты из кода Plex Web 4.162.1 (сага `scrobble`: берёт у провайдера action
`scrobble`, вызывает `key` = отметить / `reverseKey` = снять с `params: {identifier: <provider identifier>, key: <ratingKey>}`)
и из python-plexapi. Перед первым боевым использованием проверить на тестовом тайтле с разрешения владельца.

### 1. Тайтл есть в библиотеке сервера

```
GET http://127.0.0.1:32400/:/scrobble?identifier=com.plexapp.plugins.library&key={ratingKey}
    X-Plex-Token: <токен владельца>
```
снять отметку — `/:/unscrobble` с теми же параметрами. Отмечается для владельца токена (<plex.user>).
Один файл может быть в нескольких библиотеках (разные ratingKey) — состояние хранится по guid элемента,
поэтому для `plex://movie/…` достаточно одного ratingKey; для элементов без агента (`com.plexapp.agents.none`)
отмечать каждый ratingKey. Включена синхронизация состояния с plex.tv
(`GET https://clients.plex.tv/api/v2/user/view_state_sync` → `consent: true`), так что отметка
plex://-элемента уходит и в plex.tv.
Проверка: `GET /library/metadata/{ratingKey}` → `viewCount ≥ 1`, `lastViewedAt`.

Как найти ratingKey:
- по IMDb id: `GET https://metadata.provider.plex.tv/library/metadata/matches?type=1&guid=imdb://tt4870838`
  (type 1 = фильм, 2 = сериал) → `Metadata[0].guid = plex://movie/<id>` →
  `GET /library/all?guid=plex://movie/<id>` → `ratingKey` (может быть несколько — по библиотекам);
  или локально: таблица `plex_items`, где `guids` содержит `imdb://tt…`.
- по названию: `GET /library/sections/{id}/all?title=<подстрока>` или `GET /hubs/search?query=<текст>`;
  `matches?type=1&title=<название>&year=<год>` у metadata-провайдера → plex guid → как выше.

### 2. Тайтла нет в библиотеке (Plex Discover / plex.tv)

Провайдеры объявляют action `scrobble` (`GET https://discover.provider.plex.tv/` и
`GET https://metadata.provider.plex.tv/` → `MediaProvider.Feature[type=actions]`):

```
PUT https://discover.provider.plex.tv/actions/scrobble?identifier=tv.plex.provider.discover&key={plexId}
    X-Plex-Token: <токен владельца>
```
(путь и параметры — как у веб-клиента: `key` action'а провайдера + `identifier` провайдера + `key`;
HTTP-метод веб-клиента из минифицированного кода однозначно не виден, python-plexapi использует **PUT**:
`PUT https://metadata.provider.plex.tv/actions/scrobble?identifier=com.plexapp.plugins.library&key={plexId}`.
Если PUT вернёт 404/405 — пробовать GET. Не проверялось вживую.)
Снять отметку — `/actions/unscrobble`. `plexId` — 24-символьный hex из `plex://movie/<plexId>`.

Как найти plexId:
- по IMDb id: `GET https://metadata.provider.plex.tv/library/metadata/matches?type=1&guid=imdb://tt…` → `ratingKey`;
- по названию: `GET https://discover.provider.plex.tv/library/search?query=<текст>&searchTypes=movies&searchProviders=discover&limit=10&includeMetadata=1`
  (`searchTypes=tv` для сериалов) → `SearchResults[].SearchResult[].Metadata.ratingKey`, сверить год/тип.

Проверка: `GET https://discover.provider.plex.tv/library/metadata/{plexId}/userState` →
`UserState[0].viewCount ≥ 1`, `viewState = "complete"`; запись появится в `watchHistory` (GraphQL выше).

Для сериала целиком — тот же вызов с id сериала (`plex://show/…`); для одной серии — id серии.

## Предложение по схеме (не применено — `films/db.py` не трогал)

Временное хранение описано в docstring `films/sources/plex.py`. Предлагаемые колонки:

`plex_items`: `plex_guid TEXT` (индекс; сейчас первый элемент `guids`), `library_id INTEGER`,
`video_codec`, `video_profile`, `video_bit_depth`, `video_kbps`, `h264_equiv_kbps` (как в `releases`),
`width`, `height`, `container`, `size_bytes`, `duration_min`, `forced_subs TEXT` (JSON),
`files TEXT` (JSON всех частей/версий), `season`, `episode`, `show_title` (для `episode-like`),
`original_title`, `edition`, `added_at`, `present INTEGER` / `removed_at TEXT` (элемент исчез из Plex —
сейчас видно только по старому `synced_at`), `audio` — только аудиодорожки.

`plex_history`: `history_id INTEGER` (номер из historyKey / id активности plex.tv — вместо маркеров в `guids`),
`view_type TEXT` (played / marked / state), `library_section_id INTEGER`, `device_id INTEGER`,
`season INTEGER`, `episode INTEGER`.
