"""Is the trailer really spoken in Russian? For trailers whose YouTube metadata does not say it (no audio-track
language, no automatic captions), and on request for any title.

Usage:
    python -X utf8 -m films.sources.trailer_lang check --unconfirmed [--limit N] [--dry-run]
    python -X utf8 -m films.sources.trailer_lang check --ids 12,34
    python -X utf8 -m films.sources.trailer_lang video <youtube_id>          # one video, prints the verdict
    python -X utf8 -m films.sources.trailer_lang cuts 2827 6870              # distinct cuts → owner questions
    python -X utf8 -m films.sources.trailer_lang answer 330=LPmFV3HaPG0      # the owner's pick (or 330=none)

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
import time
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
CONFIRMED = ("audio_track", "asr", "speech", "teaser_only", "no_speech", "owner")   # owner: the owner listened and chose
# Whisper's well-known hallucinations on music / silence (subtitle credits, outros) — never speech
_FILLER = re.compile(r"субтитр|dimatorzok|продолжение следует|спасибо за (?:просмотр|внимание)|редактор|корректор|"
                     r"подписывайтесь|ставьте лайк|amara\.org|субтітр|дякую за перегляд|thanks for watching|"
                     r"subtitles by|please subscribe|thank you for watching", re.I)
_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_WORD = re.compile(r"\w{2,}")
_CYR_ANY = re.compile(r"[а-яёіїєґ]{3,}", re.I)
# «Please sign in. This helps protect our community» and «Sign in to confirm your age» are per-video sign-in walls
# (checked 2026-10-25: other videos still download right after) — skill trailer-check 3б (Chrome), not a block
_BLOCK = re.compile(r"429|Too Many Requests|not a bot|confirm you.re not|unusual traffic", re.I)
ALT_BUDGET_S = 180   # per title: checking alternative trailers stops after 3 min (owner 2026-10-25: «10 мин без движения — плохо»)


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
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "geo_bypass_country": "UA",
            # YouTube's JS challenges: Deno + yt-dlp-ejs (owner 2026-10-25 «Да, поставить Deno»); without a JS runtime
            # some videos are «This video is not available».  No remote components are fetched.
            "js_runtimes": {"deno": {}}}
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
            started = time.monotonic()
            for alt in _alternatives(yt, conn, tid, vid):
                if time.monotonic() - started > ALT_BUDGET_S:
                    # each candidate = audio download + speech recognition; «Восемь сотен» held the run 12 min
                    # (2026-10-25) — the title stays as it is (unconfirmed), the next run goes on from the cache
                    det["alt_timeout"] = True
                    stats["alt_timeout"] = stats.get("alt_timeout", 0) + 1
                    log(f"  {tid}: alternatives over {ALT_BUDGET_S} s — left unconfirmed")
                    break
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


_WATCHED_FACTS: dict[str, dict] | None = None


def _watch_facts() -> dict[str, dict]:
    """{video id: last watch-page record} from youtube.VIDEO_CACHE — the video's own title and date on YouTube (TMDB's
    name may differ: «STARGATE SG1 season 1 Trailer» is «Stargate SG-1 Trailer» there) and whether it still plays."""
    global _WATCHED_FACTS
    if _WATCHED_FACTS is None:
        _WATCHED_FACTS = {}
        try:
            with yt_mod.VIDEO_CACHE.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    _WATCHED_FACTS[r.get("id")] = r
        except OSError:
            pass
    return _WATCHED_FACTS


def _dead_videos() -> set[str]:
    """Videos whose watch page said removed / private / sign-in only."""
    return {k for k, r in _watch_facts().items() if r.get("status") not in (None, "OK")}


def _season_tmdb(conn, stm: int, n: int | None) -> tuple[str | None, str | None]:
    """(uk url, original url) for one season: the season's own TMDB videos, else series-level videos of this season
    (PLAN V4, 2026-10-29: the series-level trailer — usually season 1's — stood on every season). Season 1 / unknown
    season takes the series-level ones as before."""
    from . import tmdb
    from .. import season_trailers as st
    series = tmdb.tv(conn, stm)
    if not series:
        return None, None
    if (n or 1) > 1:
        dates = {s["season_number"]: s["air_date"] for s in series.get("seasons") or [] if s.get("air_date")}
        dead = _dead_videos()

        def first(vs, own=False):
            # the season's own videos: not one naming another season TMDB has («STARGATE SG1 season 1 Trailer» on
            # S2–S10); series-level ones: this season's by name or date; never a video YouTube says is gone
            for v in vs:
                if v["not_trailer"] or _vid(v["url"]) in dead:
                    continue
                seen = _watch_facts().get(_vid(v["url"])) or {}   # YouTube's own title / date when known
                ok = st.verdict(n, seen.get("title") or v["name"], seen.get("publish_date") or v["published_at"], dates)[0]
                if ok or (own and ok is None):
                    return v["url"]
            return None
        own = tmdb.pick_trailers((tmdb.season_raw(conn, stm, n) or {}).get("videos"))
        uk, en = first(own["trailers_uk"], True), first(own["trailers_en"], True)
        if uk or en:
            return uk, en
        return first(series.get("trailers_uk") or []), first(series.get("trailers_en") or [])
    return (series.get("trailers_uk") or [{}])[0].get("url"), series.get("trailer_en_url")


def fill_fallbacks(conn, only_missing: bool = False) -> dict:
    """titles without a Russian-speech trailer get trailer_alt_* in the owner's order (2026-10-23):
    Ukrainian voice-over (YouTube search finding, then TMDB) → the original trailer (TMDB here, YouTube in
    search_originals) → the original with Russian subtitles (the search's 'subtitled' finding) → any trailer, best
    quality (search_originals, last resort)."""
    from . import tmdb
    latest = yt_mod.latest_decisions()
    stats = {"uk": 0, "ru_sub": 0, "original": 0, "none": 0}
    # the owner's own pick stays (2026-10-28)
    rows = conn.execute("""SELECT id, kind, tmdb_id, series_tmdb_id, season_number FROM titles
                           WHERE trailer_ru_url IS NULL AND coalesce(trailer_alt_source, '') != 'owner'"""
                        + (" AND trailer_alt_url IS NULL" if only_missing else "")).fetchall()
    with conn:
        for tid, kind, tm, stm, season in rows:
            d = latest.get(tid) or {}
            url = kind_ = src = None
            uk = [c for c in d.get("uk") or [] if not c.get("reject")]
            sub = [c for c in d.get("subtitled") or [] if not c.get("reject")]
            if kind == "movie" and tm:
                body = tmdb.movie(conn, tm)
                tmdb_uk = ((body or {}).get("trailers_uk") or [{}])[0].get("url")
                tmdb_en = (body or {}).get("trailer_en_url")
            else:
                tmdb_uk, tmdb_en = _season_tmdb(conn, stm, season) if stm else (None, None)
            if uk:
                url, kind_, src = f"https://www.youtube.com/watch?v={uk[0]['id']}", "uk", "youtube_search"
            elif tmdb_uk:
                url, kind_, src = tmdb_uk, "uk", "tmdb"
            elif tmdb_en:
                url, kind_, src = tmdb_en, "original", "tmdb"
            elif sub:   # owner: Russian subtitles only after the original
                url, kind_, src = f"https://www.youtube.com/watch?v={sub[0]['id']}", "ru_sub", "youtube_search"
            conn.execute("UPDATE titles SET trailer_alt_url = ?, trailer_alt_kind = ?, trailer_alt_source = ? WHERE id = ?",
                         (url, kind_, src, tid))
            stats[kind_ or "none"] += 1
    db.mark_sync(conn, "trailer_fallbacks", None, stats)
    return stats


# words a video title may put before the name («Official Trailer: Doubt», «NEW 4K Trailer Doubt», «Bande-annonce Le
# Mystère …», «LEGO Disney Frozen: …»); compared as youtube.norm tokens («трейлер» → «треилер»)
_ORIG_PREFIX = {yt_mod.norm(w) for w in (
    "official", "new", "final", "main", "first", "exclusive", "international", "us", "uk", "hd", "4k", "uhd", "trailer",
    "teaser", "трейлер", "тизер", "netflix", "hbo", "max", "prime", "video", "apple", "tv", "disney", "hulu", "peacock",
    "paramount", "original", "eng", "engsub", "sub", "subs", "drama", "series", "movie", "film", "bande", "annonce", "lego",
    "ard", "zdf", "tf1", "m6", "trt", "rtl", "canal", "official", "virallinen", "officiell", "oficjalny", "officiele")}
# words a video title may put right after the name («Doubt 2008 Official Trailer», «Stuntman (2024) 武替道 - Movie Trailer»,
# «Missing (2024) Japanese Movie Trailer», «Paolo Temp.1», «(ARD Trailer)») — any other word makes it another title
# («The Company You Keep»)
_ORIG_SUFFIX = _ORIG_PREFIX | {yt_mod.norm(w) for w in (
    "season", "temp", "temporada", "saison", "staffel", "sezon", "сезон", "tanıtım", "fragman", "fragmanı", "bande",
    "zwiastun", "promo", "japanese", "korean", "chinese", "thai", "indian", "french", "german", "spanish", "italian",
    "turkish", "english", "russian", "polish", "dutch", "danish", "swedish", "norwegian", "finnish", "brazilian", "mexican",
    "argentine", "hindi", "dubbed", "dub", "version", "subtitles", "subtitled", "full", "redband", "red", "band",
    "ufficiale", "oficial", "officiel", "officielle", "offizieller", "resmi", "vf", "vostfr", "русский", "трейлер", "тизер",
    "the", "movie", "film", "official", "säsong", "sæson", "sesong", "sezona", "seizoen", "series", "traileri",
    # review round 13: «Vefa Sultan İlk Tanıtım», «Vicdansız Dizisi …», «Hawaiian Vacation Short Official Trailer»,
    # «… elokuvateattereissa (traileri)», «… Tamil Trailer»
    "ilk", "dizisi", "short", "elokuvateattereissa", "tamil", "telugu", "malayalam", "kannada")}
# before the name only: «Bande-annonce de l'École des espions»
_PREFIX_ONLY = {"de", "du", "des", "l", "d"}
_ARTICLES = {"il", "le", "la", "el", "der", "die", "das", "o", "a", "un", "une"}
_YEAR_TOKEN = re.compile(r"(?:19|20)\d\d")   # a year after the name, not a number («Pati 2 | zwiastun» is the sequel)
# a word of another script — no Latin letter and at least one non-ASCII character («红色日子», «3分鐘預告»)
_NON_ASCII = re.compile(r"^[^a-z]*[^\x00-\x7f][^a-z]*$")
_CYR_WORD = re.compile(r"[а-яёіїєґ]")
# Russian / Ukrainian words describing the video before a Cyrillic name («Тизер новой серии / Мастер Витя и Мотор»)
_CYR_PREFIX = {yt_mod.norm(w) for w in (
    "официальный", "официальный", "офіційний", "трейлер", "тизер", "анонс", "новый", "новой", "новые", "серии", "серия",
    "сериала", "сериал", "серіалу", "серіал", "фильм", "фильма", "фільм", "мультфильм", "мультфильма", "мультсериал",
    "мультсериала", "мультфільм", "русский", "український", "премьера", "прем'єра", "сезон", "сезона", "второй", "1", "2")}
# tags in brackets that are no name: «【DRAMA SERIES】», «[ENG SUB]», «【Trailer】»
_TAG_WORDS = _ORIG_PREFIX | {"short", "official", "trailer", "teaser", "full", "ep", "multi"}
# words saying the video's language is the original one (a one-word name of a non-English title needs one, a year or
# another script: «Kaboul - Bande annonce VF - France TV»; «Patriot - Official Trailer | Prime Video» is another film)
_LANG_WORDS = re.compile(r"bande|annonce|\bvf\b|vostfr|officiel|offizieller|deutsch|ufficiale|italiano|oficial|tráiler|"
                         r"fragman|tan[ıi]t[ıi]m|zwiastun|virallinen|traileri|officiell|säsong|sæson|sesong|"
                         r"temporada|saison|staffel|sezon|resmi|offici[eë]le|hindi|tamil|telugu|malayalam|kannada|"
                         r"turkish|予告|预告|預告|예고", re.I)


def _plain(vt: str) -> str:
    """Full-width bars and colons as ASCII; tags in 【】 / [] as separators — a tag of service words dropped («【DRAMA
    SERIES】Name｜Trailer», «[ENG SUB]»), a bracketed name kept between bars («|【西施新传 New Biography of Xishi】|»)."""
    vt = vt.replace("｜", "|").replace("：", ":").replace("💟", " ")

    def tag(m):
        inner = m.group(1)
        return " | " if all(w in _TAG_WORDS for w in yt_mod.tokens(inner)) else f" | {inner} | "
    # round brackets are no separator: «Name (Alt name)» let clips, «(Movie)» and other names in (review round 13)
    return re.sub(r"[\[【]([^\]】]*)[\]】]", tag, vt)


_ORIG_EPISODE = re.compile(r"\b\d+\s*\.?\s*(?:bolum|bölüm|folge|серия|серии)\b|\b(?:episode|ep|серия|bolum|bölüm|folge|"
                           r"capitulo|capítulo)\s*\d", re.I)
_SEASON_WORDS = r"season|сезон|sezon|staffel|temporada|saison|säsong|sasong|sæson|sesong|sezona|seizoen|stagione"
_ORIG_SEASON = re.compile(r"\b(?:" + _SEASON_WORDS + r"|series)\s*(\d{1,2})\b|\b(\d{1,2})\s*(?:\.\s*)?"
                          r"(?:" + _SEASON_WORDS + r")\b", re.I)


_ANY_TRAILER = re.compile(r"trailer|teaser|трейлер|тизер|трейлър|bande[- ]?annonce|fragman|tan[ıi]t[ıi]m|zwiastun|promo|"
                          r"tr[aá]iler|treiler|avance|anons|анонс|予告|预告|預告|예고", re.I)
_ANY_NOT = re.compile(r"\bintro\b|opening|\bcredits\b|\bclip\b|\bscene\b|deleted|explained|recap|\bending\b|сцена|отрывок|"
                      r"клип|пересказ|опенинг|эндинг|титры|заставка|\bop\b|\bed\b", re.I)


_INDIAN = {"hi": "hindi", "ta": "tamil", "te": "telugu", "ml": "malayalam", "kn": "kannada"}
_INDIAN_RE = re.compile(r"\b(" + "|".join(_INDIAN.values()) + r")\b", re.I)


def original_fits(name: str, year: int | None, kind: str | None, season: int | None, video_title: str | None,
                  lang: str | None = None, orig_lang: str | None = None, runtime: int | None = None) -> bool:
    """The video is a trailer of this very title (review round 9: «Doubt» took «Reasonable Doubt», «Jumanji» 1997 «Jumanji:
    Open World» 2026, «Blood & Sweat» «Blood, Sweat & Tears», season 1 «Arafta Season 2», episode promos «7. Bölüm»):
    the whole name as a phrase, opening the title or a part of it after | – : (or after words like «Official Trailer»);
    no year in the video title other than the title's ±1; a series' video naming no other season; no episode promo,
    game, review, reaction or fan edit. `lang` — the original language (TMDB) of a one-word name IMDb has namesakes for:
    a non-English one is ambiguous in English videos (review round 12: «Patriot», «Ghosts», «Surface») and needs a year,
    a word of its language or another script. `orig_lang` — the title's original language: a video naming another Indian
    language is a dub's trailer (review round 14: «Ayalaan» ta got the Telugu one); `runtime` — «Short» after the name
    only for a short film (round 14: «Dark Romance Short Film»)."""
    vt = video_title or ""
    need = yt_mod.tokens(name)
    if not need or _CUT_NOT.search(vt) or _ORIG_EPISODE.search(vt):
        return False
    if orig_lang and any(w.lower() != _INDIAN.get(orig_lang) for w in _INDIAN_RE.findall(vt)):
        return False
    # a year that is a part of the name is no year of the video («Estado de fuga 1986», review round 12)
    years = [int(y) for y in _CUT_YEAR.findall(vt) if y not in need]
    if year and years and not any(abs(y - year) <= 1 for y in years):
        return False
    if kind == "series" and season:
        named = {int(a or b) for a, b in _ORIG_SEASON.findall(vt)}
        # a later season's video names it, as for the Russian ones (PLAN V4, 2026-10-29: season 1's «Official Trailer»
        # stood on seasons 2–10); a whole-series trailer comes only from TMDB (_season_tmdb)
        # «Pati 2 | zwiastun»; not Turkish «Kirli Sepeti 2. Teaser» / «Mahkum 2. Tanıtımı» — the 2nd teaser, not season 2
        right_after = re.search(re.escape(name) + r"\W*0?" + str(season) + r"\b(?!\s*\.\s*(?:teaser|tan|fragman|trailer|promo))",
                                vt, re.I)
        if (named and season not in named) or (not named and season > 1 and not right_after):
            return False
    if len(need) == 1 and lang and lang != "en" and not (
            (year and any(abs(y - year) <= 1 for y in years)) or _LANG_WORDS.search(vt)
            or any(_NON_ASCII.match(w) for w in yt_mod.tokens(vt))):
        return False
    latin_name = not _CYR_WORD.search(" ".join(need))

    def other_script(w, after=False, quoted=False):
        # «红色日子 Red Days»; a Cyrillic word next to a Cyrillic name goes on with another name («Друзі та сусіди»,
        # «Телефонные мошенники» for «Мошенники»), unless it is a description word or the name is quoted after it
        # («Мультсериал "Нейроны" | Трейлер», «Официальный трейлер комедийной драмы "Билет из Рая"»)
        if not _NON_ASCII.match(w):
            return False
        return latin_name or not _CYR_WORD.search(w) or (not after and (quoted or w in _CYR_PREFIX))

    vt = _plain(vt)
    # the whole title too: a name with its own «:» / « - » («Red Days 2: Cecilia Trailer»), or the video's «:» inside the
    # name («Angel Flight: The Movie» for «Angel Flight THE MOVIE»)
    whole = [vt]
    pieces = re.split(r"(\s[|–—-]\s|[|:–—])", vt)
    parts, seps = pieces[0::2], pieces[1::2]
    first = re.escape(name.split()[0]) if name.split() else ""
    # a leading article the video may leave out («Ainsley McGregor Mysteries: …» for «The Ainsley McGregor Mysteries»)
    # (only «The»: «A Family Thanksgiving» is not «Family Thanksgiving»); one or two words left are too little without the
    # title's year in the video (review round 14: «The Shadow Dancer» 2005 took «Shadow Dancer» 2012)
    year_seen = bool(year and year in years)
    needs = [need] + ([need[1:]] if need[0] == "the" and len(need) > 1 and (len(need) > 3 or year_seen) else [])
    short_film = bool(runtime and runtime < 40)
    for k, part in enumerate(parts + whole):
        toks = yt_mod.tokens(part)
        quoted = bool(first and re.search(r"[\"«“„”']\s*" + first, part, re.I))
        for nd, i in ((nd, i) for nd in needs for i in yt_mod.find_phrase(toks, nd)):
            # before the name: «Official Trailer», a tag, or the title in another script («红色日子 Red Days»)
            if not all(w in _ORIG_PREFIX or w in _PREFIX_ONLY or other_script(w, quoted=quoted) or _ANY_TRAILER.match(w)
                       for w in toks[:i]):
                continue
            # a short name opening a part after «Other name:» is the other work's subtitle («Dune: Prophecy», «BLADE
            # RUNNER: 2099», «Black Bag: Doppio Gioco» — review round 12); a longer one is this title under a franchise
            # tag («Cars Toon: Mater's Tall Tales»)
            if len(nd) <= 2 and 0 < k < len(parts) and seps[k - 1] == ":" and not all(
                    w in _ORIG_PREFIX or other_script(w) or _ANY_TRAILER.match(w) for w in yt_mod.tokens(parts[k - 1])):
                continue
            after = toks[i + len(nd):]
            if kind == "series" and (after[:1] in (["movie"], ["film"]) or after[:2] == ["the", "movie"]):
                continue   # the film of the series («No. 1 Sentai Gozyuger THE MOVIE»)
            # right after it: a year, «Official Trailer», a language, another script; a series' own season number
            # («Pati 2 | zwiastun» for S2, «Rüzgarlı Tepe 2. Sezon») — any other word is another title the name only
            # opens (review round 11: «The Company» → «The Company You Keep», «Dream» → «Dream Scenario»)
            season_no = kind == "series" and season and after[:1] == [str(season)]
            if after and not (season_no or _YEAR_TOKEN.fullmatch(after[0]) or after[0] in _ORIG_SUFFIX
                              or other_script(after[0], True) or _ANY_TRAILER.match(after[0])):
                continue
            if after[:1] == ["the"] and after[1:2] != ["movie"]:
                continue
            if after[:1] == ["short"] and not short_film:
                continue   # «Dark Romance Short Film» is a short of the name, not this feature
            # «Jumanji: Open World Trailer» — the name closing a part and a subtitle after «:» → another work; «: bande-
            # annonce», «: il trailer», «: Original Trailer», «: The Movie» are no subtitles
            nxt = yt_mod.tokens(parts[k + 1]) if k < len(seps) and seps[k] == ":" and not after else []
            if nxt and not (nxt[0] in _ORIG_PREFIX or _YEAR_TOKEN.fullmatch(nxt[0]) or _ANY_TRAILER.match(" ".join(nxt))
                            or (nxt[0] in _ARTICLES and len(nxt) > 1 and _ANY_TRAILER.match(nxt[1]))
                            or nxt[:2] == ["the", "movie"]):
                continue
            return True
    return False


def search_originals(conn, recheck: bool = False, log=print) -> dict:
    """Unwatched titles still without any trailer: the original trailer from YouTube search — «<original> <year> trailer»
    (series: «… season N trailer»); accepted by original_fits and saying trailer / teaser / трейлер, length 30 s – 5 min.
    `recheck`: the earlier finds are judged again from the search cache — a find no longer accepted is replaced or
    taken back."""
    yt = yt_mod.YouTube()
    stats = {"titles": 0, "found": 0, "replaced": 0, "taken_back": 0}
    again = " OR (trailer_alt_source = 'youtube_search' AND trailer_alt_kind IN ('original', 'any'))" if recheck else ""
    if recheck:
        # a row that is another's duplicate is not looked for any more — its old find goes (review round 12: 82 left)
        with conn:
            stats["taken_back"] += conn.execute(
                """UPDATE titles SET trailer_alt_url = NULL, trailer_alt_kind = NULL, trailer_alt_source = NULL
                   WHERE duplicate_of IS NOT NULL AND trailer_alt_source = 'youtube_search'
                     AND trailer_alt_kind IN ('original', 'any')""").rowcount
    rows = conn.execute(f"""SELECT id, title_original, title_latin, year, kind, season_number, trailer_alt_url,
                               trailer_alt_kind, original_language, runtime_min FROM titles
                            WHERE trailer_ru_url IS NULL AND (trailer_alt_url IS NULL OR trailer_alt_kind = 'ru_sub'{again})
                            AND duplicate_of IS NULL AND EXISTS (SELECT 1 FROM release_titles r WHERE r.title_id = titles.id)"""
                        + yt_mod.NOT_WATCHED).fetchall()
    dead = _dead_videos()   # removed / private videos are never picked again (PLAN V4)
    for tid, orig, latin, year, kind, season, had, had_kind, lang, runtime in rows:
        name = latin or orig
        if not name:
            continue
        stats["titles"] += 1
        q = f"{name} {'season ' + str(season) + ' ' if kind == 'series' and season else ''}{year or ''} trailer"
        res = yt.search(q)
        # a one-word name with IMDb namesakes («Surface», «Ghosts») is checked against its language (original_fits)
        ambiguous = len(yt_mod.tokens(name)) == 1 and conn.execute(
            """SELECT count(*) FROM imdb_basics WHERE primary_title = ? COLLATE NOCASE
               AND title_type IN ('movie', 'tvSeries', 'tvMiniSeries')""", (name,)).fetchone()[0] >= 2
        fits = [v for v in res.get("videos") or [] if original_fits(name, year, kind, season, v.get("title"),
                                                                       lang if ambiguous else None, lang, runtime)
                and 30 <= (yt_mod.parse_length(v.get("length")) or 0) <= 300 and v.get("id") not in dead]
        # normalized («Tráiler») and raw: youtube.norm folds «й» («трейлер» → «треилер», review round 12)
        pick = next((v for v in fits if re.search(r"trailer|teaser|тизер", yt_mod.norm(v.get("title")))
                     or re.search(r"трейлер", v.get("title") or "", re.I)), None)
        kind_ = "original"
        if not pick and (not had or had_kind in ("original", "any")):
            # nothing at all: any trailer of this title, preferring 4K / HD in the video title (owner: «в лучшем качестве»)
            # — a trailer in another language's word, not an intro, a scene or a recap (review round 10: 40 of 418 were)
            fits = [v for v in fits if _ANY_TRAILER.search(v.get("title") or "") and not _ANY_NOT.search(v.get("title") or "")]
            fits.sort(key=lambda v: (0 if re.search(r"4k|2160|uhd", v.get("title") or "", re.I) else
                                     1 if re.search(r"hd|1080", v.get("title") or "", re.I) else 2))
            pick, kind_ = (fits[0], "any") if fits else (None, None)
        url = f"https://www.youtube.com/watch?v={pick['id']}" if pick else None
        if had_kind in ("original", "any"):
            if url == had and (kind_ if url else None) == had_kind:   # the same video and kind
                continue
            stats["replaced" if url else "taken_back"] += 1
            with conn:
                conn.execute("UPDATE titles SET trailer_alt_url = ?, trailer_alt_kind = ?, trailer_alt_source = ? WHERE id = ?",
                             (url, kind_ if url else None, "youtube_search" if url else None, tid))
        elif pick:
            with conn:
                conn.execute("""UPDATE titles SET trailer_alt_url = ?, trailer_alt_kind = ?,
                                trailer_alt_source = 'youtube_search' WHERE id = ?""", (url, kind_, tid))
            stats["found"] += 1
        log(f"  {stats['titles']}/{len(rows)} {name} {year}: {'→ ' + pick['title'][:60] if pick else 'нет'}"
            f"{' (было ' + had + ')' if had_kind in ('original', 'any') else ''}")
    db.mark_sync(conn, "trailer_originals", None, stats)
    return stats


# ---- distinct trailer cuts for the owner (owner 2026-10-25) ---------------------------------------------------------
# «где написано, сколько времени идут другие и какой там язык? Я вижу один. Как мне выбрать?» and «если время одинаковое
# или почти одинаковое с разных каналов — это одно и то же, мне нужны реально разные длительности и их язык».
CUTS_FILE = db.ROOT / "data" / "cache" / "trailer_cuts.json"
QUESTIONS_FILE = db.ROOT / "data" / "cache" / "survey_queue" / "trailers_left.json"
CUT_TOLERANCE = 3        # seconds: uploads this close in length are one cut re-uploaded by different channels
_CUT_TRAILER = re.compile(r"trailer|teaser|трейлер|тизер|трейлър|預告|预告|tráiler|bande", re.I)
# not a trailer of the film: games, playthroughs, reviews, reactions, clips, fan edits / fan films, other series
_CUT_NOT = re.compile(r"gameplay|\bgame\b|геймплей|игр[аыу]\b|игров|прохожд|walkthrough|playthrough|\bps[2-5]\b|xbox|"
                      r"nintendo|steam|sega|let.?s ?play|обзор|review|reaction|реакция|разбор|behind the scenes|"
                      r"за кадром|интервью|interview|music video|\bost\b|soundtrack|full movie|фильм целиком|"
                      r"полный фильм|\bfan\b|фанат|re-?cut|modern trailer|trailer style|animated trailer|bloxburg|"
                      r"roblox|\bgta\b|minecraft|майнкрафт|for honor|ubisoft", re.I)
_CUT_EPISODE = re.compile(r"\bсери[яиюй]\b|сериал|\bseries\b|\bэпизод|\bepisode\b|\bсезон|\bseason\b", re.I)
_CUT_YEAR = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
_T2S = str.maketrans("無機變戰劍傳說華語電影預", "无机变战剑传说华语电影预")   # traditional → simplified (name match)
_CUT_HD = re.compile(r"4k|2160|uhd|1080|hd", re.I)
_CLAIMS_RU = re.compile(r"рус|дубл|озвуч", re.I)
_LANG_RU = {"ru": "русский", "en": "английский", "zh": "китайский", "yue": "кантонский", "fr": "французский",
            "uk": "украинский", "ja": "японский", "ko": "корейский", "de": "немецкий", "es": "испанский",
            "it": "итальянский", "ar": "арабский", "tt": "татарский", "ba": "башкирский", "kk": "казахский",
            "hi": "хинди", "tr": "турецкий", "pt": "португальский", "pl": "польский", "th": "тайский"}


def mmss(s) -> str:
    s = int(s or 0)
    return f"{s} с" if s < 60 else (f"{s // 60} мин {s % 60} с" if s % 60 else f"{s // 60} мин")


def cut_title_ok(row: dict, title: str | None, cast: set[str] = frozenset()) -> bool:
    """A search result is a trailer of THIS film: says trailer / teaser; not a game / review / fan edit / episode of a
    series; no other year in the title (±1); the Russian / Ukrainian or original non-Latin name is in it, or a Latin name
    confirmed by the year, a cast / director name or a name of 3+ words («Hidden Blade» alone is also Assassin's Creed)."""
    title = title or ""
    if not _CUT_TRAILER.search(title) or _CUT_NOT.search(title):
        return False
    if row.get("kind", "movie") == "movie" and _CUT_EPISODE.search(title):
        return False
    if row.get("kind") == "series" and row.get("season_number"):
        # a season's trailer names that season; season 1 may go without a number, a later one may not
        # (review 2026-10-28: Heartstopper S3 was offered S1 / S2 cuts and the «Forever» film)
        nums = yt_mod.season_mentions(title)
        if (nums and row["season_number"] not in nums) or (not nums and row["season_number"] > 1):
            return False
    ys = [int(x) for x in _CUT_YEAR.findall(title)]
    if ys and row.get("year") and not any(abs(y - row["year"]) <= 1 for y in ys):
        return False
    t = f" {yt_mod.norm(title.translate(_T2S))} "

    def has(n):
        return bool(n) and bool(yt_mod.norm(n)) and f" {yt_mod.norm(n.translate(_T2S))} " in t
    if has(row.get("title_ru")) or has(row.get("title_uk")):
        return True
    orig = row.get("title_original") or ""
    if orig and not re.search(r"[A-Za-z]", orig) and has(orig):
        return True
    latin = [n for n in (row.get("title_latin"), orig if re.search(r"[A-Za-z]", orig) else None) if n and has(n)]
    if not latin:
        return False
    return bool(ys) or bool(set(cast) & set(t.split())) or any(len(yt_mod.norm(n).split()) >= 3 for n in latin)


def group_cuts(videos: list[dict], tol: int = CUT_TOLERANCE) -> list[list[dict]]:
    """Uploads sorted by length; a length within `tol` s of the previous one joins its cut."""
    groups: list[list[dict]] = []
    last = None
    for v in sorted((v for v in videos if v.get("length")), key=lambda v: v["length"]):
        if groups and last is not None and v["length"] - last <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
        last = v["length"]
    return groups


def best_upload_key(v: dict) -> tuple:
    """Official (verified) channel first, then 4K / HD in the title, then views."""
    verified = any("VERIFIED" in (b or "") for b in v.get("owner_badges") or [])
    return (0 if verified else 1, 0 if _CUT_HD.search(v.get("title") or "") else 1, -(v.get("views") or 0))


def cut_language(c: dict) -> str:
    """What the owner reads: русский / английский / … / «речь не распознана (под музыкой)» — never «речи нет» unless
    someone listened (no confident speech = music only, or speech the recogniser cannot separate from the music)."""
    v = c.get("verdict")
    if v == "ru":
        return "русский"
    if v == "uk":
        return "украинский"
    if v == "neural":
        return "нейросетевая озвучка"
    if c.get("error"):
        hint = c.get("meta_lang")
        return "язык не проверить — YouTube не отдаёт звук" + (f" (по данным YouTube: {_LANG_RU.get(hint, hint)})"
                                                                if hint else "")
    if v == "other" and c.get("language") in _LANG_RU and (c.get("transcript") or "").strip():
        return _LANG_RU[c["language"]]
    return "речь не распознана (под музыкой)"


def cut_quote(c: dict) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"\d+s: ", "", c.get("transcript") or "")).strip(" |")
    if not t or c.get("error") or c.get("verdict") not in ("ru", "uk", "other") or \
            cut_language(c).startswith("речь не распознана"):
        return ""
    if not re.search(r"[A-Za-zА-Яа-яЁё]{2}", t):          # CJK: no spaces between words
        return t[:30] + "…" if len(t) > 30 else t
    return t[:70].rsplit(" ", 1)[0] + "…" if len(t) > 70 else t


def _cut_queries(row) -> list[str]:
    ru, orig, lat, uk, y = row["title_ru"], row["title_original"], row["title_latin"], row["title_uk"], row["year"] or ""
    qs = []
    if ru:
        qs += [f"{ru} трейлер", f"{ru} тизер", f"{ru} трейлер 2", f"{ru} дублированный трейлер", f"{ru} {y} трейлер"]
    for o in [x for x in (orig, lat) if x and yt_mod.norm(x) != yt_mod.norm(ru)]:
        qs += [f"{o} trailer", f"{o} teaser", f"{o} {y} trailer", f"{o} трейлер"]
    if uk and yt_mod.norm(uk) != yt_mod.norm(ru):
        qs.append(f"{uk} трейлер")
    return list(dict.fromkeys(" ".join(q.split()) for q in qs))


def title_cuts(conn, yt: yt_mod.YouTube, cache: dict, tid: int, log=print) -> dict:
    """The distinct cuts of one film: search → filters → ≤ 3 s groups → per cut the best upload (and one claiming a
    Russian dub) speech-checked; the first Russian one represents the cut."""
    row = conn.execute("SELECT * FROM titles WHERE id = ?", (tid,)).fetchone()
    rd = dict(row)
    cast = set(yt_mod.title_context(conn, row).get("cast_tokens") or [])
    cur = {k: _vid(row[c]) for k, c in (("ru", "trailer_ru_url"), ("alt", "trailer_alt_url"))}
    pool: dict[str, dict] = {}
    for q in _cut_queries(row):
        for v in yt.search(q).get("videos") or []:
            if v.get("length") and 15 <= v["length"] <= 300 and cut_title_ok(rd, v.get("title"), cast):
                pool.setdefault(v["id"], v)
    for vid in cur.values():                     # the trailer set now is always listed
        if vid and vid not in pool:
            w = yt.watch(vid)
            pool[vid] = {"id": vid, "title": w.get("title"), "length": w.get("length"), "views": w.get("views"),
                         "owner_badges": []}
    cuts = []
    for g in group_cuts(list(pool.values())):
        ordered = sorted(g, key=best_upload_key)
        claim = [v for v in ordered if _CLAIMS_RU.search(v.get("title") or "")]
        chosen = None
        for v in list({x["id"]: x for x in ordered[:2] + claim[:1]}.values()):
            w = yt.watch(v["id"])
            if w.get("status") != "OK" or w.get("available_in_ua") is False:
                continue
            r = check_video(yt, v["id"], cache)
            al, _ = yt_mod.audio_language(w.get("audio_tracks") or [], w.get("captions") or [])
            c = {"id": v["id"], "title": w.get("title"), "channel": w.get("author"), "length": w.get("length"),
                 "verdict": r.get("verdict"), "language": r.get("language"), "transcript": r.get("transcript") or "",
                 "method": r.get("method"), "error": r.get("error"), "meta_lang": al}
            if chosen is None or (chosen["verdict"] != "ru" and c["verdict"] == "ru") or \
                    (chosen.get("error") and not c.get("error")):
                chosen = c
            if chosen["verdict"] == "ru":
                break
        if chosen is None:                       # nothing playable in this cut: listed without a language
            v = ordered[0]
            chosen = {"id": v["id"], "title": v.get("title"), "channel": v.get("channel"), "length": v.get("length"),
                      "verdict": None, "language": None, "transcript": "", "error": "не воспроизводится в UA",
                      "meta_lang": None}
        chosen.update(uploads=len(g), upload_ids=[x["id"] for x in g],
                      current=[k for k, vid in cur.items() if vid and vid in {x["id"] for x in g}])
        chosen["lang_label"], chosen["quote"] = cut_language(chosen), cut_quote(chosen)
        cuts.append(chosen)
    log(f"  cuts {tid} {row['title_ru']}: {len(pool)} uploads → {len(cuts)} cuts: " +
        "; ".join(f"{c['length']}s {c['lang_label']}" for c in cuts))
    return {"title_id": tid, "title_ru": row["title_ru"], "orig": row["title_original"], "year": row["year"],
            "is_animation": row["is_animation"], "kind": row["kind"], "season": row["season_number"], "cuts": cuts}


def cuts_question(f: dict, max_options: int = 3) -> dict:
    """One owner-survey question: options = up to 3 cuts (Russian first, then the one set now, then the longest) +
    «Ни один»; links_markdown = every cut (length, language, channel, quote, «сейчас стоит») to post before it."""
    cuts = sorted(f["cuts"], key=lambda c: c["length"] or 0)
    for c in cuts:
        c.setdefault("lang_label", cut_language(c))
        c.setdefault("quote", cut_quote(c))
    url = "https://www.youtube.com/watch?v="
    links = []
    for c in cuts:
        now = (" — сейчас стоит" + (" (запасной трейлер)" if c["current"] == ["alt"] else "")) if c["current"] else ""
        links.append(f"[{mmss(c['length'])} — {c['lang_label']}]({url}{c['id']}) — {c.get('channel') or '?'}"
                     f"{'; слышно: «' + c['quote'] + '»' if c['quote'] else ''}"
                     f"{'; таких роликов ' + str(c['uploads']) if c['uploads'] > 1 else ''}{now}")
    pri = sorted(cuts, key=lambda c: (c["verdict"] != "ru", not c["current"], -(c["length"] or 0)))[:max_options]
    opts = []
    for c in sorted(pri, key=lambda c: c["length"] or 0):
        # the language is already known from the speech check — said plainly (owner 2026-10-28: «Что значит станет?»)
        # Whisper reads Ukrainian as Russian at times («Тоди я оптимист», «Netflix українською», Heartstopper 2026-10-28)
        ua_chan = c["verdict"] == "ru" and re.search(r"(?i)україн|украин|\bua\b", c.get("channel") or "")
        desc = (c.get("channel") or "") + (f"; слышно: «{c['quote']}»" if c["quote"] else "") + \
            ("; сейчас стоит" if c["current"] else "") + \
            ("; канал украиноязычный — возможно, речь украинская" if ua_chan else "") + \
            (". Русская речь (проверено по звуку) — в таблицу как русский трейлер." if c["verdict"] == "ru" else
             ". Украинская речь — в таблицу как украинский трейлер." if c["verdict"] == "uk" else
             ". Не русская речь — в таблицу как трейлер на другом языке." if c["verdict"] == "other" else
             ". Язык не определён (речь под музыкой или звук недоступен) — в таблицу как трейлер с неизвестным языком.")
        opts.append({"label": f"{mmss(c['length'])} — {c['lang_label'].split(' (')[0]}"[:60], "description": desc[:200],
                     "video": c["id"], "russian": c["verdict"] == "ru"})
    opts.append({"label": "Ни один", "description": "Ни один не подходит: русского трейлера нет, запасной не меняется."})
    cat = ("мультсериал" if f.get("is_animation") else "сериал") + (f", сезон {f['season']}" if f.get("season") else "") \
        if f.get("kind") == "series" else "мультфильм" if f.get("is_animation") else "фильм"
    name = f"«{f['title_ru']}»" + (f" / {f['orig']}" if f.get("orig") and f["orig"] != f["title_ru"] else "")
    found = "найдена одна версия" if len(cuts) == 1 else f"{len(cuts)} разных версий"
    extra = f" В вариантах {len(pri)} из {len(cuts)}, остальные — в списке." if len(cuts) > len(pri) else ""
    return {"key": f"trailer:{f['title_id']}", "title_id": f["title_id"], "header": "Трейлер", "multiSelect": False,
            "question": (f"{cat}, {f['year']}, {name}: на YouTube {found} трейлера (ролики одной длительности ±3 с с "
                         f"разных каналов — одна версия). Список с длительностью, языком и ссылками — перед вопросом."
                         f"{extra} Какую поставить?"),
            "links_markdown": links, "options": opts,
            "apply": "вариант с russian=true → titles.trailer_ru_url = видео, trailer_ru_source='owner', "
                     "trailer_ru_check='owner'; иначе → trailer_ru_url = NULL, trailer_ru_check='owner_alt', "
                     "trailer_alt_url = видео, trailer_alt_source='owner' (trailer_alt_kind по языку); "
                     "«Ни один» → без изменений"}


def _vid(url: str | None) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None


def watched_ids(conn, ids: list[int]) -> set[int]:
    """Titles the owner has seen (owner 2026-10-25: «Не нужны трейлеры для фильмов, которые я видел»)."""
    if not ids:
        return set()
    q = ",".join(str(int(i)) for i in ids)
    seen = {r[0] for r in conn.execute(f"SELECT id FROM titles WHERE id IN ({q})" + yt_mod.NOT_WATCHED)}
    return set(ids) - seen


def run_cuts(ids: list[int], write_questions: bool = True, log=print) -> dict:
    conn = db.connect()
    yt = yt_mod.YouTube(log=log)
    cache = _load_checks()
    seen = watched_ids(conn, ids)
    for t in sorted(seen):
        log(f"  cuts {t}: watched by the owner — no trailer, skipped")
    store = json.loads(CUTS_FILE.read_text(encoding="utf-8")) if CUTS_FILE.exists() else {}
    done = []
    try:
        for tid in [i for i in ids if i not in seen]:
            store[str(tid)] = title_cuts(conn, yt, cache, tid, log=log)
            done.append(tid)
            CUTS_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        shutil.rmtree(AUDIO_DIR, ignore_errors=True)
    qs = [cuts_question(store[str(t)]) for t in done if store[str(t)]["cuts"]]
    for i, q in enumerate(qs, 1):
        q["question"] = f"{i}/{len(qs)} · " + q["question"]
    if write_questions:
        QUESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUESTIONS_FILE.write_text(json.dumps({
            "kind": "trailers_left", "created": datetime.now().date().isoformat(), "version": 2,
            "note": ("Трейлеры, которые не решились автоматически (films.sources.trailer_lang cuts). По каждому фильму — "
                     "реально разные версии трейлера: ролики с длительностью ±3 с — одна версия (лучшая загрузка), у "
                     "каждой длительность, язык по проверке звука (локальный Whisper) и цитата. links_markdown — "
                     "опубликовать сообщением перед вопросом (в вопросе ссылки не кликабельны). 4 вопроса за вызов."),
            "questions": qs}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"titles": len(ids), "watched_skipped": sorted(seen), "questions": len(qs)}


def apply_answer(conn, tid: int, video: str | None) -> str:
    """The owner's pick from a trailers_left question (cuts_question «apply»); video None = «Ни один»."""
    q = next((q for q in json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))["questions"] if q["title_id"] == tid), None)
    if q is None:
        raise SystemExit(f"no question for title {tid}")
    if video is None:
        return "Ни один: без изменений"
    opt = next((o for o in q["options"] if o.get("video") == video), None)
    if opt is None:
        raise SystemExit(f"{video} is not an option of title {tid}")
    url = f"https://www.youtube.com/watch?v={video}"
    with conn:
        if opt["russian"]:
            conn.execute("""UPDATE titles SET trailer_ru_url = ?, trailer_ru_source = 'owner', trailer_ru_check = 'owner',
                            trailer_ru_checked_at = datetime('now') WHERE id = ?""", (url, tid))
            return "русский трейлер"
        lang = next((c["verdict"] for c in (json.loads(CUTS_FILE.read_text(encoding="utf-8")).get(str(tid)) or {})
                     .get("cuts", []) if c["id"] == video), None) if CUTS_FILE.exists() else None
        kind = "uk" if lang == "uk" else "original" if lang == "other" else "any"   # unknown speech → «другой»
        conn.execute("""UPDATE titles SET trailer_ru_url = NULL, trailer_ru_check = 'owner_alt',
                        trailer_ru_checked_at = datetime('now'), trailer_alt_url = ?, trailer_alt_kind = ?,
                        trailer_alt_source = 'owner' WHERE id = ?""", (url, kind, tid))
        return f"трейлер на другом языке ({kind})"


def main(argv=None):
    p = argparse.ArgumentParser(prog="films.sources.trailer_lang")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--unconfirmed", action="store_true")
    c.add_argument("--ids")
    c.add_argument("--limit", type=int)
    c.add_argument("--dry-run", action="store_true")
    sub.add_parser("fallbacks")
    o = sub.add_parser("originals")
    o.add_argument("--recheck", action="store_true", help="judge the earlier finds again (search cache)")
    v = sub.add_parser("video")
    v.add_argument("id")
    cu = sub.add_parser("cuts")
    cu.add_argument("ids", nargs="+", help="title ids (space or comma separated)")
    cu.add_argument("--no-questions", action="store_true", help="only data/cache/trailer_cuts.json")
    an = sub.add_parser("answer", help="the owner's pick: title id and video id (or «none»)")
    an.add_argument("pairs", nargs="+", help="330=LPmFV3HaPG0 889=none …")
    a = p.parse_args(argv)
    if a.cmd == "answer":
        conn = db.connect()
        for pair in a.pairs:
            tid, video = pair.split("=", 1)
            print(tid, apply_answer(conn, int(tid), None if video == "none" else video))
        return
    if a.cmd == "cuts":
        ids = [int(x) for s in a.ids for x in s.split(",") if x.strip()]
        try:
            st = run_cuts(ids, not a.no_questions, log=lambda m: print(m, file=sys.stderr, flush=True))
        except yt_mod.Blocked as e:
            print(f"STOPPED: YouTube blocked: {e}", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    if a.cmd == "originals":
        print(json.dumps(search_originals(db.connect(), a.recheck, log=lambda m: print(m, file=sys.stderr, flush=True)),
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
