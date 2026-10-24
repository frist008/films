# films

Личная база зарубежных фильмов, сериалов и мультфильмов с 2024 года (без верхней границы): раздачи с торрент-трекеров,
метаданные и рейтинги, сверка с медиасерверами и магазинами, итоговая таблица с сортировкой и плейлисты трейлеров.
Проект некоммерческий; ведётся с Claude Code (правила — [CLAUDE.md](CLAUDE.md), ход работ — [PLAN.md](PLAN.md)).

## Источники данных

| Источник | Что берём |
|---|---|
| [rutor.info](https://rutor.info), [rutracker.org](https://rutracker.org) | раздачи: качество, HDR/Dolby Vision, озвучка, размер |
| [TMDB](https://www.themoviedb.org) | роли, режиссёры, жанры, страны, студии, названия RU/UK, трейлеры. *This product uses the TMDB API but is not endorsed or certified by TMDB.* |
| [IMDb](https://www.imdb.com) | рейтинги и голоса (датасеты IMDb, личное некоммерческое использование), технические характеристики |
| [Rotten Tomatoes](https://www.rottentomatoes.com) | Tomatometer и Popcornmeter |
| YouTube / Google TV | русские трейлеры, цены и наличие в магазине |
| Wikidata / Википедия | состав франшиз и вселенных |
| Plex, Jellyfin, qBittorrent (локально) | что скачано и просмотрено |

## База

`data/films.db` (SQLite) в git не кладётся — слишком большая. Её текстовая копия лежит в `backup/`:
`schema.sql` и файлы `<таблица>.<NNN>.jsonl` (строка = строка таблицы). Кэши сторонних сайтов (страницы раздач, ответы
TMDB, датасеты IMDb, страницы RT) не публикуются; после восстановления они скачиваются заново.

```bash
python -X utf8 -m films.backup restore data/films.db   # восстановить базу из backup/
python -X utf8 -m films.backup export                  # обновить backup/ из базы
```

Секреты и личные идентификаторы в репозиторий не попадают (`.env`, `secrets/`, `config/private.toml`); перед каждым
push работает `python -X utf8 -m films privacy-check`.
