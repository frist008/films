"""Hard guard for every write into the owner's Plex watch history (the owner, 2026-10-23).

Why: every «mark watched» adds a history entry dated *now* and moves «last viewed». Re-marking what the owner had
already watched destroyed his real chronology for ~220 films («Не трогай мои фильмы, где была чёткая история, когда я
что посмотрел. Ты создашь баги хронологии! Это очень важно»). A rule in a skill does not stop that — this does.

`GuardedSession` replaces `requests.Session` inside `films.sources.plex.Plex`, so every project script and every
agent that talks to Plex through the project goes through it:
  - scrobble (library `/:/scrobble`, Discover `/actions/scrobble`): first reads the item's current state; if it is
    already watched (viewCount > 0 or viewState «complete») the request is NOT sent → `PlexWriteBlocked`;
    a season / show key is refused outright (it would stamp «now» on every episode) — mark episodes one by one;
  - unscrobble (library and Discover) and history removal (community GraphQL `removeActivity`): refused unless the
    exact key / activity id was approved by the owner for this process with `owner_approved(keys, reason)`.
Every allowed and every blocked write is appended to data/cache/logs/plex_writes.jsonl.
A Claude Code PreToolUse hook (.claude/hooks/plex-guard.sh) blocks shell commands that would reach Plex past this
module (raw scrobble / unscrobble / removeActivity calls) and edits of this file.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

LOG = Path(__file__).resolve().parents[2] / "data" / "cache" / "logs" / "plex_writes.jsonl"
_APPROVED: dict[str, str] = {}   # key / activity id → the owner's approval (reason)

SCROBBLE = re.compile(r"(/:/scrobble|/actions/scrobble)(\?|$)")
UNSCROBBLE = re.compile(r"(/:/unscrobble|/actions/unscrobble)(\?|$)")


class PlexWriteBlocked(RuntimeError):
    pass


def owner_approved(keys, reason: str) -> None:
    """Allow destructive writes (unscrobble / removeActivity) for exactly these keys, in this process only.
    Call ONLY with a list the owner confirmed in chat, and quote his words in `reason`."""
    for k in keys:
        _APPROVED[str(k)] = reason


def _log(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}, ensure_ascii=False) + "\n")


class GuardedSession(requests.Session):
    def __init__(self, local_url: str, discover_url: str):
        super().__init__()
        self._local, self._discover = local_url, discover_url

    # a raw session for the state checks (never writes)
    def _state(self, url: str, key: str) -> dict:
        if url.startswith(self._local):
            r = super().request("GET", f"{self._local}/library/metadata/{key}", timeout=60)
            r.raise_for_status()
            md = (r.json().get("MediaContainer") or {}).get("Metadata") or [{}]
            return {"type": md[0].get("type"), "viewCount": md[0].get("viewCount", 0)}
        r = super().request("GET", f"{self._discover}/library/metadata/{key}/userState", timeout=60)
        r.raise_for_status()
        st = ((r.json().get("MediaContainer") or {}).get("UserState") or [{}])[0]
        return {"type": st.get("type"), "viewCount": st.get("viewCount", 0), "viewState": st.get("viewState")}

    def request(self, method, url, params=None, data=None, json=None, **kw):  # noqa: A002
        key = str((params or {}).get("key", "")) if isinstance(params, dict) else ""
        if SCROBBLE.search(url):
            st = self._state(url, key)
            if st.get("type") in ("season", "show"):
                _log({"blocked": "scrobble_season_or_show", "url": url, "key": key, "state": st})
                raise PlexWriteBlocked(f"scrobble of a whole {st.get('type')} ({key}) refused — mark episodes one by one")
            if (st.get("viewCount") or 0) > 0 or st.get("viewState") == "complete":
                _log({"blocked": "already_watched", "url": url, "key": key, "state": st})
                raise PlexWriteBlocked(f"{key} is already watched in Plex — re-marking would rewrite the owner's history")
            _log({"allowed": "scrobble", "url": url, "key": key, "state_before": st})
        elif UNSCROBBLE.search(url):
            if key not in _APPROVED:
                _log({"blocked": "unscrobble_not_approved", "url": url, "key": key})
                raise PlexWriteBlocked(f"unscrobble of {key} refused — needs owner_approved([...]) for this key")
            _log({"allowed": "unscrobble", "url": url, "key": key, "reason": _APPROVED[key]})
        elif "community.plex.tv" in url and method.upper() == "POST":
            body = json if json is not None else data
            text = body if isinstance(body, str) else __import__("json").dumps(body or {})
            if "removeActivity" in text or "removeWatchHistory" in text:
                ids = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text)
                if not ids or any(i not in _APPROVED for i in ids):
                    _log({"blocked": "history_removal_not_approved", "ids": ids})
                    raise PlexWriteBlocked("watch-history removal refused — needs owner_approved([...]) for these ids")
                _log({"allowed": "history_removal", "ids": ids, "reason": [_APPROVED[i] for i in ids]})
        return super().request(method, url, params=params, data=data, json=json, **kw)
