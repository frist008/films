# Google: таблица, OAuth, секреты

Все перечисленные аккаунты принадлежат владельцу (<owner.name>). Любой, кто продолжает проект, — это он сам
или Claude по его поручению, поэтому доступ к `<google.cloud>` есть у всех участников.

## Кто где

| Что | Аккаунт | Почему |
|---|---|---|
| Сама таблица Google Sheets (владелец файла) | **<google.main>** | основной аккаунт владельца |
| Проект Google Cloud `films-db` (Sheets/Drive API + OAuth-клиент) | **<google.cloud>** («Gandalf») | у <google.main> исчерпан лимит проектов GCP (проекты в ожидании удаления занимают лимит 30 дней), см. ниже |
| Секреты проекта (ключи, OAuth-клиент, токены) | **заметки Google Keep аккаунта <google.cloud>** | в git секреты не хранятся никогда |

OAuth-клиент из проекта на <google.cloud> авторизует **<google.main>** (вход и согласие — под <google.main>), поэтому таблица
создаётся и живёт в Drive <google.main>. Чей проект в GCP — значения не имеет.

## Секреты

В git (открытый репозиторий) **никогда** не попадают: `.env`, `secrets/`, токены, пароли, ключи.
Хранилище секретов — заметка Google Keep «films — секреты» в аккаунте **<google.cloud>**.

| Секрет | Где на машине | Откуда взять |
|---|---|---|
| `TMDB_API_KEY` | `.env` | заметка Keep; первоисточник — themoviedb.org → Настройки → API (аккаунт TMDB <plex.user>, вход через Google <google.main>) |
| OAuth-клиент Google (JSON) | `secrets/google_oauth_client.json` | заметка Keep; первоисточник — console.cloud.google.com (<google.cloud>) → films-db → APIs & Services → Credentials |
| OAuth-токен <google.main> | `secrets/google_token.json` | создаётся сам при первом запуске скрипта таблицы (вход <google.main> в браузере); можно не хранить |
| Plex-токен | не хранится | читается из реестра Windows на машине с Plex |

Claude не вписывает ключи/токены в веб-формы и не может прочитать их со страниц (Chrome их блокирует) —
значения в Keep вставляет владелец; Claude только создаёт шаблон заметки и кладёт значения в `.env`/`secrets/`,
когда владелец даёт их в чате или файле.

## Новая машина / другой Claude-аккаунт

1. `git clone` репозитория, `pip install -r requirements.txt`.
2. Открыть заметку Keep «films — секреты» (<google.cloud>) → создать `.env` по `.env.example` и `secrets/google_oauth_client.json`.
3. `python -X utf8 -m films init-db` (схема), дальше по скилам.

## История (2026-10-23)

- У <google.main> 16 проектов в GCP, лимит исчерпан. Владелец был только в 5 (Cocktails in One, Action Record RCU UA,
  Quest Helper Go, N4K Game Collection, Sky Children of the Light Map).
- 8 чужих спам-проектов (My First Project, admob, My google, Bongtar Channel, Google adsen, Elimuhub, Facebook,
  My Project 36780) были видны через открытую группу `google-admob-ads-sdk@googlegroups.com`, которую спамеры
  добавили в свои проекты. Владелец вышел из группы — проекты исчезают из списка.
- CarSoup 3, CarSoup android (владелец владелец-чужой аккаунт) и RandeVu (владелец-чужой аккаунт): <google.main> там
  «Редактор»; ни GCP, ни Firebase не позволяют редактору выйти самому — только владелец может убрать.
- N4K Game Collection и Sky Children of the Light Map удалены владельцем (в ожидании удаления 30 дней, можно восстановить).
- Новый проект на <google.main> всё равно не создаётся (удаляемые занимают лимит) → проект `films-db` на <google.cloud>.
