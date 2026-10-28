CREATE TABLE collections (
        id            INTEGER PRIMARY KEY,                  -- TMDB collection id
        name_ru       TEXT,
        name_original TEXT,
        parts         TEXT,                                 -- JSON [{tmdb_id, title_ru, title_original, year}]
        fetched_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE TABLE credits (
        title_id    INTEGER NOT NULL REFERENCES titles(id),
        person_id   INTEGER NOT NULL REFERENCES people(id),
        role        TEXT NOT NULL,             -- actor / director / writer / producer / composer / dop ...
        character   TEXT,
        ord         INTEGER, job TEXT, episodes INTEGER,
        PRIMARY KEY (title_id, person_id, role)
    );
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
CREATE TABLE events (
        id          INTEGER PRIMARY KEY,
        run_id      INTEGER REFERENCES runs(id),
        title_id    INTEGER REFERENCES titles(id),
        kind        TEXT NOT NULL,                   -- new_title / better_release / new_dub / upgrade_available / watched ...
        details     TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        reported_at TEXT
    );
CREATE TABLE excluded_releases (
        source      TEXT NOT NULL,
        source_id   TEXT NOT NULL,
        reason      TEXT NOT NULL,                   -- dv5_only
        name        TEXT,
        excluded_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (source, source_id)
    );
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
CREATE TABLE games (
        store         TEXT NOT NULL,                  -- playstation / steam
        game_id       TEXT NOT NULL,
        title         TEXT,
        platform      TEXT,
        raw           TEXT,                           -- JSON of whatever the source gave
        checked_at    TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (store, game_id)
    );
CREATE TABLE imdb_akas (
        tconst      TEXT NOT NULL,
        ordering    INTEGER NOT NULL,
        title       TEXT,
        region      TEXT,
        language    TEXT, types TEXT, attributes TEXT, is_original INTEGER,
        PRIMARY KEY (tconst, ordering)
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
    , is_adult INTEGER);
CREATE TABLE imdb_ratings (
        tconst      TEXT PRIMARY KEY,
        rating      REAL,
        votes       INTEGER,
        loaded_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE TABLE imdb_title_norm (
        norm    TEXT NOT NULL,
        tconst  TEXT NOT NULL,
        source  TEXT,
        PRIMARY KEY (norm, tconst, source)
    );
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
CREATE TABLE match_issues (
        id          INTEGER PRIMARY KEY,
        release_id  INTEGER REFERENCES releases(id),
        plex_key    TEXT,
        problem     TEXT NOT NULL,
        candidates  TEXT,                            -- JSON
        resolved_at TEXT
    );
CREATE TABLE people (
        id          INTEGER PRIMARY KEY,
        imdb_id     TEXT UNIQUE,
        tmdb_id     INTEGER UNIQUE,
        name        TEXT NOT NULL,
        name_ru     TEXT
    , name_uk TEXT, name_ru_source TEXT, name_uk_source TEXT, names_checked_at TEXT);
CREATE TABLE person_crew (
        title_id        INTEGER NOT NULL REFERENCES titles(id),
        person_tmdb_id  INTEGER NOT NULL,
        job             TEXT NOT NULL,             -- Creator / Executive Producer / Producer / Showrunner / Story / Writer / Director
        source          TEXT NOT NULL,             -- movie_credits / season_credits / created_by / combined_credits
        checked_at      TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (title_id, person_tmdb_id, job)
    );
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
        title_id    INTEGER REFERENCES titles(id), history_id INTEGER, view_type TEXT, library_section_id INTEGER, device_id INTEGER, season INTEGER, episode INTEGER,
        UNIQUE (source, plex_guid, viewed_at)
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
    , plex_guid TEXT, library_id INTEGER, video_codec TEXT, video_profile TEXT, video_bit_depth INTEGER, video_kbps INTEGER, h264_equiv_kbps INTEGER, width INTEGER, height INTEGER, container TEXT, size_bytes INTEGER, duration_min INTEGER, forced_subs TEXT, files TEXT, season INTEGER, episode INTEGER, show_title TEXT, original_title TEXT, edition TEXT, present INTEGER NOT NULL DEFAULT 1, removed_at TEXT, added_at TEXT);
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
CREATE TABLE release_raw (
        release_id  INTEGER PRIMARY KEY REFERENCES releases(id),
        fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
        body        BLOB NOT NULL                    -- zlib-compressed UTF-8 text of the release page
    );
CREATE TABLE release_titles (
        release_id  INTEGER NOT NULL REFERENCES releases(id),
        title_id    INTEGER NOT NULL REFERENCES titles(id),
        how         TEXT,                            -- imdb_link / title_year / manual ...
        PRIMARY KEY (release_id, title_id)
    );
CREATE TABLE releases (
        id            INTEGER PRIMARY KEY,
        title_id      INTEGER REFERENCES titles(id),   -- NULL until matched
        source        TEXT NOT NULL CHECK (source IN ('rutor', 'rutracker', 'toloka', 'mazepa', 'rustorka', 'nnmclub', 'kinozal')),
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
        last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')), source_detail TEXT, dv_profile_full TEXT, dv_fallback TEXT, dv_static_md INTEGER, is_ai_upscale INTEGER, parsed TEXT, parsed_version TEXT, page_skip INTEGER NOT NULL DEFAULT 0, has_ads INTEGER, localized_video INTEGER, is_3d INTEGER, cam_source TEXT, remaster TEXT, dub_official INTEGER, video_color TEXT, fps REAL, is_kpk INTEGER, voice_18plus INTEGER,
        UNIQUE (source, source_id)
    );
CREATE TABLE responsibility (
        title_id    INTEGER PRIMARY KEY REFERENCES titles(id),
        person      TEXT NOT NULL,                   -- e.g. Дима
        source      TEXT NOT NULL,                   -- auto / auto_suggested / manual
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
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
CREATE TABLE runs (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,
        started_at  TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT,
        summary     TEXT
    );
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
        title_id     INTEGER REFERENCES titles(id), purchased_at TEXT, purchased_at_source TEXT, wishlist_added_at TEXT, wishlist_added_source TEXT,
        PRIMARY KEY (store, item_id)
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
CREATE TABLE sync_state (
        source       TEXT PRIMARY KEY,               -- rutor / plex_library / plex_history / plextv_history / stores / tmdb / rt / imdb_datasets / youtube_trailers ...
        last_run_at  TEXT,                           -- when the last successful sync finished
        cursor       TEXT,                           -- newest item already collected (date added, viewed_at, id ...)
        details      TEXT                            -- JSON: counts of the last run
    );
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
    , country_codes TEXT, release_date_kind TEXT, trailer_ru_source TEXT, trailer_ru_subtitled INTEGER, collection_id INTEGER, tmdb_checked_at TEXT, trailer_ru_check TEXT, trailer_ru_checked_at TEXT, trailer_alt_url TEXT, trailer_alt_kind TEXT, trailer_alt_source TEXT, distributor TEXT, distributor_country TEXT, distributors TEXT, keywords TEXT, is_miniseries INTEGER, cameras TEXT, film_format TEXT, color TEXT, filming_locations TEXT, original_language TEXT, spoken_languages TEXT, homepage TEXT, wikidata_id TEXT, wiki_links TEXT, meta_checked_at TEXT, duplicate_of INTEGER REFERENCES titles(id), imdb_id_wrong TEXT, tracker_only INTEGER NOT NULL DEFAULT 0, tracker_meta TEXT, identify_checked_at TEXT, poster_release_id INTEGER REFERENCES releases(id), poster_alive_at TEXT, title_uk_source TEXT, episode_dates TEXT);
CREATE TABLE tmdb_raw (
        kind        TEXT NOT NULL,                   -- movie / tv / season / collection / person / find / search
        key         TEXT NOT NULL,                   -- tmdb id, "tv_id/season", imdb id, or query string
        lang        TEXT NOT NULL,                   -- ru-RU / uk-UA / en-US / '' for language-neutral
        fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
        status      INTEGER NOT NULL,                -- HTTP status (404 kept so misses are not retried blindly)
        body        BLOB,                            -- zlib-compressed JSON
        PRIMARY KEY (kind, key, lang)
    );
CREATE TABLE user_marks (
        title_id    INTEGER PRIMARY KEY REFERENCES titles(id),
        mark        TEXT CHECK (mark IN ('want', 'maybe', 'neutral', 'order', 'not_interested')),
        ordered_by  TEXT,
        note        TEXT,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
CREATE TABLE watched (
        title_id    INTEGER NOT NULL REFERENCES titles(id),
        source      TEXT NOT NULL,                   -- plex_library / plex_history / manual
        watched_at  TEXT,
        PRIMARY KEY (title_id, source)
    );
CREATE INDEX ix_disk_files_title ON disk_files(title_id);
CREATE INDEX ix_franchise_members_imdb ON franchise_members(imdb_id);
CREATE INDEX ix_imdb_akas_title ON imdb_akas(title);
CREATE INDEX ix_imdb_basics_year ON imdb_basics(start_year);
CREATE INDEX ix_jellyfin_items_path ON jellyfin_items(path);
CREATE INDEX ix_plex_items_guid ON plex_items(plex_guid);
CREATE INDEX ix_release_titles_title ON release_titles(title_id);
CREATE INDEX ix_releases_title ON releases(title_id);
CREATE INDEX ix_titles_year ON titles(year);
CREATE INDEX person_crew_person ON person_crew (person_tmdb_id);
CREATE UNIQUE INDEX ux_titles_movie_imdb ON titles(imdb_id) WHERE imdb_id IS NOT NULL AND season_number IS NULL;
CREATE UNIQUE INDEX ux_titles_season ON titles(series_key, season_number) WHERE season_number IS NOT NULL;
