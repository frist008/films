"""Pre-push privacy check for the public repository.

Fails when any file git would publish (tracked or not ignored) or the database contains:
- a value from config/private.toml (accounts, private list ids, machine ids, names),
- an e-mail address other than the allow-list,
- something shaped like a secret (TMDB v3 key next to a key-ish word, JWT, Google OAuth client secret, ...).
Run: python -X utf8 -m films privacy-check   (also wired as .git/hooks/pre-push)
"""
import re
import sqlite3
import subprocess
import sys

from . import db

ALLOWED_EMAILS = {"noreply@anthropic.com", "google-admob-ads-sdk@googlegroups.com"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SECRET_RES = [
    ("tmdb-like key", re.compile(r"(?i)(api[_-]?key|tmdb[_a-z]*)\s*[=:]\s*['\"]?[0-9a-f]{32}\b")),
    ("JWT", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("Google OAuth secret", re.compile(r"GOCSPX-[\w-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Plex token", re.compile(r"(?i)X-Plex-Token=[A-Za-z0-9_-]{15,}")),
]
SKIP_SUFFIXES = {".db", ".gz", ".pickle", ".png", ".jpg"}


def _publishable_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=db.ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f]


def _private_values() -> list[str]:
    try:
        cfg = db.private()
    except RuntimeError:
        return []
    vals = []
    for section in cfg.values():
        for v in section.values():
            if isinstance(v, str) and len(v) >= 5:
                vals.append(v)
                local = v.split("@")[0]
                if "@" in v and len(local) >= 5:
                    vals.append(local)
    return sorted(set(vals), key=len, reverse=True)


def check() -> list[str]:
    problems = []
    private_vals = _private_values()
    for rel in _publishable_files():
        path = db.ROOT / rel
        if path.suffix in SKIP_SUFFIXES or not path.is_file() or rel.startswith("tests/fixtures/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for v in private_vals:
            if v in text:
                problems.append(f"{rel}: private value «{v[:4]}…» (config/private.toml)")
        for m in EMAIL_RE.findall(text):
            if m not in ALLOWED_EMAILS and not m.endswith(("example.com", "gserviceaccount.com")):
                problems.append(f"{rel}: e-mail {m}")
        for name, rx in SECRET_RES:
            if rx.search(text):
                problems.append(f"{rel}: looks like a {name}")
    # the database is published too
    if db.DB_PATH.exists():
        conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for v in private_vals:
            for t in tables:
                if t in ("release_raw", "tmdb_raw"):  # compressed public pages
                    continue
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                for col in cols:
                    n = conn.execute(f"SELECT count(*) FROM {t} WHERE instr(CAST({col} AS TEXT), ?) > 0", (v,)).fetchone()[0]
                    if n:
                        problems.append(f"data/films.db {t}.{col}: private value «{v[:4]}…» in {n} rows")
    return problems


def main() -> int:
    problems = check()
    for p in problems:
        print("PRIVACY:", p)
    print("privacy check:", "FAILED" if problems else "ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
