# Исследование: коэффициенты кодеков (→ H.264-эквивалент) и детектор 4K-апскейлов

Дата: 2026-10-23. Статус: исследование, решения за владельцем (см. «Открытые вопросы» в конце).
Обозначения уверенности: **высокая** — несколько независимых первоисточников сходятся; **средняя** — первоисточники
есть, но разброс большой или условия тестов далеки от наших; **низкая** — косвенные данные / вторичные источники.

---

## Часть 1. Коэффициенты кодеков: `h264_equiv_kbps = video_kbps × factor`

### 1.1. Как пересчитывать BD-rate в коэффициент

BD-rate «кодек X экономит s % битрейта относительно AVC при равном качестве» означает: X при битрейте R ≈ AVC при
битрейте R / (1 − s). Отсюда `factor = 1 / (1 − s)` (для кодеков лучше AVC, s > 0).
Для кодеков хуже AVC (MPEG-2, XviD) источники дают «AVC экономит s % относительно X», тогда `factor = 1 − s`.

| экономия s | 20 % | 25 % | 33 % | 37,5 % | 40 % | 44 % | 50 % | 55 % | 59 % |
|---|---|---|---|---|---|---|---|---|---|
| factor = 1/(1−s) | 1.25 | 1.33 | 1.5 | 1.6 | 1.67 | 1.8 | 2.0 | 2.2 | 2.44 |

### 1.2. Рекомендуемая таблица

«≤1080p» — всё с высотой кадра < 1440; «2160p» — высота ≥ 1440 (в т.ч. 2560×1440, кропнутые 3840×1600 и т.п. —
смотреть на ширину ≥ 2560).

| Кодек (MediaInfo `Format`) | ≤1080p | 2160p | Уверенность | Главные источники (см. 1.3) |
|---|---|---|---|---|
| AVC / H.264 8-bit (High) | **1.0** | **1.0** | по определению | — |
| AVC / H.264 10-bit (High 10, «Hi10P») | **1.1** | **1.1** | низкая | [S10], [S11] — +5…12 % за 10-битное кодирование |
| HEVC / H.265 10-bit (Main 10) | **1.6** | **1.8** | средняя | [S1], [S2], [S3], [S4], [S5], [S6], [S7] |
| HEVC / H.265 8-bit (Main) | **1.5** | **1.7** | низкая–средняя | то же + [S10], [S11] |
| AV1 (8 и 10 бит) | **2.0** | **2.2** | низкая–средняя | [S6], [S7], [S8], [S9], [S12], [S13] |
| VP9 (profile 0/2) | **1.4** | **1.5** | низкая–средняя | [S5], [S8] |
| VVC / H.266 | **2.4** | **2.7** | низкая (и практически не встречается) | [S6], [S7], [S14], [S15] |
| MPEG-4 ASP (XviD / DivX) | **0.6** | — | средняя | [S1], [S16] |
| VC-1 (WMV9 / Blu-ray VC-1) | **0.75** | — | низкая | [S16], [S17] |
| MPEG-2 (`MPEG Video` v2) | **0.5** | **0.5** | средняя (направление — высокая) | [S1], [S18], [S19] |
| неизвестный / прочий | `NULL` (не считать) | | | писать в лог, добавлять в конфиг вручную |

Предлагаемый вид `config/settings.toml` (сам конфиг этим исследованием **не правится** — решение за владельцем):

```toml
[codec_efficiency]
# h264_equiv_kbps = video_kbps × factor; ключ = нормализованный кодек[_битность]; [uhd] — для высоты ≥1440.
# Обоснование и источники: docs/research-codecs-upscale.md
AVC = 1.0
AVC_10bit = 1.1
HEVC = 1.5          # 8-bit
HEVC_10bit = 1.6
AV1 = 2.0
VP9 = 1.4
VVC = 2.4
MPEG4_ASP = 0.6     # XviD / DivX
VC1 = 0.75
MPEG2 = 0.5

[codec_efficiency.uhd]
HEVC = 1.7
HEVC_10bit = 1.8
AV1 = 2.2
VP9 = 1.5
VVC = 2.7
# AVC/MPEG2 — как в основном разделе
```

### 1.3. Обоснование по кодекам

**HEVC против AVC** — данных больше всего, но разброс большой:

| Источник | Что сравнивали | Экономия HEVC | factor |
|---|---|---|---|
| [S1] Ohm, Sullivan и др., IEEE TCSVT 2012, табл. VI | HM vs JM, «развлекательный» сценарий, HD/WVGA, PSNR | 35,4 % | 1.55 |
| [S1] там же, субъективный тест | HM-5.0 vs JM, HD | ≈50 % (48–65 %, в среднем 53 %) | 2.0–2.1 |
| [S2] Tan и др., IEEE TCSVT 2016 (верификационный тест JCT-VC) | субъективно / PSNR | 59 % / 44 % | 2.44 / 1.8 |
| [S3] Weerakkody и др., GlobalSIP 2014 (UHD) | субъективно, UHD | ≈50 % в целом, ≈60 % в области высокого качества | 2.0–2.5 |
| [S4] Netflix (De Cock и др., SPIE 2016) | x265 vs x264, placebo, VMAF | 35,4–53,3 % | 1.55–2.14 |
| [S5] Streaming Learning Center (Ozer) | x265 vs x264, VMAF 83–94 | 39 % | 1.64 |
| [S6] исследование FFmpeg-кодеков (MDPI, через обзор Ozer 2024) | x264 vs x265, пресеты по умолчанию, PSNR | x264 дороже на 70,7 % → 41,4 % | 1.71 |
| [S6]/[S7] MSU 2023–2024, FullHD, VMAF / YUV-SSIM | x264 vs x265, «slow» | x264 ≈2,46× / ≈2,7× | 2.46–2.7 |
| [S7] MSU 2023–2024, 4K 10-bit, YUV-SSIM (снято с графика) | x264 vs x265, «slow 1 fps» | x264 ≈2,05× | ≈2.05 |

Почему выбрано **1.6 (≤1080p) / 1.8 (2160p)**, т.е. экономия ≈37,5 % / 44 %, ближе к нижней части диапазона:
- Раздачи на трекерах — это высокие битрейты (BDRip, WEB-DL, ремуксы). Ohm и др. [S1] прямо пишут, что выигрыш при
  низких битрейтах выше среднего, т.е. при высоком качестве он меньше; на форумах энкодеров (Doom9) это же
  отмечают для почти прозрачного качества. Тесты MSU/VMAF ведутся в стриминговом диапазоне и дают завышенные для
  нас цифры. (Есть и противоречие: UHD-тест [S3] даёт ≈60 % «в области высокого качества» — но это субъективная
  шкала стриминга, а не прозрачность ремукса. **Неопределённость.**)
- Все первоисточники сходятся, что на высоком разрешении HEVC выигрывает больше ([S1]: «especially effective for
  high-resolution video»; MSU 4K). Отсюда отдельная строка 2160p.
- 8-bit vs 10-bit HEVC: 10-битное кодирование даже 8-битного исходника обычно экономит ≈5–12 % (вендорские/форумные
  данные [S10], [S11], не проверены независимо) → для 8-bit HEVC −0.1. HDR-контент всегда 10-bit.

**AV1.** Facebook 2018 [S8]: libaom против x264 High — экономия 45,7–47 % (PSNR/SSIM), против x264 Main 50 %, против
libvpx-vp9 30–40 %. FFmpeg-исследование [S6]: AV1 −25 % к x265. MSU 2023 (график [S7]): SVT-AV1 ≈0,64× x265 на FullHD
и ≈0,5× на 4K (YUV-SSIM). Бристоль/ICIP 2019–2020 [S12]: libaom против HM субъективно «без значимой разницы».
Netflix 2025 [S13]: AV1-сессии тратят ≈1/3 меньше трафика, чем AVC и HEVC, при VMAF выше на 4,3 и 0,9 (это лестница
битрейтов, а не BD-rate). Итог: AV1 ≈ HEVC × 1.25 (−20…25 % к HEVC) → **2.0 / 2.2**. Оговорки: у AV1-WEB-DL (Netflix)
используется синтез зерна (film grain synthesis) — битрейт выглядит низким, а зерно дорисовывается декодером;
любительские пережатия в SVT-AV1 часто с сильным шумоподавлением. Поэтому уверенность низкая–средняя.

**VP9.** Netflix 2016 [S5 через streamingmedia]: VP9 против x264 по VMAF −31,6 % (720p) и −42,6 % (1080p); x265 лучше VP9
в среднем на 20 % (VMAF), но на 1080p по MS-SSIM VP9 даже выигрывал. Facebook [S8]: из AV1 −46 % к x264 High и −33 % к
VP9 следует VP9 ≈ −19 % к x264. Среднее → **1.4 / 1.5**. На трекерах встречается редко (рипы YouTube).

**VVC / H.266.** Верификационные тесты JVET (сводка 3GPP TR 26.955 [S14]): VVC против HEVC −43 % (VTM-10) и −49 %
(VVenC-0.1) на UHD SDR, −49…51 % на UHD HDR. Независимое субъективное исследование [S15]: −37 % HD, −40 % UHD.
MSU 2023 4K: VVenC ≈0,6× x265. FFmpeg-исследование [S6]: −53 % к x265 (PSNR). Практический ориентир ≈ −35…40 % к HEVC →
HEVC × 1.5 → **2.4 / 2.7**. В раздачах 2024–2026 практически не встречается (в кэше rutor — 0 из 1130 описаний).

**MPEG-4 ASP (XviD/DivX).** [S1], табл. VI: AVC HP экономит 44,5 % относительно MPEG-4 ASP → 0.555. MSU 2006 [S16]
(субъективно, сцены «Терминатор 2» / «Матрица»): x264 на 690 кбит/с оценён выше XviD/DivX/WMV9 на 1024 кбит/с → ≤0.67.
Выбрано **0.6**. Важно: XviD ещё живой формат на rutor — в кэше 152 из 1130 описаний раздач 2024–2026 содержат XviD/DivX
(HDRip, DVDRip-раздачи).

**VC-1.** Строгого BD-rate-сравнения VC-1 с AVC **не нашёл**. Косвенно: MSU 2006 [S16] — WMV9 на 1024 кбит/с хуже x264
на 690 (≤0.67, но при низких битрейтах SD); рассылка FFmpeg 2006 ссылается на исследование Tandberg, где AVC Main и WMV9
дали «сравнимое» качество (вторичный источник [S17]); High-профиль AVC примерно на 8 % лучше Main (вывод из [S8]).
Выбрано **0.75**, уверенность низкая. В раздачах 2024–2026 VC-1 практически не встречается (только старые Blu-ray).

**MPEG-2.** [S1], табл. VI: AVC HP экономит 55,4 % относительно MPEG-2 MP → 0.45. Wiegand и др. 2003 [S18]: AVC даёт то
же качество при «60 % или меньше» битрейта предшественников. ATEME 2009 [S19] (маркетинг): HD при ~30 Мбит/с AVC 10-bit
против 50–60 Мбит/с MPEG-2 → 0.5–0.6. Выбрано **0.5** (с поправкой на высокие битрейты Blu-ray MPEG-2).

### 1.4. Энкодер, ремуксы, HDR — что учитывать и чего не учитывать

- **Сравнение реализаций, а не стандартов.** Ozer [S6]: «There are no codec comparisons. There are only codec
  implementation comparisons». Разница между энкодерами одного стандарта бывает больше, чем между стандартами (MSU:
  лучший HEVC-энкодер Tencent «съедает» 57 % выигрыша x265 над x264). Коэффициенты в таблице — для типичных
  программных энкодеров (x264/x265/SVT-AV1) и студийных энкодеров дисков/стримингов.
- **Аппаратные энкодеры** (NVENC/QSV/AMF): MSU Part III (FPGA/железо) — выигрыш HEVC/AV1 над AVC заметно меньше,
  чем в софте [S6]. Можно было бы снижать коэффициент (например ×0.85), если MediaInfo `Writing library` не содержит
  x264/x265/SVT-AV1 и содержит NVENC, но надёжно определить энкодер нельзя → **по умолчанию не применять** (открытый вопрос).
- **Ремуксы (UHD BD / BD)** — студийные энкоды на очень высоких битрейтах; там качество близко к прозрачному и
  H.264-эквивалент — условная величина (полезна для сравнения раздач, но не означает, что x264 на этом битрейте
  выглядел бы так же). Коэффициент применять тот же.
- **Dolby Vision Profile 7 (BL+EL).** В MediaInfo rutor-раздач (проверено на «Scary Movie (2026) UHD BDRemux от селезень»)
  BL и EL сведены в один видеотрек, и `Bit rate` (61,5 Мбит/с) включает слой EL. Для FEL это может быть несколько
  Мбит/с, которые к «картинке базового слоя» не относятся. Точную долю EL по MediaInfo не узнать — **неопределённость**;
  отмечать DV P7 FEL флагом, не корректировать битрейт.
- **HDR vs SDR.** Коэффициент сравнивает кодеки, а не форматы. HDR-раздача и SDR-раздача на одинаковом
  эквиваленте не равноценны; корректировку не предлагаю.
- **Разные разрешения несравнимы.** 20 Мбит/с в 1080p и 20 Мбит/с в 2160p — очень разное качество. Если нужно сравнивать
  через разрешения — отдельная метрика «бит на пиксель» = kbps×1000 / (ширина × высота × fps).
- **Любительские AI-апскейлы** (см. часть 2): битрейт там вообще не говорит о качестве.
- **Нет битрейта в MediaInfo** (часто у MKV без тега `BPS`): считать `Stream size × 8 / Duration`; если и этого нет —
  оставить `NULL`, а не оценивать по размеру файла (в файле ещё аудио).

---

## Часть 2. Детектор 4K-апскейлов

### 2.1. Два разных вида «апскейла»

1. **Студийный апскейл** — фильм смонтирован/цветокорректирован в 2K Digital Intermediate (DI), а 4K UHD Blu-ray и 4K
   WEB-DL сделаны масштабированием этого мастера. HDR/DV и 10 бит при этом настоящие — апскейл касается только
   разрешения. В названиях раздач **не помечается** почти никогда.
2. **Любительский (AI) апскейл** — релизер сам растянул 1080p (WEB-DL/BD) до 2160p (Topaz Video AI и т.п.). На rutracker
   **помечается в названии**; на rutor в названиях фильмов не найдено ни одного.

### 2.2. Что показали проверки (2026-10-23)

**IMDb `/title/{tt}/technical/`.** Данные лежат в `#__NEXT_DATA__` →
`props.pageProps.contentData.data.title.technicalSpecifications.processes.items[]`: `{process, attributes[]}`, например
`{"process":"Digital Intermediate","attributes":["4K","master format"]}`, `{"process":"ARRIRAW","attributes":["6.5K","4.5K","source format"]}`.
На странице это видно как «Cinematographic Process: Digital Intermediate (4K, master format)». Рядом:
`negativeFormats` (Codex, 35 mm…), `cameras`, `printedFormats`.

Выборка: 60 самых популярных (по голосам из датасета IMDb в нашей БД) фильмов 2024–2026 без документалок:

| Что в IMDb | Кол-во | Примеры |
|---|---|---|
| DI 4K (master format) | 54 / 60 | Dune: Part Two, Deadpool & Wolverine, Sinners, Superman, Gladiator II, Wicked, Captain America: BNW, A Minecraft Movie, Avatar: Fire and Ash; даже ещё не вышедшие The Odyssey, Spider-Man: Brand New Day (2026) |
| DI 2K (master) | 1 / 60 | The Substance — «2K, master format» + «4K, master format, TV show scenes only» |
| DI указан без разрешения | 3 / 60 | Furiosa, Inside Out 2, Companion |
| DI не указан вовсе | 2 / 60 | One Battle After Another (есть только плёнка/процессы), The Wild Robot (тех. характеристик нет совсем) |

То есть у крупных фильмов поле заполнено в ~93 % случаев, и почти всегда это 4K — у блокбастеров 2024–2026 студийный
апскейл редок. **Не измерено** (браузерную проверку пришлось остановить): заполненность у средних и малых фильмов —
из 2 проверенных малоизвестных один «4K», у второго тех. характеристик нет совсем. Ожидаемо (но не проверено), что у малых и
неанглоязычных фильмов поле пустое гораздо чаще. Данные IMDb пользовательские; для 2026-фильмов «4K DI» внесён до выхода —
подтверждения нет.

**blu-ray.com** — у 4K-дисков есть поле `Resolution:` со значениями «Native 4K (2160p)», «Upscaled 4K (2160p)» или просто
«4K (2160p)». Страницы доступны обычным HTTP (нужен браузерный User-Agent и `Accept-Language`, иначе ответ «Error42»);
поиск: `https://www.blu-ray.com/search/?quicksearch=1&quicksearch_country=all&quicksearch_keyword=<название>&section=4kbluraymovies`.
Проверено 56 фильмов 2024–2025: найдено 40 дисков; «Native 4K» — 36, **«Upscaled 4K» — 3: Moana 2, The Wild Robot,
Zootopia 2 (все — анимация)**, «4K (2160p)» без уточнения — 1 (The Substance). Остальные найденные (Dune: Part Two, Civil War,
Furiosa, Gladiator II, Mission: Impossible 8, Inside Out 2, Captain America: BNW, Avatar: Fire and Ash и др.) — Native.
Не найдено поиском 16 (Deadpool & Wolverine, Twisters, Wicked, Nosferatu, Conclave, Anora, Longlegs, Heretic, Abigail, Sinners,
Thunderbolts*, Superman, Weapons, Lilo & Stitch, Elio, Companion) — мой поиск по точному названию не сработал (или диска нет),
нужна доработка сопоставления.
Там, где есть и IMDb, и blu-ray.com, **противоречий нет** — но они **не независимы**: рецензент blu-ray.com в обзоре
«Kingdom of the Planet of the Apes» ссылается на IMDb («according to the IMDb had a 4K DI»), а в обзоре «The Brutalist» пишет,
что «4K DI» был предустановлен в карточке, но авторитетного подтверждения он не нашёл. Т.е. «Native» на blu-ray.com —
часто просто перенос данных IMDb; «Upscaled» выглядит более осознанной пометкой.

**realorfake4k.com** — **мёртв**. По Wayback Machine: сайт работал 2016–2018 (UHD Blu-ray + стриминги Vudu/Netflix/Amazon,
формат съёмки, VFX, DI), в 2019–2020 — ошибки Cloudflare 522/523, с 2021 — припаркованный домен (Sedo), сейчас 301-редирект
на сайт отеля. Покрытия 2024–2026 нет, запросить нельзя. Остатки экосистемы: iOS-приложение «Real or Fake 4K» (550+ фильмов,
состояние неизвестно), расширение Chrome «Is it real 4K» (v1.0, 2020) — просто читает тот же IMDb; база hdtelevizija.com (2016).
Других живых баз «real vs fake 4K» с покрытием 2024–2026 **не нашёл**.

**rutor.info.** Поиск по названиям `upscale`, `Upscaled`, `AI Upscale`, `апскейл`, `2K DI` — ни одной кино-раздачи (только
музыка и мод к игре). В 414 закэшированных описаниях раздач (`release_raw`, в основном 2026) — ноль упоминаний upscale /
апскейл / Digital Intermediate / 2K DI. Типичные 4K-пометки rutor: `UHD BDRemux 2160p`, `WEB-DL-HEVC 2160p | 4K | SDR`,
`Hybrid` (сборка из нескольких источников, обычно DV из WEB + база с диска) — это **не** признаки апскейла.

**rutracker.org** (поиск `tracker.php?nm=…`, до остановки браузера): `upscale` — 50+ результатов (лимит страницы), из них
2024–2026 — единицы; `4K upscale` — 16 из 50 за 2024–2026; `апскейл` — 4 (все старые); `2K DI` — 0. Формат пометок в названии:
`… WEBRip, 2160p … 4K AI Upscale`, `[… WEB-DL, UPSCALE] [2160p HEVC Main10]`, `BDRip 2160p, HEVC, 10 bit, 4K Upscale`,
`Handmade Upscale AI`, `DVDRip --> 1080p AI UPSCALE`. Примеры 2024–2026: KPop Demon Hunters (WEBRip 2160p, «4K AI Upscale 48fps»),
Ghost Cat Anzu, The Witcher: Sirens of the Deep, Arcane S2, Devil May Cry, Avatar: Fire and Ash 3D («Handmade Upscale AI»).
Заметно, что AI-апскейлы — в основном анимация/аниме. Правила раздела UHD Video (f=718/1457) относительно апскейлов
**не проверены** (открытый вопрос).

**Стриминговые 4K WEB-DL.** 4K-версия в стриминге делается из того же мастера/DI, что и диск; если DI 2K — 4K WEB-DL тоже
апскейл (iMore 2017 [S20]: многие «4K» фильмы iTunes были мастерингом в 2K). Netflix для своих оригиналов требует камеры с
сенсором ≥ 3840 фотосайтов и 4K-поставку [S21], поэтому Netflix Originals почти наверняка 4K-мастер; для лицензионных фильмов
и прочих сервисов такого правила нет. Отдельный случай — 2160p WEB-DL малобюджетных и азиатских тайтлов, у которых
мастер HD/2K, а метаданных нет вовсе → `unknown`.

**Анализ самого видео** (resdet — частотный анализ DCT [S22]; SRDM от MSU — нейросетевой детектор [S23]) в принципе
определяет исходное разрешение по кадрам, но требует скачать фрагмент файла. Для базы в тысячи раздач — вне рамок;
возможно как ручная проверка спорных случаев.

### 2.3. Процедура принятия решения

Входы: раздача (`name`, `source_type`, разрешение, MediaInfo, текст описания), тайтл (`kind`, `is_animation`, IMDb
processes → `titles.di_resolution`), опционально blu-ray.com `Resolution:` 4K-диска.

```
Шаг A. Уровень раздачи — любительский апскейл (проверять первым, для любой 2160p/1440p раздачи):
  если name или описание соответствуют
    (?i)\b(ai[\s._-]*)?up-?scal(e|ed|ing)\b | апскейл | upconvert | topaz | video2x | real-?esrgan
  → is_upscale = TRUE, upscale_kind = 'fan'                               [надёжность: очень высокая]
  иначе если source_type ∈ {WEBRip, BDRip, HDRip} и 2160p, а у тайтла на трекерах нет ни одного
     официального 2160p (UHD BDRemux / WEB-DL 2160p)
  → is_upscale = NULL, флаг 'suspect_fan'                                  [надёжность: низкая, только подсказка]

Шаг B. Уровень тайтла — мастер (di_resolution):
  B1. IMDb processes содержит «Digital Intermediate» с разрешением:
      - только 2K / HD / 1080p                       → master = '2K'   [средне-высокая]
      - 4K и выше (без оговорок «some scenes»)       → master = '4K'   [средне-высокая; VFX внутри 4K-DI часто 2K]
      - несколько записей (2K + 4K): брать запись без ограничительных атрибутов
        («some scenes», «… scenes only», «TV show scenes only» = частичная) → её разрешение;
        если обе «полные» → master = NULL (unknown)
  B2. DI без разрешения / нет записи → blu-ray.com 4K-диск:
      - «Upscaled 4K (2160p)» → master = '2K'                              [средняя]
      - «Native 4K (2160p)»   → master = '4K'                              [низкая–средняя: часто копия IMDb/«предустановка»]
      - «4K (2160p)» / диска нет → B3
  B3. Пометки в описании раздачи («2K DI», «мастер 2K», «апскейл с 2K») → master = '2K'  [средняя; на rutor пока не встречались]
  B4. Иначе master = NULL (unknown). Подсказки (НЕ решение): анимация без данных — чаще 2K
      (Disney/DreamWorks 2024–2025 на blu-ray.com — Upscaled); съёмочный формат < 4K без DI — слабый признак.

Шаг C. Применение к официальным 2160p-раздачам (UHD BDRemux, UHD BDRip, WEB-DL 2160p) тайтла:
  master = '2K' → is_upscale = TRUE,  upscale_kind = 'studio'
  master = '4K' → is_upscale = FALSE
  master = NULL → is_upscale = NULL (unknown)
  (4K WEB-DL наследует вердикт тайтла: тот же мастер.)

Шаг D. Таблица (строка «Без апскейла», R27):
  если «лучшая» раздача 2160p и is_upscale = TRUE → добавить строку с лучшим 1080p BDRemux
  (если BDRemux ещё нет — 1080p WEB-DL? — решение владельца, см. вопросы).
  is_upscale = NULL → строку не добавлять, в колонке качества показать «4K?» / «мастер неизвестен».
```

### 2.4. Надёжность сигналов (сводно)

| Сигнал | Покрытие 2024–2026 | Надёжность | Примечание |
|---|---|---|---|
| Пометка `AI Upscale` / `Upscale` в названии раздачи | только rutracker; на rutor — 0 | очень высокая (если есть) | ловит только любительские апскейлы |
| IMDb DI «2K, master format» | крупные фильмы ~93 % заполнено; малые — не измерено | средне-высокая | пользовательские данные; бывает внесено до выхода фильма |
| IMDb DI «4K, master format» | то же | средне-высокая | VFX в 4K-DI часто 2K — «настоящий 4K» не на 100 % |
| blu-ray.com «Upscaled 4K» | только фильмы с UHD-диском | средняя | выглядит как осознанная пометка |
| blu-ray.com «Native 4K» | то же | низкая–средняя | часто дублирует IMDb, иногда «предустановлено» |
| Пометки «2K DI» в описаниях раздач | на rutor 0 из 414 | средняя | почти не встречается |
| realorfake4k.com | нет (сайт мёртв с ~2019) | — | не использовать |
| Анализ кадров (resdet/SRDM) | любая раздача, нужен файл | средняя–высокая | дорого, только ручная проверка |

---

## Источники

- [S1] J.-R. Ohm, G. J. Sullivan, H. Schwarz, T. K. Tan, T. Wiegand, «Comparison of the Coding Efficiency of Video Coding Standards — Including HEVC», IEEE TCSVT 22(12), 2012 (табл. V–VI, раздел V). PDF: http://amalia.img.lx.it.pt/~fp/cav/Additional_material/HEVC-Performance.pdf ; аннотация: https://www.microsoft.com/en-us/research/?p=798952
- [S2] T. K. Tan, R. Weerakkody, M. Mrak и др., «Video Quality Evaluation Methodology and Verification Testing of HEVC Compression Performance», IEEE TCSVT 26(1), 2016 — https://www.microsoft.com/en-us/research/?p=798808
- [S3] R. Weerakkody, M. Mrak, V. Baroncini, J.-R. Ohm, T. K. Tan, G. J. Sullivan, «Verification testing of HEVC compression performance for UHD video», IEEE GlobalSIP 2014 — https://www.microsoft.com/en-us/research/?p=798835
- [S4] J. De Cock, A. Mavlankar, A. Moorthy, A. Aaron (Netflix), «A large-scale video codec comparison of x264, x265 and libvpx for practical VOD applications», SPIE 9971, 2016 — https://opticalengineering.spiedigitallibrary.org/conference-proceedings-of-spie/9971/997116/A-large-scale-video-codec-comparison-of-x264-x265-and/10.1117/12.2238495.full ; цифры VMAF — по https://entertainment.slashdot.org/story/16/09/06/1423256/netflix-finds-x265-20-more-efficient-than-vp9
- [S5] Streaming Media, «Netflix Finds x265 20% More Efficient than VP9» (VP9 vs x264: 31,6 % / 42,6 %) — https://streamingmedia.com/Articles/Editorial/Featured-Articles/Netflix-Finds-x265-20-More-Efficient-than-VP9-113346.aspx ; Streaming Learning Center, x265 vs x264 39,06 % — https://streaminglearningcenter.com/?p=12840
- [S6] J. Ozer, «There are no codec comparisons. There are only codec implementation comparisons», 2024-10-15 (обзор MDPI-исследования FFmpeg-кодеков и MSU 2023–2024) — https://streaminglearningcenter.com/?p=19206
- [S7] MSU Video Codecs Comparison 2023–2024, FullHD и 4K (BSQ-rate относительно Reference x265; значения сняты с графиков slow23.png / 4k_slow.png, YUV-SSIM) — https://compression.ru/video/codec_comparison/2023/main_report.html , https://compression.ru/video/codec_comparison/2023/4k_report.html
- [S8] Facebook Engineering, «AV1 beats x264 and libvpx-vp9 in practical use case», 2018-04-10 — https://engineering.fb.com/2018/04/10/video-engineering/av1-beats-x264-and-libvpx-vp9-in-practical-use-case/
- [S9] Meta Engineering, «AV1 codec for Facebook and Instagram Reels», 2023-02-21 — https://engineering.fb.com/2023/02/21/video-engineering/av1-codec-facebook-instagram-reels/
- [S10] Fora Soft, «8-bit vs 10-bit encoding», 2026 (вторичный, ссылается на HEVC-исследование 5,8–12,3 %) — https://www.forasoft.com/learn/video-encoding/articles/8bit-vs-10bit-encoding
- [S11] Обсуждения 10-bit из 8-bit источника: VideoHelp — https://forum.videohelp.com/showthread.php?p=2378313 ; ffmpeg-user — https://ffmpeg.org/pipermail/ffmpeg-user/2015-August/027869.html
- [S12] A. Katsenou и др. (Univ. of Bristol), «Comparing VVC, HEVC and AV1 using objective and subjective assessments» — https://research-information.bris.ac.uk/en/publications/comparing-vvc-hevc-and-av1-using-objective-and-subjective-assessm/
- [S13] Netflix AV1 (декабрь 2025), пересказ: https://www.streamingmediablog.com/2025/12/netflix-av1.html , https://www.tvbeurope.com/media-management/netflix-av1-powers-30-per-cent-of-all-viewing
- [S14] 3GPP TR 26.955 §8.2.6 (сводка верификационных тестов JVET VVC) — https://itecspec.com/3gpp/26.955/s/8.2.6
- [S15] «Video Quality Assessment and Coding Complexity of the Versatile Video Coding Standard», arXiv:2310.13093 — https://arxiv.org/pdf/2310.13093
- [S16] MSU, «Subjective Comparison of Modern Video Codecs», январь 2006 (DivX 6, XviD 1.1, x264, WMV9; 690/1024 кбит/с) — https://compression.ru/video/codec_comparison/pdf/msu_subjective_codecs_comparison_en.pdf
- [S17] ffmpeg-devel, 2006 (упоминание исследования Tandberg H.264 vs WMV9) — https://ffmpeg.org/pipermail/ffmpeg-devel/2006-April/006211.html ; https://en.wikipedia.org/wiki/Comparison_of_H.264_and_VC-1 (количественных данных нет)
- [S18] T. Wiegand и др., «Rate-constrained coder control and comparison of video coding standards», IEEE TCSVT 2003 — https://iphome.hhi.de/wiegand/assets/pdfs/csvt_ratecontrol_0305.pdf
- [S19] ATEME, NAB 2009 (10-bit 4:2:2 H.264 vs MPEG-2) — https://www.ateme.com/press/ateme-launches-worlds-first-10-bit-422/
- [S20] iMore, «Many '4K' iTunes movies are upscaled from lower resolutions», 2017 — https://www.imore.com/many-4k-itunes-movies-upscaled-lower-resolutions
- [S21] Netflix Partner Help Center, Cameras and Image Capture — https://backlothelp.netflix.com/hc/en-us/articles/360000579527-Cameras-and-Image-Capture
- [S22] resdet — https://www.github.com/0x09/resdet
- [S23] MSU SRDM — https://github.com/msu-video-group/SRDM
- Real or Fake 4K: упоминание в Sound & Vision — https://www.soundandvision.com/content/are-4k-ultra-hd-blu-rays-really-4k ; история домена — Wayback CDX `realorfake4k.com` (проверено 2026-10-23)
- blu-ray.com, пример поля «Upscaled 4K»: https://www.blu-ray.com/movies/Maleficent-4K-Blu-ray/247739/ ; обзоры с цитатами про DI: https://www.blu-ray.com/movies/Kingdom-of-the-Planet-of-the-Apes-4K-Blu-ray/348790/ , https://www.blu-ray.com/movies/The-Brutalist-4K-Blu-ray/379717/

---

## Открытые вопросы владельцу

1. Устраивают ли коэффициенты по разрешению (две колонки) или нужен один коэффициент на кодек?
2. Снижать ли коэффициент для аппаратных энкодеров (NVENC и т.п.), если их удаётся распознать по MediaInfo? (Сейчас — нет.)
3. Строка «Без апскейла»: если 1080p BDRemux ещё не вышел — показывать лучший 1080p WEB-DL или ничего?
4. Любительские AI-апскейлы (rutracker, «4K AI Upscale»): исключать из выбора «лучшей» раздачи совсем или только помечать?
5. `is_upscale = unknown` (мастер неизвестен) — как показывать в таблице («4K?») и добавлять ли строку 1080p на всякий случай?
6. Нужен ли blu-ray.com как второй источник (HTTP-доступ работает; ~2 запроса на фильм с UHD-диском)? Решение по CLAUDE.md
   правилу 4 — это новый тип метаданных.

## Что осталось непроверенным (нужен браузер / отдельный прогон)

- Заполненность IMDb DI у средних и малых фильмов 2024–2026 (проверены только 60 самых популярных + 2 малых). Метод для
  другого процесса: выборка из `imdb_basics`+`imdb_ratings` (movie, 2024–2026, без Documentary) по слоям голосов
  (≥50k, 5k–50k, 500–5k), `fetch('/title/{tt}/technical/')` из вкладки IMDb, разбор `#__NEXT_DATA__` как в 2.2.
- Правила раздела rutracker «UHD Video» (f=718, 1457) насчёт апскейлов и обязательных пометок.
- Поиск blu-ray.com не нашёл 16 из 56 тестовых фильмов по точному названию (Deadpool & Wolverine, Wicked, Sinners и др.) —
  нужен поиск по IMDb ID / более гибкое сопоставление.
- Доля EL в битрейте DV Profile 7 FEL по MediaInfo не определяется.
