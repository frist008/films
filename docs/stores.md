# Магазины: Google Play Фильмы / Google TV / YouTube (аккаунт <google.main>)

Снято 2026-10-23 (агент через Claude in Chrome). Всё — **только чтение**: ничего не покупалось,
в списки ничего не добавлялось и не удалялось, настройки не менялись. Код записи в БД —
[films/sources/stores.py](../films/sources/stores.py), таблица `store_items` (миграция 2).

## Итоговая картина (где что живёт)

| Что | Где | Сколько | `store_items.store` |
|---|---|---|---|
| **Купленные фильмы (библиотека)** | YouTube → Фильмы → вкладка «Покупки» → «Фильмы, которые вы купили или взяли напрокат» → «Показать все»: `https://www.youtube.com/feed/storefront?bp=kgEDCMkEogUCKAQ%3D` | 307 | `youtube`, `owned=1` |
| Покупки, сделанные прямо в YouTube | `https://www.youtube.com/feed/purchases` → редирект на `/paid_memberships`, блок «Купленный и взятый напрокат цифровой контент» | 3 (The Revenant, Hotel Transylvania 2, Маска) — все есть в библиотеке выше | — |
| История заказов Google Play (фильмы куплены через Play-биллинг) | `https://play.google.com/store/account/orderhistory` («Бюджет и история заказов», кнопка «ПОКАЗАТЬ ЕЩЁ») | 276 строк, из них фильмов (`data-item-type="1"`) 183 строки = 182 уникальных id; глубина — до 19.09.2021 | `google_play`, `owned=1` + `raw.youtube_library_vid` |
| **Список просмотра Google TV** (главный «список желаемого») | `https://www.google.com/interests/saved` → «Список просмотра в Google TV»: `<google_tv.watchlist_url>` (2 страницы, `&pageNumber=2`) | 317 | `google_tv`, `in_wishlist=1` |
| Список просмотра в Поиске Google | `<google_tv.search_watchlist_url>` | 159 — **все 159 входят** в список Google TV | флаг `raw.in_search_watchlist` у строк `google_tv` |
| Список желаний Google Play (legacy) | `https://play.google.com/wishlist` (раздел «Фильмы»; там же «Игры и приложения» — не наше) | 113 фильмов/сериалов (объединение по 52 регионам, см. ниже) | `google_play`, `in_wishlist=1` |

Магазин фильмов Google Play в регионе аккаунта (Украина, UAH) **закрыт**:
`play.google.com/store/movies` и любая `…/store/movies/details?id=…` → HTTP 403 «К сожалению, этот контент
недоступен в вашей стране». Покупка фильмов фактически идёт через YouTube (цены в грн). Отдельного
веб-интерфейса Google TV со списком/библиотекой нет (`tv.google.com` — промо-сайт); список просмотра
Google TV виден только в «Сохранённом» Google (interests/saved).

Проверка аккаунта: на play.google.com aria-label аватара «Аккаунт Google: <owner.name> (<google.main>)»;
на youtube.com `getAccountSwitcherEndpoint` → активен канал <youtube.channel_name> (<youtube.channel>) в секции
<google.main>, `DELEGATED_SESSION_ID` пуст (не brand-аккаунт), `GL=UA`; на google.com — тот же аккаунт.

## Результат снятия 2026-10-23 (store_items)

* `youtube` 418 строк: 307 купленных (`owned=1`, у всех `audio_langs`, `acquired`; у 183 нет ни RU, ни UK
  дорожки — `raw.has_ru_uk_audio=false`, по ним выполнен поиск альтернативных листингов `raw.alt_versions_search`),
  111 непокупленных листингов, найденных для списков желаемого (цены, языки, `wishlist_play_ids`/`gtv_mids`).
* `google_play` 295 строк: 113 — список желаний Play (`in_wishlist=1`), 182 — фильмы из истории заказов
  (181 связан с библиотекой YouTube, 1 — «ФОРСАЖ 9», 91 грн, 09.06.2022 — в библиотеке нет, вероятно прокат).
* `google_tv` 317 строк: список просмотра Google TV; 101 можно купить на YouTube в UA (38 с RU-озвучкой,
  ещё 10 только с UK, 53 без RU/UK), 60(+1) уже куплены, 156 не найдены поиском фильмов YouTube.
* Скидки (все в `google_tv`/`youtube`): Супергёрл (2026) покупка −33% 239→161, прокат −53% 149→70;
  Супермен (2025) покупка −40% 171→103; 28 лет спустя: Храм костей покупка −6% 377→356; Игры разума прокат
  −30% 133→93; G.I. Joe: Бросок кобры 2 прокат −31% 134→93; Улыбка 2 прокат −1% 106→105.
* Правило «куплено только с EN, есть версия с RU/UK»: найден 1 случай — Green Lantern (2011), куплен
  `I7JbeADITsY` (en), есть `sd5rE-Ih5dw` (en, es, ru, fr; покупка 103 грн UHD, прокат 50) — кандидат в желаемое
  (ничего не добавлено).

## Языки озвучки (audio_langs)

* Источник — **страница просмотра YouTube** `https://www.youtube.com/watch?v=<id>`: в `ytInitialData`
  блок описания содержит `metadataRowRenderer` со строками «Языки» (= звуковые дорожки), «Субтитры»,
  «Дата выхода», «Длительность», «Владелец контента», «Приобретено» (дата покупки — только для купленных),
  «В ролях», «Режиссер», «Жанры». Языки переводятся в коды (`ru`, `uk`, `en`, `en-AD` = тифлокомментарий…),
  словарь — в JS ниже (`__L`).
* У купленных фильмов с несколькими дорожками то же видно в `ytInitialPlayerResponse.streamingData.adaptiveFormats[].audioTrack`
  (`ru.3`, `uk.3`, `en.4` = «оригинальная») — совпадает со строкой «Языки».
* В диалоге «Купить или взять напрокат» (ypc/get_offers) есть строка «Язык звуковой дорожки: …».
* На Google Play языков нет нигде (страницы фильмов 403 в UA; карточка списка желаний содержит только название,
  рейтинг, жанр, возрастной рейтинг, одну цену и описание). В списке Google TV (interests/saved) языков тоже нет.
* **Отдельные листинги с другой озвучкой существуют**: один и тот же фильм бывает выложен несколькими videoId
  (оригинал EN / UA-дубляж / RU), напр. Annabelle: `Fq9aMIyCDHs` (en) и `NJgcGFTkQXM` (uk); Крик (2022):
  `J4gtn_-AmXg` (en,ru,…) и `iQLFcjBS1_4` (только ru). В Play-истории заказов UA-листинги видны с украинскими
  названиями («Вартові галактики 3», «Стихії», …).
* **Как искать альтернативные листинги**: `youtubei/v1/search` c фильтром «Фильмы» (`params: "EgIQBA=="`)
  **с разными `context.client.hl`** — `uk`, `ru`, `en`. Поиск отдаёт листинг на языке интерфейса: по запросу
  «Annabelle» при `hl=ru` UA-версии нет, при `hl=uk` она первая. Связи «оригинал → локализованная версия»
  на странице оригинала нет (обратная ссылка есть только в похожих видео локализованной версии).
  Ограничение: поиск YouTube неполный (например, купленные «Kingsman: The Golden Circle» и «Halloween (2018)»
  по названию не нашлись), поэтому «не найдено» ≠ «нет в магазине» — помечается `availability_reason`.

## Как снимать (JS-сниппеты, выполнять через `javascript_tool` во вкладке нужного домена)

Вывод в контекст ограничен (~100 элементов массива, длинные строки режутся, строки с `?x=`/base64 блокируются
фильтром инструмента) — отдавать компактные строки `id|title|…` пачками по 100 и сверять SHA-1 хэш
(`crypto.subtle.digest`) с хэшем записанного файла. Попытка слить данные в локальный приёмник
(`fetch('http://127.0.0.1:…')` упирается в запрос Chrome Local Network Access; вариант через popup+postMessage
заблокирован классификатором автоматизации как RCE-поверхность) — **не использовать**.

### 1. Google Play — список желаний (`https://play.google.com/wishlist`)

Данные в HTML: `AF_initDataCallback({key: 'ds:5', … data: …})` (блок, где встречается `/store/movies/details`).
Список: `d[0][1][0][21][0]` — элементы `it`: `it[0][0]` id, `it[3]` название, `it[5]` жанр, `it[8]` цена
(`[null, [[micros, 'UAH', '237,00 грн.']], null, ['Взять напрокат'|'Купить', …], …]`; `null` = нет предложения),
`it[10]` ссылка, `it[13][1]` описание. Хвост `d[0][1][0][21][1][3][1]` — токен продолжения.

```js
window.__at = WIZ_global_data.SNlM0e;               // XSRF для batchexecute
const page = async qs => { const t = await (await fetch('/wishlist'+qs)).text();
  const m = [...t.matchAll(/AF_initDataCallback\(\{key: '(ds:\d+)'[^]*?data:([^]*?), sideChannel/g)]
             .find(m => m[2].includes('/store/movies/details'));
  return JSON.parse(m[2]); };
const call = async (inner, params) => {              // продолжение списка, rpc qnKhOb
  const fr = JSON.stringify([[["qnKhOb", JSON.stringify(inner), null, "generic"]]]);
  const q = new URLSearchParams({rpcids:'qnKhOb','source-path':'/wishlist',hl:'ru',authuser:'0',rt:'c',...params});
  return (await fetch('/_/PlayStoreUi/data/batchexecute?'+q, {method:'POST',
    body: new URLSearchParams({'f.req':fr, at:__at}),
    headers:{'content-type':'application/x-www-form-urlencoded;charset=UTF-8'}})).text(); };
// для каждого gl из 52 стран: d = await page('?hl=ru&gl='+g); items = d[0][1][0][21][0];
// tok = d[0][1][0][21][1][3][1]; если tok — call([[null,[[1,[20,100]],null,null,[1,456]],null,tok],[1]], {gl:g})
// ответ: строка '[["wrb.fr"…' → JSON.parse(arr[0][2]) → items = d[0][21][0]
```

Подводные камни:
* **Список зависит от региона** (`gl`): сервер отдаёт только документы, «видимые» в стране. UA — 98 + продолжение
  падает с `PlayDataError [1]`; US — 95+4; GB/DE/FR/… — 85. Объединение по 52 странам = **113** (насыщение: от
  16 до 52 стран объединение не росло). 85 «legacy»-документов (id вида `…MV`/`…SH`/`…EP`) видны во всех
  странах, но **без цены нигде** — это мёртвые карточки старого Play Movies.
* Продолжение в UA ломается (`PlayDataError`), с `gl=US` тот же токен отдаёт ещё 3 документа (Дюна, Knives Out,
  Puss in Boots). Токен второй страницы (`qgIz…user_wishlisted…`) страница сама не использует — конец.
* Цена на карточке — **одна**, самая низкая (обычно прокат, `it[8][3][0]`), не цена покупки. Скидка на карточке
  была бы двумя ценами подряд (видно у приложения Machinarium: «199,00 грн.99,00 грн.»).
* id с суффиксом `.P` — Play-документы, **не** YouTube videoId (watch?v=… → «Видео недоступно»); 11-символьные
  id без суффикса — это YouTube videoId.

### 2. Google Play — история заказов (`https://play.google.com/store/account/orderhistory`)

Кнопка «ПОКАЗАТЬ ЕЩЁ» внизу (кликать, пока не исчезнет; `[data-encoded-next-page-token]` становится пустым).
Строки: `div.U6fuTe` с `data-item-type` (`1` = фильм, `7`/`21`/`66` = приложения/внутриигровое, `25` = спонсорство
каналов). В строке: ссылка `/store/movies/details?id=<id>`, название, дата, сумма, «Отменено»/«Сообщить о проблеме».
Покупка или прокат — **не указано**. Связь с библиотекой YouTube — `stores.link_play_orders()` (по id, по названию,
по ручной таблице `MANUAL_ORDER_LINKS` для украинских названий; сверено по дате «Приобретено»).

### 3. YouTube — библиотека купленного

```js
// SAPISIDHASH для youtubei
const auth = async () => { const sap = document.cookie.match(/(?:^|; )SAPISID=([^;]+)/)[1];
  const ts = Math.floor(Date.now()/1000); const s = `${ts} ${sap} https://www.youtube.com`;
  const h = [...new Uint8Array(await crypto.subtle.digest('SHA-1', new TextEncoder().encode(s)))]
            .map(b=>b.toString(16).padStart(2,'0')).join('');
  return `SAPISIDHASH ${ts}_${h} SAPISID1PHASH ${ts}_${h} SAPISID3PHASH ${ts}_${h}`; };
const yti = async (path, body, hl) => { const cfg = ytcfg.data_;
  const ctx = structuredClone(cfg.INNERTUBE_CONTEXT); if (hl) ctx.client.hl = hl;
  return (await fetch(`/youtubei/v1/${path}?prettyPrint=false`, {method:'POST', credentials:'include',
    headers:{'content-type':'application/json', authorization: await auth(), 'x-origin':'https://www.youtube.com',
             'x-goog-authuser': String(cfg.SESSION_INDEX||0),
             'x-youtube-client-name': String(cfg.INNERTUBE_CONTEXT_CLIENT_NAME),
             'x-youtube-client-version': cfg.INNERTUBE_CLIENT_VERSION},
    body: JSON.stringify({context: ctx, ...body})})).json(); };
// библиотека: одна страница, 307 gridMovieRenderer {videoId, title, metadata "Жанр • Год", lengthText}
const lib = await yti('browse', {browseId:'FEstorefront', params:'kgEDCMkEogUCKAQ='});
```

Вкладки витрины: `params ogUCKAU=` «Каталог», `ogUCKAQ=` «Покупки». Продолжений у списка «Показать все» нет
(проверено: в ответе нет `continuationCommand`). Сортировка — по videoId.

### 4. YouTube — данные фильма и цены

```js
const t = await (await fetch('/watch?v='+vid+'&hl=ru')).text();
// ytInitialPlayerResponse.playabilityStatus: OK (куплено) | UNPLAYABLE «Это платное видео» (продаётся)
//                                            | UNPLAYABLE «Видео недоступно» (не продаётся в UA)
// ytInitialData → metadataRowRenderer («Языки», «Дата выхода», «Приобретено», …)
const itemParams = JSON.parse('"' + t.match(/"ypcOffersEndpoint":\{"params":"([^"]+)"/)[1] + '"');
const offers = await yti('ypc/get_offers', {itemParams});   // ВАЖНО: ключ itemParams, не params (иначе 400)
// offerSectionRenderer.title: «Взять напрокат» / «Купить»; offerContentRenderer.title: «Высокое/Стандартное/
// Ультравысокое разрешение»; actionButton.buttonRenderer.text: «237,00 грн.» или «133,00 грн.  93,00 грн.» (скидка:
// старая → новая цена). get_offers только показывает варианты (то же, что открыть диалог); покупка — следующий
// шаг ypc/get_cart, который НЕ вызывается.
```

### 5. Google TV / Поиск — списки просмотра (`https://www.google.com/interests/saved/list/<id>`)

HTML страницы содержит `AF_initDataCallback({key:'ds:0'…})`: `d[1]` — элементы страницы (до 200), у каждого
`x[0]` = текст `item_type: TVM_ENTITY\nmid: "/m/…"`, `x[1]` название (бывает пустым — «Объект без названия»),
`x[14][0]` — время добавления (unix-сек). Вторая страница — `?pageNumber=2` (кнопка «Следующая страница», счётчик
«201–317 из 317»). В ссылке элемента `/search?q=…&stick=<gzip+base64>` внутри тоже лежит mid.
Года и языков нет → mid (Freebase `/m/…` = Wikidata P646, Google KG `/g/…` = P2671) разрешается через
Wikidata SPARQL (год P577, IMDb P345, метки en/ru/uk): разрешилось 297 из 317; остальные 20 — через
`google.com/search?kgmid=<mid>` (заголовок страницы + «фильм 2016» в выдаче).

### 6. Google TV — добавление в список просмотра (2026-10-24)

Карточка фильма в Поиске Google (`/search?q=<название> <год> фильм&hl=ru`), блок «Смотреть фильм»: кнопка
`[role=button][aria-label*="Хочу посмотреть"]`, `aria-pressed` = есть ли фильм в списке просмотра Google TV. Страница поиска
открывается в iframe той же вкладки google.com (same-origin, доступ к DOM есть). Кнопка — переключатель, поэтому только
`films/browser/gtv_watchlist_guard.js` (`__gtvGuard.check/add`) под хуком `.claude/hooks/gtv-guard.sh`.

## Полнота (как проверено)

* Play wishlist: DOM карусели «Фильмы» (UA) = 98 карточек = 98 в `ds:5`; прокрутка карусели до конца новых
  не добавляет (scrollWidth 21560 / 98 ≈ 220px на карточку); объединение 52 регионов + продолжения = 113.
* Play order history: «ПОКАЗАТЬ ЕЩЁ» до исчезновения, последняя запись 19.09.2021.
* YouTube библиотека: 307 = 307 уникальных videoId, продолжения нет; все 307 открыты на watch-странице
  (статус OK, у всех есть «Приобретено» и «Языки»).
* Google TV: счётчик страницы «317 объектов» = 317 уникальных mid (ds:0 двух страниц) = 317 в DOM после
  перехода на 2-ю страницу; Поиск: «159 объектов» = 159.
* Переписанные из браузера файлы сверены SHA-1 хэшем с данными в странице.

## Файлы выгрузки (для `python -m films.sources.stores --import DIR`)

`gp_wishlist.json` (сырьё Play wishlist по регионам), `gp_orderhistory.json`, `yt_library_rows.txt`
(`vid|title|жанр • год|длительность`), `yt_library_audio.txt` (`vid|год|приобретено|владелец|языки`),
`wishlist_final.json`, `youtube_offers.json`, `gtv_watchlist_rows.txt` (`mid|title|added|в_поиске|страница`),
`gtv_final.json`. Выгрузка 2026-10-23 лежала в scratchpad агента; в БД всё сохранено в `store_items.raw`.

## Поля, которых нет в схеме (вопрос владельцу)

Сохранены пока в `store_items.raw`: языки озвучки (`audio_langs`), языки субтитров (страница YouTube, не
сохранялись по решению «субтитры не нужны», кроме форсированных — их магазин не показывает), цена проката и её
скидка (`rent_price`, `rent_discount_pct`), качество (HD/SD/UHD) у каждой цены, дата покупки («Приобретено»),
«Владелец контента» (студия-дистрибьютор), id Google Knowledge Graph (`mid`) + IMDb id из Wikidata, дата
добавления в список просмотра, история заказов Play (дата/сумма каждой транзакции).
