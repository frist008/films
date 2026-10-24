"""Is the trailer really spoken in Russian? For trailers whose YouTube metadata does not say it (no audio-track
language, no automatic captions), and on request for any title.

Usage:
    python -X utf8 -m films.sources.trailer_lang check --unconfirmed [--limit N] [--dry-run]
    python -X utf8 -m films.sources.trailer_lang check --ids 12,34
    python -X utf8 -m films.sources.trailer_lang video <youtube_id>          # one video, prints the verdict

Order of checks for one video (cheapest first, owner's instruction 2026-10-23):
1. Watch page (films.sources.youtube.YouTube.watch): audio-track language / automatic ('asr') captions = the
   spoken language. A manual Russian caption track on a video whose speech is not known is noted (a hint that
   it is the original trailer with Russian subtitles), never decisive by itself.
2. Speech: the audio stream is downloaded with yt-dlp into data/cache/trailer_audio/ (a temporary file, deleted
   right after — nothing is kept but the verdict), and faster-whisper (local model, nothing is sent anywhere)
   detects the language on the speech parts (VAD) and transcribes them.  'ru' with probability >= RU_MIN_PROB and
   Cyrillic in the transcript = confirmed Russian speech.
Verdicts are appended to data/cache/youtube/audio_checks.jsonl (video id, method, language, probability, a short
transcript sample) and reused.

For a title whose trailer turns out not to be in Russian: the other candidates of the same title (runners-up of
the trailer search, plus one extra «дубляж» query) are checked the same way; the first confirmed Russian one
replaces it.  If none is confirmed the link is removed from titles.trailer_ru_url (only links this project found
on YouTube or TMDB) and the finding stays in the log. The confirmation method is written to titles.trailer_ru_check
(audio_track / asr / speech; neural / other / unconfirmed) — uploaded (manual) captions never confirm anything.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from films import db
from films.sources import youtube as yt_mod

AUDIO_DIR = db.ROOT / "data" / "cache" / "trailer_audio"
CHECKS = yt_mod.CACHE_DIR / "audio_checks.jsonl"
MODEL_SIZE = "small"        # multilingual; enough for language id + a rough transcript of a 2-minute trailer
RU_MIN_PROB = 0.7
_CYR = re.compile(r"[а-яё]{3,}", re.I)
# neural / AI Russian voice-over does not count as a Russian trailer (owner 2026-10-23) — the original one is better
_NEURAL = re.compile(r"нейро|нейросет|\bai\b|\bии\b[- ]?(?:озвуч|дубляж|перевод)|ai[- ]?(?:dub|voice)|синтез", re.I)


def check_method(rec: dict) -> str:
    """titles.trailer_ru_check from a verdict: audio_track / asr / speech / neural / other / unconfirmed."""
    if rec.get("verdict") == "neural":
        return "neural"
    if rec.get("verdict") != "ru":
        return rec.get("verdict") or "unconfirmed"
    m = rec.get("method") or ""
    return "speech" if m.startswith("speech") else "audio_track" if "audio_track" in m else "asr" if "asr" in m else "unconfirmed"
_UK_ONLY = re.compile(r"[ієїґ]", re.I)

_model = None


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        # CPU: the GPU loads but fails at inference without the CUDA 12 runtime (cublas64_12.dll); a 2-minute
        # trailer takes seconds on CPU anyway
        _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


def _load_checks() -> dict[str, dict]:
    out = {}
    if CHECKS.exists():
        for line in CHECKS.open(encoding="utf-8"):
            try:
                r = json.loads(line)
                out[r["id"]] = r
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def _save_check(rec: dict):
    CHECKS.parent.mkdir(parents=True, exist_ok=True)
    with CHECKS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _download_audio(video_id: str) -> Path | None:
    import yt_dlp
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    opts = {"format": "bestaudio[filesize<30M]/bestaudio", "outtmpl": str(AUDIO_DIR / f"{video_id}.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "geo_bypass_country": "UA"}
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([f"https://www.youtube.com/watch?v={video_id}"])
    files = list(AUDIO_DIR.glob(f"{video_id}.*"))
    return files[0] if files else None


def _pcm16k(path: Path):
    """Decode with the system ffmpeg to 16 kHz mono float32 (faster-whisper's own PyAV decoder breaks on some
    PyAV versions: «open() got an unexpected keyword argument 'metadata_errors'»)."""
    import subprocess
    import numpy as np
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def speech_language(video_id: str) -> dict:
    """Download the audio, detect the spoken language, delete the file."""
    path = None
    try:
        path = _download_audio(video_id)
        if not path:
            return {"language": None, "error": "no audio downloaded"}
        segments, info = _whisper().transcribe(_pcm16k(path), vad_filter=True, beam_size=1,
                                               language_detection_segments=4, condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments)[:2000]
        return {"language": info.language, "probability": round(info.language_probability, 3),
                "speech_seconds": round(getattr(info, "duration_after_vad", 0) or 0, 1), "transcript": text[:300],
                "cyrillic": bool(_CYR.search(text)), "uk_letters": bool(_UK_ONLY.search(text))}
    except Exception as e:  # noqa: BLE001 — a failed download is a verdict «unknown», the run goes on
        return {"language": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        for f in AUDIO_DIR.glob(f"{video_id}.*"):  # also .part leftovers of an interrupted download
            f.unlink(missing_ok=True)


def check_video(yt: yt_mod.YouTube, video_id: str, cache: dict) -> dict:
    """{'id', 'verdict': 'ru' | 'uk' | 'other' | 'unknown', 'method', ...} — cached in audio_checks.jsonl."""
    if video_id in cache:
        return cache[video_id]
    rec = {"id": video_id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    w = yt.watch(video_id)
    al, how = yt_mod.audio_language(w.get("audio_tracks") or [], w.get("captions") or [])
    rec.update(watch_status=w.get("status"), watch_title=w.get("title"), ru_manual_captions=w.get("ru_manual_captions"))
    if _NEURAL.search(w.get("title") or ""):
        rec.update(verdict="neural", method="title")   # a neural voice-over is not a Russian trailer
    elif w.get("status") not in ("OK", "LOGIN_REQUIRED"):
        rec.update(verdict="unavailable", method="watch")
    elif al == "ru":
        rec.update(verdict="ru", method=f"watch:{how}", language=al)
    else:
        # unknown, or YouTube's metadata says another language — its metadata is sometimes wrong (a dubbed
        # trailer with 'en' asr was seen), so a «not Russian» is always confirmed by the speech itself
        s = speech_language(video_id)
        rec.update(s, method="speech" if not al else f"speech (watch:{how} said {al})")
        lang = s.get("language")
        if not lang and al:  # speech check failed: fall back to the metadata
            rec["verdict"] = "uk" if al == "uk" else "other"
        elif lang == "ru" and (s.get("probability") or 0) >= RU_MIN_PROB and s.get("cyrillic"):
            rec["verdict"] = "ru"
        elif lang == "uk" and (s.get("probability") or 0) >= RU_MIN_PROB:
            rec["verdict"] = "uk"
        elif lang and (s.get("probability") or 0) >= RU_MIN_PROB and (s.get("speech_seconds") or 0) >= 5:
            rec["verdict"] = "other"
        else:
            rec["verdict"] = "unknown"   # too little speech / low confidence / download failed
    if not rec.get("error"):        # a failed download / decode is retried next time, not remembered
        _save_check(rec)
        cache[video_id] = rec
    return rec


def unconfirmed_titles(conn) -> list[int]:
    """Titles with a Russian trailer (any source — YouTube search or TMDB) whose Russian speech is not confirmed
    by the original audio track, automatic captions or speech recognition (titles.trailer_ru_check)."""
    return [tid for (tid,) in conn.execute(
        """SELECT id FROM titles WHERE trailer_ru_url IS NOT NULL
           AND coalesce(trailer_ru_check, 'unconfirmed') NOT IN ('audio_track', 'asr', 'speech')""")]


def _alternatives(yt: yt_mod.YouTube, conn, tid: int, current: str) -> list[str]:
    d = yt_mod.latest_decisions().get(tid) or {}
    ids = [c["id"] for c in d.get("runners_up") or [] if c.get("lang") == "ru" and not c.get("reject")]
    row = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
    ctx = yt_mod.title_context(conn, row)
    name = row["title_ru"] or row["title_original"]
    q = f"{name} {row['year'] or ''} трейлер дубляж".strip()
    res = yt.search(q)
    for v in res.get("videos") or []:
        c = yt_mod.evaluate(v, ctx, res.get("fetched_at") or "")
        if c.get("ok") and c.get("lang") == "ru":
            ids.append(c["id"])
    return [i for i in dict.fromkeys(ids) if i != current]


def check_titles(ids: list[int], dry_run: bool = False, log=print) -> dict:
    conn = db.connect()
    yt = yt_mod.YouTube()
    cache = _load_checks()
    stats = {"titles": len(ids), "confirmed_ru": 0, "replaced": 0, "removed": 0, "unknown": 0, "details": []}
    for n, tid in enumerate(ids, 1):
        row = conn.execute("SELECT id, title_ru, year, trailer_ru_url FROM titles WHERE id = ?", (tid,)).fetchone()
        vid = (row["trailer_ru_url"] or "").split("v=")[-1]
        r = check_video(yt, vid, cache)
        det = {"title_id": tid, "title": row["title_ru"], "year": row["year"], "video": vid, "verdict": r["verdict"],
               "method": r.get("method"), "language": r.get("language"), "probability": r.get("probability")}
        if r["verdict"] == "ru":
            stats["confirmed_ru"] += 1
        elif r["verdict"] == "unknown":
            stats["unknown"] += 1      # keep the link; listed for the owner
        else:
            new = None
            for alt in _alternatives(yt, conn, tid, vid):
                if check_video(yt, alt, cache)["verdict"] == "ru":
                    new = alt
                    break
            det["replacement"] = new
            if not dry_run:
                with conn:
                    if new:
                        conn.execute("""UPDATE titles SET trailer_ru_url = ?, trailer_ru_source = 'youtube_search',
                                        trailer_checked_at = datetime('now') WHERE id = ?""",
                                     (f"https://www.youtube.com/watch?v={new}", tid))
                    else:
                        conn.execute("""UPDATE titles SET trailer_ru_url = NULL, trailer_checked_at = datetime('now')
                                        WHERE id = ?""", (tid,))   # not Russian speech (or neural): the finding stays in the log
            stats["replaced" if new else "removed"] += 1
        if not dry_run:
            final = check_video(yt, det.get("replacement"), cache) if det.get("replacement") else r
            with conn:
                conn.execute("UPDATE titles SET trailer_ru_check = ?, trailer_ru_checked_at = datetime('now') WHERE id = ?",
                             (check_method(final) if (det.get("replacement") or r["verdict"] in ("ru", "unknown")) else None,
                              tid))
        stats["details"].append(det)
        log(f"  {n}/{len(ids)} {row['title_ru']} ({row['year']}): {r['verdict']} via {r.get('method')}"
            f"{' → ' + str(det.get('replacement')) if 'replacement' in det else ''}")
    if not dry_run:
        db.mark_sync(conn, "trailer_lang", None, {k: v for k, v in stats.items() if k != "details"})
    shutil.rmtree(AUDIO_DIR, ignore_errors=True)  # no audio is kept
    return stats


def fill_fallbacks(conn) -> dict:
    """titles without a Russian-speech trailer get trailer_alt_* in the owner's order (2026-10-23):
    Ukrainian voice-over (YouTube search finding, then TMDB) → the original trailer (TMDB here, YouTube in
    search_originals) → the original with Russian subtitles (the search's 'subtitled' finding) → any trailer, best
    quality (search_originals, last resort)."""
    from . import tmdb
    latest = yt_mod.latest_decisions()
    stats = {"uk": 0, "ru_sub": 0, "original": 0, "none": 0}
    rows = conn.execute("""SELECT id, kind, tmdb_id, series_tmdb_id, season_number FROM titles
                           WHERE trailer_ru_url IS NULL""").fetchall()
    with conn:
        for tid, kind, tm, stm, season in rows:
            d = latest.get(tid) or {}
            url = kind_ = src = None
            uk = [c for c in d.get("uk") or [] if not c.get("reject")]
            sub = [c for c in d.get("subtitled") or [] if not c.get("reject")]
            body = tmdb.movie(conn, tm) if kind == "movie" and tm else tmdb.tv(conn, stm) if stm else None
            tmdb_uk = ((body or {}).get("trailers_uk") or [{}])[0].get("url")
            if uk:
                url, kind_, src = f"https://www.youtube.com/watch?v={uk[0]['id']}", "uk", "youtube_search"
            elif tmdb_uk:
                url, kind_, src = tmdb_uk, "uk", "tmdb"
            elif body and body.get("trailer_en_url"):
                url, kind_, src = body["trailer_en_url"], "original", "tmdb"
            elif sub:   # owner: Russian subtitles only after the original
                url, kind_, src = f"https://www.youtube.com/watch?v={sub[0]['id']}", "ru_sub", "youtube_search"
            conn.execute("UPDATE titles SET trailer_alt_url = ?, trailer_alt_kind = ?, trailer_alt_source = ? WHERE id = ?",
                         (url, kind_, src, tid))
            stats[kind_ or "none"] += 1
    db.mark_sync(conn, "trailer_fallbacks", None, stats)
    return stats


def search_originals(conn, log=print) -> dict:
    """Titles still without any trailer: the original trailer from YouTube search — «<original> <year> trailer»
    (series: «… season N trailer»); accepted when the video title contains every word of the original name and
    says trailer / teaser / трейлер, length 30 s – 5 min."""
    yt = yt_mod.YouTube()
    stats = {"titles": 0, "found": 0}
    rows = conn.execute("""SELECT id, title_original, title_latin, year, kind, season_number FROM titles t
                           WHERE trailer_ru_url IS NULL AND (trailer_alt_url IS NULL OR trailer_alt_kind = 'ru_sub')
                           AND EXISTS (SELECT 1 FROM release_titles r WHERE r.title_id = t.id)""").fetchall()
    for tid, orig, latin, year, kind, season in rows:
        name = latin or orig
        if not name:
            continue
        stats["titles"] += 1
        q = f"{name} {'season ' + str(season) + ' ' if kind == 'series' and season else ''}{year or ''} trailer"
        res = yt.search(q)
        words = [w for w in yt_mod.tokens(name) if len(w) > 1]
        pick = None
        for v in res.get("videos") or []:
            t = yt_mod.norm(v.get("title"))
            ln = yt_mod.parse_length(v.get("length"))
            if all(w in t for w in words) and re.search(r"trailer|teaser|трейлер|тизер", t) and ln and 30 <= ln <= 300:
                pick = v
                break
        kind_ = "original"
        if not pick and not conn.execute("SELECT trailer_alt_url FROM titles WHERE id = ?", (tid,)).fetchone()[0]:
            # nothing at all: any trailer of this title, preferring 4K / HD in the video title (owner: «в лучшем качестве»)
            hits = [v for v in res.get("videos") or [] if all(w in yt_mod.norm(v.get("title")) for w in words)
                    and 30 <= (yt_mod.parse_length(v.get("length")) or 0) <= 300]
            hits.sort(key=lambda v: (0 if re.search(r"4k|2160|uhd", v.get("title") or "", re.I) else
                                     1 if re.search(r"hd|1080", v.get("title") or "", re.I) else 2))
            pick, kind_ = (hits[0], "any") if hits else (None, None)
        if pick:
            with conn:
                conn.execute("""UPDATE titles SET trailer_alt_url = ?, trailer_alt_kind = ?,
                                trailer_alt_source = 'youtube_search' WHERE id = ?""",
                             (f"https://www.youtube.com/watch?v={pick['id']}", kind_, tid))
            stats["found"] += 1
        log(f"  {stats['titles']}/{len(rows)} {name} {year}: {'→ ' + pick['title'][:60] if pick else 'нет'}")
    db.mark_sync(conn, "trailer_originals", None, stats)
    return stats


def main(argv=None):
    p = argparse.ArgumentParser(prog="films.sources.trailer_lang")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--unconfirmed", action="store_true")
    c.add_argument("--ids")
    c.add_argument("--limit", type=int)
    c.add_argument("--dry-run", action="store_true")
    sub.add_parser("fallbacks")
    sub.add_parser("originals")
    v = sub.add_parser("video")
    v.add_argument("id")
    a = p.parse_args(argv)
    if a.cmd == "originals":
        print(json.dumps(search_originals(db.connect(), log=lambda m: print(m, file=sys.stderr, flush=True)),
                         ensure_ascii=False, indent=1))
        return
    if a.cmd == "fallbacks":
        print(json.dumps(fill_fallbacks(db.connect()), ensure_ascii=False, indent=1))
        return
    if a.cmd == "video":
        print(json.dumps(check_video(yt_mod.YouTube(), a.id, _load_checks()), ensure_ascii=False, indent=1))
        return
    conn = db.connect()
    ids = [int(x) for x in a.ids.split(",")] if a.ids else unconfirmed_titles(conn) if a.unconfirmed else []
    ids = ids[: a.limit] if a.limit else ids
    st = check_titles(ids, a.dry_run, log=lambda m: print(m, file=sys.stderr, flush=True))
    print(json.dumps(st, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
