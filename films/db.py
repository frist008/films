"""SQLite schema and connection helpers.

Schema changes go through MIGRATIONS (append-only); PRAGMA user_version tracks the applied count.
JSON columns are TEXT holding JSON arrays/objects.
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "films.db"

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE titles (
        id               INTEGER PRIMARY KEY,
        kind             TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
        is_animation     INTEGER NOT NULL DEFAULT 0,
        tracker_category TEXT,                 -- foreign_films / foreign_series / animation
        title_original   TEXT,
        title_latin      TEXT,                 -- transliterated/English title when original is non-Latin
        title_ru         TEXT,
        title_uk         TEXT,
        year             INTEGER,
        release_date     TEXT,                 -- world premiere, ISO date
        countries        TEXT,                 -- JSON array
        genres           TEXT,                 -- JSON array, ordered as the source lists them
        main_genre       TEXT,                 -- first genre
        runtime_min      INTEGER,
        studios          TEXT,                 -- JSON array
        budget_usd       INTEGER,
        box_office_usd   INTEGER,              -- worldwide gross; shown together with budget in one column
        seasons_total    INTEGER,              -- series only
        episodes_total   INTEGER,
        series_status    TEXT,                 -- returning / ended / canceled / in_production
        imdb_id          TEXT UNIQUE,
        tmdb_id          INTEGER,
        tmdb_type        TEXT,                 -- movie / tv
        rt_url           TEXT,
        imdb_rating      REAL,
        imdb_votes       INTEGER,
        rt_critics       INTEGER,              -- Tomatometer %
        rt_audience      INTEGER,              -- Popcornmeter %
        ratings_checked_at TEXT,
        rt_checked_at    TEXT,                 -- set even when RT has no page, with rt_url NULL
        trailer_ru_url   TEXT,
        trailer_checked_at TEXT,
        poster_url       TEXT,                 -- from IMDb or RT, shown as a "Картинка" hyperlink
        di_resolution    TEXT,                 -- digital intermediate / master format (2K / 4K) — upscale detection
        is_imax          INTEGER,              -- NULL = not checked yet
        imax_source      TEXT,
        no_dub_needed    INTEGER NOT NULL DEFAULT 0,  -- manual: no dialogue / voiceover not needed
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX ix_titles_year ON titles(year);

    CREATE TABLE people (
        id          INTEGER PRIMARY KEY,
        imdb_id     TEXT UNIQUE,
        tmdb_id     INTEGER UNIQUE,
        name        TEXT NOT NULL,
        name_ru     TEXT
    );

    CREATE TABLE credits (
        title_id    INTEGER NOT NULL REFERENCES titles(id),
        person_id   INTEGER NOT NULL REFERENCES people(id),
        role        TEXT NOT NULL,             -- actor / director / writer / producer / composer / dop ...
        character   TEXT,
        ord         INTEGER,
        PRIMARY KEY (title_id, person_id, role)
    );

    CREATE TABLE releases (
        id            INTEGER PRIMARY KEY,
        title_id      INTEGER REFERENCES titles(id),   -- NULL until matched
        source        TEXT NOT NULL CHECK (source IN ('rutor', 'rutracker')),
        source_id     TEXT NOT NULL,
        url           TEXT NOT NULL,
        btih          TEXT,
        name          TEXT NOT NULL,                   -- raw release title
        category      TEXT,
        added_at      TEXT,                            -- date on the tracker
        size_bytes    INTEGER,
        seeders       INTEGER,
        leechers      INTEGER,
        season        TEXT,                            -- series: "1", "1-3"
        episodes      TEXT,
        resolution    INTEGER,                         -- 2160 / 1080 / 720 / 480
        source_type   TEXT,                            -- BDRemux / BDRip / WEB-DL / WEBRip / HDTV / DVD / CAMRip / TS ...
        video_codec   TEXT,                            -- AVC / HEVC / AV1 / VP9 / MPEG-2 ...
        video_bit_depth INTEGER,
        video_kbps    INTEGER,                         -- video stream bitrate from MediaInfo
        h264_equiv_kbps INTEGER,                       -- video_kbps × codec efficiency factor (config [codec_efficiency])
        hdr           TEXT,                            -- JSON array: DV, HDR10+, HDR10, HLG
        dv_profile    INTEGER,
        is_imax_cut   INTEGER,                         -- release marked IMAX / IMAX Enhanced
        edition       TEXT,                            -- JSON array: open_matte / extended / directors_cut / theatrical ...
        is_upscale    INTEGER,                         -- 4K made by upscaling a 2K master (NULL = unknown)
        audio         TEXT,                            -- JSON array of {lang, kind, studio, codec, channels}
        best_voice    TEXT,                            -- dub / mvo / dvo / avo / amateur / none
        forced_subs   TEXT,                            -- JSON array of languages with forced subtitles (only forced are kept)
        details_parsed_at TEXT,
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (source, source_id)
    );
    CREATE INDEX ix_releases_title ON releases(title_id);

    CREATE TABLE release_raw (
        release_id  INTEGER PRIMARY KEY REFERENCES releases(id),
        fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
        body        BLOB NOT NULL                    -- zlib-compressed UTF-8 text of the release page
    );

    CREATE TABLE plex_items (
        rating_key   TEXT PRIMARY KEY,
        title_id     INTEGER REFERENCES titles(id),
        library      TEXT NOT NULL,
        kind         TEXT,
        plex_title   TEXT,
        plex_year    INTEGER,
        guids        TEXT,                           -- JSON array: imdb://, tmdb://, tvdb://
        file_path    TEXT,
        resolution   INTEGER,
        source_type  TEXT,
        hdr          TEXT,
        dv_profile   INTEGER,
        audio        TEXT,
        view_count   INTEGER,
        last_viewed_at TEXT,
        synced_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE watched (
        title_id    INTEGER NOT NULL REFERENCES titles(id),
        source      TEXT NOT NULL,                   -- plex_library / plex_history / manual
        watched_at  TEXT,
        PRIMARY KEY (title_id, source)
    );

    CREATE TABLE user_marks (
        title_id    INTEGER PRIMARY KEY REFERENCES titles(id),
        mark        TEXT CHECK (mark IN ('want', 'maybe', 'neutral', 'order')),
        ordered_by  TEXT,
        note        TEXT,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE responsibility (
        title_id    INTEGER PRIMARY KEY REFERENCES titles(id),
        person      TEXT NOT NULL,                   -- e.g. Дима
        source      TEXT NOT NULL,                   -- auto / auto_suggested / manual
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE store_status (
        title_id     INTEGER NOT NULL REFERENCES titles(id),
        store        TEXT NOT NULL,                  -- google_play / youtube
        owned        INTEGER,
        in_wishlist  INTEGER,
        available    INTEGER,                        -- 0 = not sold in the store
        price        REAL,
        currency     TEXT,
        discount_pct INTEGER,
        url          TEXT,
        checked_at   TEXT,
        PRIMARY KEY (title_id, store)
    );

    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,
        started_at  TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT,
        summary     TEXT
    );

    CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        run_id      INTEGER REFERENCES runs(id),
        title_id    INTEGER REFERENCES titles(id),
        kind        TEXT NOT NULL,                   -- new_title / better_release / new_dub / upgrade_available / watched ...
        details     TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        reported_at TEXT
    );

    CREATE TABLE match_issues (
        id          INTEGER PRIMARY KEY,
        release_id  INTEGER REFERENCES releases(id),
        plex_key    TEXT,
        problem     TEXT NOT NULL,
        candidates  TEXT,                            -- JSON
        resolved_at TEXT
    );
    """,
    # 2: raw source tables filled before titles exist (matched to titles later)
    """
    CREATE TABLE plex_history (
        id          INTEGER PRIMARY KEY,
        source      TEXT NOT NULL,                   -- library / plextv
        plex_guid   TEXT,                            -- plex://movie/... or local ratingKey guid
        guids       TEXT,                            -- JSON array: imdb://, tmdb://, tvdb://
        kind        TEXT,                            -- movie / show / season / episode
        title       TEXT,
        year        INTEGER,
        grandparent_title TEXT,                      -- show title for episodes
        viewed_at   TEXT,
        title_id    INTEGER REFERENCES titles(id),
        UNIQUE (source, plex_guid, viewed_at)
    );

    CREATE TABLE imdb_basics (
        tconst      TEXT PRIMARY KEY,
        title_type  TEXT,
        primary_title TEXT,
        original_title TEXT,
        start_year  INTEGER,
        end_year    INTEGER,
        runtime_min INTEGER,
        genres      TEXT
    );
    CREATE INDEX ix_imdb_basics_year ON imdb_basics(start_year);

    CREATE TABLE imdb_ratings (
        tconst      TEXT PRIMARY KEY,
        rating      REAL,
        votes       INTEGER,
        loaded_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE imdb_akas (
        tconst      TEXT NOT NULL,
        ordering    INTEGER NOT NULL,
        title       TEXT,
        region      TEXT,
        language    TEXT,
        PRIMARY KEY (tconst, ordering)
    );
    CREATE INDEX ix_imdb_akas_title ON imdb_akas(title);

    CREATE TABLE store_items (
        store        TEXT NOT NULL,                  -- google_play / youtube
        item_id      TEXT NOT NULL,                  -- store's own id
        title        TEXT,
        year         INTEGER,
        url          TEXT,
        owned        INTEGER,
        in_wishlist  INTEGER,
        available    INTEGER,
        price        REAL,
        currency     TEXT,
        discount_pct INTEGER,
        raw          TEXT,                           -- JSON of whatever else the page gave
        checked_at   TEXT NOT NULL DEFAULT (datetime('now')),
        title_id     INTEGER REFERENCES titles(id),
        PRIMARY KEY (store, item_id)
    );
    """,
    # 3: a series season is its own title (owner, 2026-10-23); 'not_interested' mark; columns proposed
    #    by the source modules (docs/plex-api.md, docs/ratings.md §4, rutor parser report).
    #    titles and user_marks were empty when this was written, so they are rebuilt, not copied.
    """
    PRAGMA foreign_keys = OFF;

    DROP TABLE titles;
    CREATE TABLE titles (
        id               INTEGER PRIMARY KEY,
        kind             TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
        season_number    INTEGER,              -- series: this row is one season (NULL for movies)
        series_key       TEXT,                 -- groups seasons of one series: 'imdb:tt…' or 'tmdb:…'
        series_imdb_id   TEXT,
        series_tmdb_id   INTEGER,
        is_animation     INTEGER NOT NULL DEFAULT 0,
        tracker_category TEXT,                 -- foreign_films / foreign_series / animation
        title_original   TEXT,
        title_latin      TEXT,
        title_ru         TEXT,
        title_uk         TEXT,
        year             INTEGER,              -- movie: release year; season: year the season aired
        release_date     TEXT,                 -- movie premiere / season first air date
        countries        TEXT,                 -- JSON array
        genres           TEXT,                 -- JSON array, ordered as the source lists them
        main_genre       TEXT,
        runtime_min      INTEGER,              -- movie runtime / typical episode runtime
        studios          TEXT,                 -- JSON array
        budget_usd       INTEGER,
        box_office_usd   INTEGER,              -- worldwide gross; shown together with budget in one column
        seasons_total    INTEGER,              -- series: seasons announced/aired so far
        episodes_total   INTEGER,              -- season: episodes in this season
        series_status    TEXT,                 -- returning / ended / canceled / in_production
        imdb_id          TEXT,                 -- movie tconst; for a season the series tconst is in series_imdb_id
        tmdb_id          INTEGER,
        tmdb_type        TEXT,                 -- movie / tv
        rt_url           TEXT,                 -- season page for seasons
        imdb_rating      REAL,
        imdb_votes       INTEGER,
        imdb_season_rating REAL,               -- season: mean of its episodes' IMDb ratings
        imdb_season_votes  INTEGER,
        rt_critics       INTEGER,              -- Tomatometer %
        rt_audience      INTEGER,              -- Popcornmeter %
        rt_critics_reviews  INTEGER,
        rt_audience_ratings INTEGER,
        rt_audience_type TEXT,                 -- VERIFIED / ALL
        ratings_checked_at TEXT,
        rt_checked_at    TEXT,                 -- set even when RT has no page, with rt_url NULL
        trailer_ru_url   TEXT,
        trailer_checked_at TEXT,
        poster_url       TEXT,                 -- IMDb image (owner wants a "Картинка" hyperlink)
        poster_source    TEXT,                 -- imdb / rt
        poster_checked_at TEXT,
        di_resolution    TEXT,                 -- digital intermediate / master format (2K / 4K) — upscale detection
        upscale_verdict  TEXT,                 -- native / upscaled / unknown, with upscale_source
        upscale_source   TEXT,                 -- imdb_di / bluray_com / ...
        aspect_ratios    TEXT,                 -- JSON, IMDb tech specs (DB only, owner 2026-10-23)
        sound_mix        TEXT,                 -- JSON, IMDb tech specs (DB only)
        is_imax          INTEGER,              -- NULL = not checked yet
        imax_source      TEXT,
        no_dub_needed    INTEGER NOT NULL DEFAULT 0,  -- manual: no dialogue / voiceover not needed
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX ix_titles_year ON titles(year);
    CREATE UNIQUE INDEX ux_titles_movie_imdb ON titles(imdb_id) WHERE imdb_id IS NOT NULL AND season_number IS NULL;
    CREATE UNIQUE INDEX ux_titles_season ON titles(series_key, season_number) WHERE season_number IS NOT NULL;

    DROP TABLE user_marks;
    CREATE TABLE user_marks (
        title_id    INTEGER PRIMARY KEY REFERENCES titles(id),
        mark        TEXT CHECK (mark IN ('want', 'maybe', 'neutral', 'order', 'not_interested')),
        ordered_by  TEXT,
        note        TEXT,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- a multi-season release belongs to every season it contains
    CREATE TABLE release_titles (
        release_id  INTEGER NOT NULL REFERENCES releases(id),
        title_id    INTEGER NOT NULL REFERENCES titles(id),
        how         TEXT,                            -- imdb_link / title_year / manual ...
        PRIMARY KEY (release_id, title_id)
    );
    CREATE INDEX ix_release_titles_title ON release_titles(title_id);

    ALTER TABLE releases ADD COLUMN source_detail TEXT;      -- "UHD BDRemux", "WEB-DLRip-AVC"
    ALTER TABLE releases ADD COLUMN dv_profile_full TEXT;    -- "8.1"
    ALTER TABLE releases ADD COLUMN dv_fallback TEXT;        -- HDR10 / SDR / HLG / none / unknown
    ALTER TABLE releases ADD COLUMN dv_static_md INTEGER;    -- DV rebuilt from HDR10 with static metadata
    ALTER TABLE releases ADD COLUMN is_ai_upscale INTEGER;   -- fan AI upscale
    ALTER TABLE releases ADD COLUMN parsed TEXT;             -- full parser output (JSON), re-derivable from release_raw
    ALTER TABLE releases ADD COLUMN parsed_version TEXT;

    ALTER TABLE plex_items ADD COLUMN plex_guid TEXT;
    ALTER TABLE plex_items ADD COLUMN library_id INTEGER;
    ALTER TABLE plex_items ADD COLUMN video_codec TEXT;
    ALTER TABLE plex_items ADD COLUMN video_profile TEXT;
    ALTER TABLE plex_items ADD COLUMN video_bit_depth INTEGER;
    ALTER TABLE plex_items ADD COLUMN video_kbps INTEGER;
    ALTER TABLE plex_items ADD COLUMN h264_equiv_kbps INTEGER;
    ALTER TABLE plex_items ADD COLUMN width INTEGER;
    ALTER TABLE plex_items ADD COLUMN height INTEGER;
    ALTER TABLE plex_items ADD COLUMN container TEXT;
    ALTER TABLE plex_items ADD COLUMN size_bytes INTEGER;
    ALTER TABLE plex_items ADD COLUMN duration_min INTEGER;
    ALTER TABLE plex_items ADD COLUMN forced_subs TEXT;
    ALTER TABLE plex_items ADD COLUMN files TEXT;
    ALTER TABLE plex_items ADD COLUMN season INTEGER;
    ALTER TABLE plex_items ADD COLUMN episode INTEGER;
    ALTER TABLE plex_items ADD COLUMN show_title TEXT;
    ALTER TABLE plex_items ADD COLUMN original_title TEXT;
    ALTER TABLE plex_items ADD COLUMN edition TEXT;
    ALTER TABLE plex_items ADD COLUMN present INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE plex_items ADD COLUMN removed_at TEXT;
    CREATE INDEX ix_plex_items_guid ON plex_items(plex_guid);

    ALTER TABLE plex_history ADD COLUMN history_id INTEGER;
    ALTER TABLE plex_history ADD COLUMN view_type TEXT;      -- played / marked / state
    ALTER TABLE plex_history ADD COLUMN library_section_id INTEGER;
    ALTER TABLE plex_history ADD COLUMN device_id INTEGER;
    ALTER TABLE plex_history ADD COLUMN season INTEGER;
    ALTER TABLE plex_history ADD COLUMN episode INTEGER;

    ALTER TABLE imdb_akas ADD COLUMN types TEXT;
    ALTER TABLE imdb_akas ADD COLUMN attributes TEXT;
    ALTER TABLE imdb_akas ADD COLUMN is_original INTEGER;
    ALTER TABLE imdb_basics ADD COLUMN is_adult INTEGER;
    CREATE TABLE imdb_title_norm (
        norm    TEXT NOT NULL,
        tconst  TEXT NOT NULL,
        source  TEXT,
        PRIMARY KEY (norm, tconst, source)
    );

    CREATE TABLE rt_pages (
        url            TEXT PRIMARY KEY,
        kind           TEXT,                       -- movie / series / season
        parent_url     TEXT,
        season         INTEGER,
        title          TEXT,
        year           INTEGER,                    -- RT year (US release)
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

    PRAGMA foreign_keys = ON;
    """,
    # 4: releases the owner never wants stored (DV profile 5 without a fallback layer); kept as ids
    #    so collectors skip them instead of re-adding them on every walk
    """
    CREATE TABLE excluded_releases (
        source      TEXT NOT NULL,
        source_id   TEXT NOT NULL,
        reason      TEXT NOT NULL,                   -- dv5_only
        name        TEXT,
        excluded_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source, source_id)
    );
    """,
    # 5: raw TMDB responses (so nothing has to be re-fetched to extract a new field later)
    """
    CREATE TABLE tmdb_raw (
        kind        TEXT NOT NULL,                   -- movie / tv / season / collection / person / find / search
        key         TEXT NOT NULL,                   -- tmdb id, "tv_id/season", imdb id, or query string
        lang        TEXT NOT NULL,                   -- ru-RU / uk-UA / en-US / '' for language-neutral
        fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
        status      INTEGER NOT NULL,                -- HTTP status (404 kept so misses are not retried blindly)
        body        BLOB,                            -- zlib-compressed JSON
        PRIMARY KEY (kind, key, lang)
    );
    """,
    # 6: TMDB enrichment (docs/tmdb.md); collections back the franchise feature (R34)
    """
    ALTER TABLE titles ADD COLUMN country_codes TEXT;        -- JSON ISO codes
    ALTER TABLE titles ADD COLUMN release_date_kind TEXT;    -- premiere / theatrical:US / digital:US ...
    ALTER TABLE titles ADD COLUMN trailer_ru_source TEXT;    -- tmdb_movie / tmdb_season / tmdb_series_named / youtube_search ...
    ALTER TABLE titles ADD COLUMN trailer_ru_subtitled INTEGER;
    ALTER TABLE titles ADD COLUMN collection_id INTEGER;     -- TMDB collection
    ALTER TABLE titles ADD COLUMN tmdb_checked_at TEXT;

    CREATE TABLE collections (
        id            INTEGER PRIMARY KEY,                  -- TMDB collection id
        name_ru       TEXT,
        name_original TEXT,
        parts         TEXT,                                 -- JSON [{tmdb_id, title_ru, title_original, year}]
        fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );

    ALTER TABLE credits ADD COLUMN job TEXT;                 -- Screenplay / Novel / Co-Director ...
    ALTER TABLE credits ADD COLUMN episodes INTEGER;         -- season credits: episodes worked on
    """,
    # 7: franchise universes (R34) and the owner's games (R36)
    """
    CREATE TABLE franchise_members (
        franchise_key TEXT NOT NULL,                  -- config/franchises.toml key
        imdb_id       TEXT NOT NULL,
        tmdb_id       INTEGER,
        tmdb_type     TEXT,                           -- movie / tv
        kind          TEXT,                           -- film / series / animated film / animated series / special / short
        title         TEXT,
        title_ru      TEXT,
        year          INTEGER,
        sources       TEXT,                           -- JSON: wikidata / wikipedia:en / wikipedia:ru / fandom:<wiki> / tmdb_collection
        confidence    TEXT,                           -- confirmed (2+ sources) / single_source / review
        note          TEXT,
        checked_at    TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (franchise_key, imdb_id)
    );
    CREATE INDEX ix_franchise_members_imdb ON franchise_members(imdb_id);

    CREATE TABLE games (
        store         TEXT NOT NULL,                  -- playstation / steam
        game_id       TEXT NOT NULL,
        title         TEXT,
        platform      TEXT,
        raw           TEXT,                           -- JSON of whatever the source gave
        checked_at    TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (store, game_id)
    );

    CREATE TABLE game_adaptations (
        store         TEXT NOT NULL,
        game_id       TEXT NOT NULL,
        imdb_id       TEXT NOT NULL,                  -- film / series based on the game (or its franchise)
        title         TEXT,
        year          INTEGER,
        kind          TEXT,
        sources       TEXT,
        PRIMARY KEY (store, game_id, imdb_id)
    );
    """,
    # 8: per-source sync checkpoints — "обнови базу" fetches only what is newer than the cursor
    """
    CREATE TABLE sync_state (
        source       TEXT PRIMARY KEY,               -- rutor / plex_library / plex_history / plextv_history / stores / tmdb / rt / imdb_datasets / youtube_trailers ...
        last_run_at  TEXT,                           -- when the last successful sync finished
        cursor       TEXT,                           -- newest item already collected (date added, viewed_at, id ...)
        details      TEXT                            -- JSON: counts of the last run
    );
    """,
    # 9: Jellyfin — the second media server (files with IMDb/TMDB ids, the owner's watch marks)
    """
    CREATE TABLE jellyfin_items (
        item_id         TEXT PRIMARY KEY,            -- Jellyfin BaseItems.Id
        kind            TEXT NOT NULL,               -- movie / series / season / episode
        name            TEXT,
        original_title  TEXT,
        year            INTEGER,
        path            TEXT,                        -- file (movie/episode) or folder (series/season)
        imdb_id         TEXT,
        tmdb_id         TEXT,
        tvdb_id         TEXT,
        series_item_id  TEXT,
        season          INTEGER,
        episode         INTEGER,
        runtime_min     INTEGER,
        size_bytes      INTEGER,
        library         TEXT,                        -- top library name (who it is for)
        title_id        INTEGER REFERENCES titles(id),
        synced_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX ix_jellyfin_items_path ON jellyfin_items(path);

    CREATE TABLE jellyfin_played (
        key             TEXT PRIMARY KEY,            -- Jellyfin UserData.CustomDataKey (tt… / tt…SSSEEE for episodes)
        kind            TEXT NOT NULL,               -- movie / episode
        imdb_id         TEXT,                        -- movie IMDb id, or the series IMDb id for an episode
        season          INTEGER,
        episode         INTEGER,
        item_id         TEXT,                        -- still-present item, NULL when the file was removed
        played_at       TEXT,
        title_id        INTEGER REFERENCES titles(id)
    );
    """,
    # 10: qBittorrent — what the owner put on download (queued = shown green «В очереди», R47)
    """
    CREATE TABLE qbt_torrents (
        infohash        TEXT PRIMARY KEY,            -- v1 info-hash, lowercase hex (= releases.btih)
        name            TEXT,
        size_bytes      INTEGER,
        progress        REAL,                        -- 0..1 from the fastresume piece bitmap
        paused          INTEGER,
        added_at        TEXT,
        completed_at    TEXT,
        save_path       TEXT,
        tracker         TEXT,                        -- rutor / rutracker / other
        topic_url       TEXT,                        -- torrent 'comment' (tracker topic page)
        release_id      INTEGER REFERENCES releases(id),
        title_id        INTEGER REFERENCES titles(id),
        how             TEXT,                        -- btih / topic / name / tmdb / manual
        present         INTEGER NOT NULL DEFAULT 1,  -- still in the client (0 = removed since, kept for history)
        synced_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 11: how the Russian speech of the trailer was confirmed (owner 2026-10-23: only the original audio track,
    # YouTube's AUTOMATIC captions (asr — made from the voice) or our own speech recognition count; uploaded
    # captions prove nothing; a neural (AI) Russian voice-over does not count — then the original trailer is used)
    """
    ALTER TABLE titles ADD COLUMN trailer_ru_check TEXT;     -- audio_track / asr / speech / title_only / unconfirmed
    ALTER TABLE titles ADD COLUMN trailer_ru_checked_at TEXT;
    """,
    # 12: a listing row found by a broad name search (all years) that names none of the wanted titles: kept, but its
    # page is not downloaded (a short name like «Лу» or «Дрон» brings thousands of unrelated releases)
    """
    ALTER TABLE releases ADD COLUMN page_skip INTEGER NOT NULL DEFAULT 0;
    """,
    # 13: a trailer always exists (owner 2026-10-23): when there is no Russian-speech one — the Ukrainian voice-over,
    # else the original with Russian subtitles, else the original (TMDB / YouTube)
    """
    ALTER TABLE titles ADD COLUMN trailer_alt_url TEXT;
    ALTER TABLE titles ADD COLUMN trailer_alt_kind TEXT;      -- uk / ru_sub / original
    ALTER TABLE titles ADD COLUMN trailer_alt_source TEXT;
    """,
    # 14: crew jobs (creator / showrunner / executive producer / producer / story …) of the owner's important people
    # marked `crew = true` in config/people.toml (R75) — `credits` holds only actor / director / writer.
    # Filled by films.crew from the TMDB cache + one /person/{id}/combined_credits per person; seasons via the series.
    """
    CREATE TABLE person_crew (
        title_id        INTEGER NOT NULL REFERENCES titles(id),
        person_tmdb_id  INTEGER NOT NULL,
        job             TEXT NOT NULL,             -- Creator / Executive Producer / Producer / Showrunner / Story / Writer / Director
        source          TEXT NOT NULL,             -- movie_credits / season_credits / created_by / combined_credits
        checked_at      TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (title_id, person_tmdb_id, job)
    );
    CREATE INDEX person_crew_person ON person_crew (person_tmdb_id);
    """,
    # 15: every video file on the local drives (R85, owner 2026-10-23: «проверить каждый файл») — downloaded =
    # not watched yet, whether or not Plex / Jellyfin know the file. Filled by films.disks (read-only on the disks);
    # a file gone since the last scan keeps its row with present = 0.
    """
    CREATE TABLE disk_files (
        path            TEXT PRIMARY KEY,            -- full Windows path as found on disk
        drive           TEXT NOT NULL,               -- C / D / E …
        size_bytes      INTEGER,
        mtime           TEXT,                        -- UTC
        ext             TEXT,                        -- lowercase, without the dot
        folder          TEXT,                        -- parent folder (full path)
        nfo_path        TEXT,                        -- the .nfo used (same-stem / movie.nfo / tvshow.nfo)
        nfo_tmdb_id     INTEGER,
        nfo_imdb_id     TEXT,
        parsed_title    TEXT,                        -- name used for the search (file or show folder)
        parsed_year     INTEGER,
        season          INTEGER,
        episode         INTEGER,
        title_id        INTEGER REFERENCES titles(id),
        extra_title_ids TEXT,                        -- JSON: more titles in the same file («A 3 + B 4.mkv»)
        how             TEXT,                        -- nfo / folder_tmdbid / plex / jellyfin / qbt / torrent / name_match /
                                                     -- manual / personal / not_film / season_clash / unmatched
        note            TEXT,                        -- why unmatched / which candidates / what was used
        scanned_at      TEXT NOT NULL DEFAULT (datetime('now')),
        present         INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX ix_disk_files_title ON disk_files(title_id);
    """,
]


def years() -> list[int]:
    """Collected years: config year_from … next year (open-ended — the DB is kept up to date for good)."""
    import tomllib
    from datetime import date
    with open(ROOT / "config" / "settings.toml", "rb") as f:
        start = tomllib.load(f)["year_from"]
    return list(range(start, date.today().year + 2))


def mark_sync(conn, source: str, cursor: str | None = None, details: dict | None = None) -> None:
    """Record a finished sync of `source` (keeps the previous cursor when none is given)."""
    import json
    with conn:
        conn.execute(
            """INSERT INTO sync_state (source, last_run_at, cursor, details) VALUES (?, datetime('now'), ?, ?)
               ON CONFLICT(source) DO UPDATE SET last_run_at = excluded.last_run_at,
                   cursor = COALESCE(excluded.cursor, sync_state.cursor), details = excluded.details""",
            (source, cursor, json.dumps(details, ensure_ascii=False, default=str) if details else None))


def sync_cursor(conn, source: str) -> str | None:
    row = conn.execute("SELECT cursor FROM sync_state WHERE source = ?", (source,)).fetchone()
    return row[0] if row else None


def private() -> dict:
    """Personal identifiers (accounts, private list ids) from config/private.toml — never committed.
    Documents refer to them as <section.key>; the values are also kept in the owner's Google Keep note."""
    import tomllib
    path = ROOT / "config" / "private.toml"
    if not path.exists():
        raise RuntimeError("config/private.toml is missing: copy config/private.example.toml and fill it "
                           "from the Google Keep note «films — секреты» (see docs/google-setup.md)")
    with open(path, "rb") as f:
        return tomllib.load(f)


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")  # several collectors write concurrently
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()
    return len(MIGRATIONS)
