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
3. Speech without VAD (when step 2 says «unknown»; agent_trailers3, 2026-10-25): on music-heavy trailers Silero VAD
   drops all speech (speech_seconds 0 → «en 0.368» = no data).  The audio is cut into 30-second windows (from 0 s,
   then from 15 s), each transcribed with automatic language detection; only confident windows count
   (probability >= RU_MIN_PROB, >= 2 real words).  Whisper's sound tags («НАПРЯЖЕННАЯ МУЗЫКА», «[MUSIC]»), repeated
   chants and its known filler phrases on music («Субтитры …», «Продолжение следует…») are not speech
   (speech_words / is_filler).  Russian = >= 3 Russian words in Cyrillic and at least twice the words of any other
   confidently detected language (window_verdict).  Still undecided → 8-second speech islands: the language of
   every piece, a confidently Russian piece transcribed in Russian (island_verdict).
Verdicts are appended to data/cache/youtube/audio_checks.jsonl (video id, method, language, probability, a short
transcript sample) and reused; a cached v1 «unknown» is re-checked by step 3 once.  Audio downloads share the
YouTube pace (films.locks 'youtube_http'); 429 / bot check → yt_mod.Blocked (stop at once).

Alternatives must have been uploaded no earlier than 400 days before the title's release (uploaded_too_early: an
older upload with the same Russian name is another film or a clip channel).  A title whose film has no dialogue
(TMDB spoken language «xx», e.g. «Поток» / Flow) and whose trailer has no speech gets trailer_ru_check = 'no_speech'.

For a title whose trailer turns out not to be in Russian: the other candidates of the same title (runners-up of
the trailer search, plus one extra «дубляж» query) are checked the same way; the first confirmed Russian one
replaces it.  If none is confirmed the link is removed from titles.trailer_ru_url (only links this project found
on YouTube or TMDB) and the finding stays in the log. The confirmation method is written to titles.trailer_ru_check
(audio_track / asr / speech; teaser_only = confirmed Russian but < 90 s and no longer one exists (set by the search
pipelines); no_speech; neural / other / unknown / unconfirmed) — uploaded (manual) captions never confirm anything.
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

# ---- speech check v2 (no VAD) — text rules, pure functions (tests/test_trailer_lang.py) ---------------------------
SR = 16000
WIN = 30            # seconds per window of the no-VAD pass
ISLAND = 8          # seconds per piece of the speech-island pass
MIN_WIN = 10        # shorter tails are padded with silence and Whisper hallucinates on them
CONFIRMED = ("audio_track", "asr", "speech", "teaser_only", "no_speech")
# Whisper's well-known hallucinations on music / silence (subtitle credits, outros) — never speech
_FILLER = re.compile(r"субтитр|dimatorzok|продолжение следует|спасибо за (?:просмотр|внимание)|редактор|корректор|"
                     r"подписывайтесь|ставьте лайк|amara\.org|субтітр|дякую за перегляд|thanks for watching|"
                     r"subtitles by|please subscribe|thank you for watching", re.I)
_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_WORD = re.compile(r"\w{2,}")
_CYR_ANY = re.compile(r"[а-яёіїєґ]{3,}", re.I)
_BLOCK = re.compile(r"429|Too Many Requests|not a bot|confirm you.re not|unusual traffic", re.I)


def is_filler(text: str | None) -> bool:
    """A Whisper filler phrase (credits / outro hallucinated on music), not speech."""
    return bool(_FILLER.search(text or ""))


def speech_words(text: str | None) -> set[str]:
    """Real spoken words of a transcript: sound tags in brackets and ALL-CAPS tags («НАПРЯЖЕННАЯ МУЗЫКА») dropped,
    each word counted once (a chant «Superman! Superman! …» is one word)."""
    return {w.lower() for w in _WORD.findall(_TAGS.sub(" ", text or "")) if not w.isupper()}


def window_verdict(wins: list[dict]) -> dict:
    """Verdict over 30-second windows [{'lang', 'p', 'words', 'text'}]: only confident windows (p >= RU_MIN_PROB,
    >= 2 real words) count.  ru: >= 3 Russian words in Cyrillic (no Ukrainian-only letters) and >= 2x the words of
    the other languages; uk / other alike (other needs >= 5 words)."""
    conf = [w for w in wins if (w.get("p") or 0) >= RU_MIN_PROB and (w.get("words") or 0) >= 2]
    by: dict[str, int] = {}
    for w in conf:
        by[w["lang"]] = by.get(w["lang"], 0) + w["words"]
    ru_text = " ".join(w.get("text") or "" for w in conf if w["lang"] == "ru")
    uk_text = " ".join(w.get("text") or "" for w in conf if w["lang"] == "uk")
    ru, uk = by.get("ru", 0), by.get("uk", 0)
    other = sum(v for k, v in by.items() if k not in ("ru", "uk"))
    out = {"by_lang": by, "ru_words": ru, "other_words": other}
    if ru >= 3 and _CYR_ANY.search(ru_text) and not _UK_ONLY.search(ru_text) and ru >= 2 * (other + uk):
        out.update(verdict="ru", language="ru", transcript=ru_text[:300])
    elif uk >= 3 and uk >= 2 * (other + ru):
        out.update(verdict="uk", language="uk", transcript=uk_text[:300])
    elif other >= 5 and other >= 2 * (ru + uk):
        lang = max((k for k in by if k not in ("ru", "uk")), key=by.get)
        out.update(verdict="other", language=lang,
                   transcript=" ".join(w.get("text") or "" for w in conf if w["lang"] == lang)[:300])
    else:
        out.update(verdict="unknown", language=None)
    return out


def island_verdict(ru_pieces: int, ru_words: int, other: dict[str, int], uk_pieces: int) -> dict:
    """Verdict over 8-second pieces: ru_pieces = confidently Russian pieces with real Cyrillic words, ru_words = their
    distinct words, other = {lang: confident pieces}, uk_pieces."""
    n_other = sum(other.values())
    if ru_words >= 3 and ru_pieces >= 2 * (n_other + uk_pieces):
        return {"verdict": "ru", "language": "ru"}
    if uk_pieces >= 3 and uk_pieces >= 2 * (n_other + ru_pieces):
        return {"verdict": "uk", "language": "uk"}
    if n_other >= 3 and ru_pieces == 0 and uk_pieces == 0:
        return {"verdict": "other", "language": max(other, key=other.get)}
    return {"verdict": "unknown", "language": None}


def uploaded_too_early(publish_date: str | None, release_date: str | None, days: int = 400) -> bool:
    """The video was uploaded more than `days` before the title's release: an older upload carrying the same Russian
    name is another film or a clip channel (seen: «Синяя коробка» 2024 upload for a 2026 film)."""
    if not publish_date or not release_date:
        return False
    try:
        return (datetime.fromisoformat(release_date[:10]) - datetime.fromisoformat(publish_date[:10])).days > days
    except ValueError:
        return False


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
    from films.locks import pace
    pace("youtube_http", yt_mod.MIN_INTERVAL)   # shared YouTube rate with search / watch pages (all processes)
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
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        if _BLOCK.search(msg):
            raise yt_mod.Blocked(f"yt-dlp: {msg[:150]}") from e
        return {"language": None, "error": msg}
    finally:
        for f in AUDIO_DIR.glob(f"{video_id}.*"):  # also .part leftovers of an interrupted download
            f.unlink(missing_ok=True)


def _windows(audio, offset: int) -> list[dict]:
    """No-VAD pass: WIN-second windows from `offset` s, each transcribed with automatic language detection."""
    model = _whisper()
    wins = []
    for start in range(offset * SR, len(audio), WIN * SR):
        chunk = audio[start:start + WIN * SR]
        if len(chunk) < MIN_WIN * SR:
            continue
        segs, info = model.transcribe(chunk, vad_filter=False, beam_size=1, condition_on_previous_text=False,
                                      temperature=0.0)
        segs = [s for s in segs if s.no_speech_prob < 0.6 and len(s.text.strip()) > 1
                and (s.compression_ratio or 0) <= 2.4 and (s.avg_logprob or 0) >= -1.2 and not is_filler(s.text)]
        text = " ".join(s.text.strip() for s in segs)
        wins.append({"t": start // SR, "lang": info.language, "p": round(info.language_probability, 3),
                     "words": len(speech_words(text)), "text": text[:160]})
    return wins


def _islands(audio) -> dict:
    """Speech islands under music: the language of every ISLAND-second piece; a confidently Russian piece is
    transcribed in Russian and counts only with real Cyrillic words."""
    model = _whisper()
    ru_pieces, ru_words, other, uk, texts = 0, set(), {}, 0, []
    for st in range(0, len(audio), ISLAND * SR):
        ch = audio[st:st + ISLAND * SR]
        if len(ch) < 4 * SR:
            continue
        lang, prob, _ = model.detect_language(ch)
        if prob < RU_MIN_PROB:
            continue
        if lang == "ru":
            segs, _i = model.transcribe(ch, language="ru", beam_size=1, condition_on_previous_text=False,
                                        temperature=0.0)
            segs = [s for s in segs if s.no_speech_prob < 0.6 and (s.avg_logprob or -9) >= -1.0
                    and (s.compression_ratio or 0) <= 2.4 and not is_filler(s.text)]
            text = " ".join(s.text.strip() for s in segs)
            ws = speech_words(text)
            if ws and _CYR_ANY.search(text):
                ru_pieces += 1
                ru_words |= ws
                texts.append(f"{st // SR}s: {text[:80]}")
        elif lang == "uk":
            uk += 1
        else:
            other[lang] = other.get(lang, 0) + 1
    out = island_verdict(ru_pieces, len(ru_words), other, uk)
    out["islands"] = {"ru_pieces": ru_pieces, "ru_words": len(ru_words), "other_pieces": other, "uk_pieces": uk}
    if out["verdict"] == "ru":
        out["transcript"] = " | ".join(texts)[:300]
    return out


def speech_language_v2(video_id: str) -> dict:
    """Step 3: download the audio (deleted at once), no-VAD 30-second windows (from 0 s, then from 15 s), then
    8-second speech islands.  {'verdict', 'language', 'probability', 'transcript', 'by_lang', 'islands'} or
    {'error'}."""
    try:
        path = _download_audio(video_id)
        if not path:
            return {"verdict": "unknown", "error": "no audio downloaded"}
        audio = _pcm16k(path)
        wins = _windows(audio, 0)
        out = window_verdict(wins)
        if out["verdict"] == "unknown":
            wins += _windows(audio, WIN // 2)
            out = window_verdict(wins)
        if out["verdict"] == "unknown":
            out.update(_islands(audio))
        out["probability"] = max([w["p"] for w in wins if w["lang"] == out.get("language") and w["words"] >= 2],
                                 default=None)
        out["windows"] = len(wins)
        return out
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        if _BLOCK.search(msg):
            raise yt_mod.Blocked(f"yt-dlp: {msg[:150]}") from e
        return {"verdict": "unknown", "error": msg}
    finally:
        for f in AUDIO_DIR.glob(f"{video_id}.*"):
            f.unlink(missing_ok=True)


def check_video(yt: yt_mod.YouTube, video_id: str, cache: dict) -> dict:
    """{'id', 'verdict': 'ru' | 'uk' | 'other' | 'unknown', 'method', ...} — cached in audio_checks.jsonl.
    A v1 «unknown» (VAD found no speech) goes on to step 3, the no-VAD check (also for an older cached verdict)."""
    if video_id in cache:
        rec = cache[video_id]
        if not _needs_v2(rec):
            return rec
        return _apply_v2(video_id, dict(rec), cache)
    rec = _check_v1(yt, video_id)
    if not rec.get("error") and _needs_v2(rec):
        return _apply_v2(video_id, rec, cache)
    if not rec.get("error"):        # a failed download / decode is retried next time, not remembered
        _save_check(rec)
        cache[video_id] = rec
    return rec


def _needs_v2(rec: dict) -> bool:
    return rec.get("verdict") == "unknown" and not rec.get("error") and \
        not str(rec.get("method") or "").startswith("speech_v2")


def _apply_v2(video_id: str, rec: dict, cache: dict) -> dict:
    s = speech_language_v2(video_id)
    if s.get("error"):                # step 3 could not run: the v1 verdict is remembered, step 3 retried next time
        if video_id not in cache:
            _save_check(rec)
            cache[video_id] = rec
        return dict(rec, error_v2=s["error"])
    rec["v1"] = {k: rec.pop(k, None) for k in ("verdict", "language", "probability", "speech_seconds", "cyrillic",
                                               "uk_letters", "transcript")}
    rec.update({k: s.get(k) for k in ("verdict", "language", "probability", "transcript", "by_lang", "islands")},
               method=f"speech_v2 (no VAD: {WIN} s windows, {ISLAND} s islands); v1 {rec.get('method')} said unknown",
               at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    rec["transcript"] = (rec.get("transcript") or "")[:300]
    _save_check(rec)
    cache[video_id] = rec
    return rec


def _check_v1(yt: yt_mod.YouTube, video_id: str) -> dict:
    """Steps 1–2: watch-page metadata, then speech with VAD."""
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
    return rec


def unconfirmed_titles(conn) -> list[int]:
    """Titles with a Russian trailer (any source — YouTube search or TMDB) whose Russian speech is not confirmed
    by the original audio track, automatic captions or speech recognition (titles.trailer_ru_check)."""
    q = ",".join("?" * len(CONFIRMED))
    return [tid for (tid,) in conn.execute(
        f"""SELECT id FROM titles WHERE trailer_ru_url IS NOT NULL
            AND coalesce(trailer_ru_check, 'unconfirmed') NOT IN ({q})""" + yt_mod.NOT_WATCHED, CONFIRMED)]


def _alternatives(yt: yt_mod.YouTube, conn, tid: int, current: str) -> list[str]:
    """Russian candidates of the same title: runners-up of the search, then «дубляж» queries by the Russian and the
    original / Latin name; without uploads older than 400 days before the release, unplayable or longer than 5 min."""
    d = yt_mod.latest_decisions().get(tid) or {}
    ids = [c["id"] for c in d.get("runners_up") or [] if c.get("lang") == "ru" and not c.get("reject")]
    row = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
    ctx = yt_mod.title_context(conn, row)
    names = []
    for k in ("title_ru", "title_original", "title_latin"):
        if row[k] and yt_mod.norm(row[k]) not in {yt_mod.norm(x) for x in names}:
            names.append(row[k])
    for name in names[:2]:
        res = yt.search(" ".join(f"{name} {row['year'] or ''} трейлер дубляж".split()))
        for v in res.get("videos") or []:
            c = yt_mod.evaluate(v, ctx, res.get("fetched_at") or "")
            if c.get("ok") and c.get("lang") == "ru":
                ids.append(c["id"])
    out = []
    for i in dict.fromkeys(ids):
        if i == current:
            continue
        w = yt.watch(i)
        if w.get("status") != "OK" or (w.get("length") or 0) > 300 or \
                uploaded_too_early(w.get("publish_date"), row["release_date"]):
            continue
        out.append(i)
    return out


def _no_dialogue(conn, tid: int) -> bool:
    """The film has no dialogue (TMDB spoken language «xx», e.g. «Поток» / Flow): its trailer cannot have speech."""
    row = conn.execute("SELECT spoken_languages FROM titles WHERE id = ?", (tid,)).fetchone()
    try:
        return json.loads(row[0] or "[]") == ["xx"]
    except (TypeError, ValueError):
        return False


def check_titles(ids: list[int], dry_run: bool = False, log=print) -> dict:
    conn = db.connect()
    yt = yt_mod.YouTube()
    cache = _load_checks()
    stats = {"titles": len(ids), "confirmed_ru": 0, "no_speech": 0, "replaced": 0, "removed": 0, "unknown": 0,
             "details": []}
    alt_rank = {"uk": 0, "original": 1, "ru_sub": 2, "any": 3}
    for n, tid in enumerate(ids, 1):
        row = conn.execute("""SELECT id, title_ru, year, trailer_ru_url, trailer_ru_source, trailer_alt_kind
                              FROM titles WHERE id = ?""", (tid,)).fetchone()
        vid = (row["trailer_ru_url"] or "").split("v=")[-1]
        r = check_video(yt, vid, cache)
        det = {"title_id": tid, "title": row["title_ru"], "year": row["year"], "video": vid, "verdict": r["verdict"],
               "method": r.get("method"), "language": r.get("language"), "probability": r.get("probability")}
        check = None
        if r["verdict"] == "ru":
            stats["confirmed_ru"] += 1
            check = check_method(r)
        elif r["verdict"] == "unknown" and _no_dialogue(conn, tid):
            stats["no_speech"] += 1     # a film without dialogue: no trailer of it has speech, the link stays
            check = "no_speech"
        else:
            new = None                  # not Russian, or no decidable speech: a confirmed Russian alternative wins
            for alt in _alternatives(yt, conn, tid, vid):
                if check_video(yt, alt, cache)["verdict"] == "ru":
                    new = alt
                    break
            det["replacement"] = new
            if new:
                stats["replaced"] += 1
                check = check_method(check_video(yt, new, cache))
                if not dry_run:
                    with conn:
                        conn.execute("""UPDATE titles SET trailer_ru_url = ?, trailer_ru_source = 'youtube_search',
                                        trailer_checked_at = datetime('now') WHERE id = ?""",
                                     (f"https://www.youtube.com/watch?v={new}", tid))
            elif r["verdict"] == "unknown":
                stats["unknown"] += 1   # keep the link; listed for the owner
                check = "unknown"
            else:
                stats["removed"] += 1   # not Russian speech (or neural / unplayable): the finding stays in the log
                kind = "uk" if r["verdict"] == "uk" else \
                    ("ru_sub" if r.get("ru_manual_captions") else "original") if r["verdict"] == "other" else None
                det["moved_to_alt"] = kind if kind and alt_rank[kind] < alt_rank.get(row["trailer_alt_kind"], 9) else None
                if not dry_run:
                    with conn:
                        conn.execute("""UPDATE titles SET trailer_ru_url = NULL, trailer_checked_at = datetime('now')
                                        WHERE id = ?""", (tid,))
                        if det["moved_to_alt"]:   # a real trailer in another language stays as the fallback one
                            conn.execute("""UPDATE titles SET trailer_alt_url = ?, trailer_alt_kind = ?,
                                            trailer_alt_source = ? WHERE id = ?""",
                                         (row["trailer_ru_url"], kind, row["trailer_ru_source"] or "youtube_search", tid))
        if not dry_run:
            with conn:
                conn.execute("UPDATE titles SET trailer_ru_check = ?, trailer_ru_checked_at = datetime('now') WHERE id = ?",
                             (check, tid))
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
    try:
        st = check_titles(ids, a.dry_run, log=lambda m: print(m, file=sys.stderr, flush=True))
    except yt_mod.Blocked as e:   # 429 / bot check: stop at once (owner rule); done titles are already written
        shutil.rmtree(AUDIO_DIR, ignore_errors=True)
        print(f"STOPPED: YouTube blocked: {e}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(st, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
