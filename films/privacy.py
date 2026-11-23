"""Pre-push privacy check for the public repository.

Fails when any file git would publish (tracked or not ignored) or the database contains:
- a value from config/private.toml (accounts, private list ids, machine ids, names),
- an e-mail address other than the allow-list,
- something shaped like a secret (TMDB v3 key next to a key-ish word, JWT, Google OAuth client secret, ...).
Run: python -X utf8 -m films privacy-check   (also wired as .git/hooks/pre-push)

Only what changed (R330, owner 2026-11-14: «Зачем нам проверять все данные если достаточно только изменённые?»):
by default the lines added since the last pushed commit (origin/main) — committed and not yet committed — plus
untracked files are checked; the full pass over every file and the whole database (~15 min, GitHub dropped the push
connection while it ran) is `--full`. It runs by itself when there is no pushed commit to compare with or when
config/private.toml got a value the last full pass did not search for.
"""
import hashlib
import json
import lzma
import re
import sqlite3
import subprocess
import sys

from . import db
from .backup import CACHE_TABLES

# git@github.com — the SSH user of every GitHub remote (git@github.com:owner/repo), not anybody's address (2026-11-24)
ALLOWED_EMAILS = {"noreply@anthropic.com", "google-admob-ads-sdk@googlegroups.com", "git@github.com"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
SECRET_RES = [
    ("tmdb-like key", re.compile(r"(?i)(api[_-]?key|tmdb[_a-z]*)\s*[=:]\s*['\"]?[0-9a-f]{32}\b")),
    ("JWT", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("Google OAuth secret", re.compile(r"GOCSPX-[\w-]{10,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Plex token", re.compile(r"(?i)X-Plex-Token=[A-Za-z0-9_-]{15,}")),
]
USER_PATH_RE = re.compile(r"(?i)[A-Z]:[\\/]+Users[\\/]+(?!<user>|Public|Default)([\w.-]+)")
SKIP_SUFFIXES = {".db", ".gz", ".pickle", ".png", ".jpg"}
STATE = db.ROOT / "data" / "cache" / "locks" / "privacy_full.json"   # what the last clean full pass searched for


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
            # short numbers (postcode) and dates (birth date) collide with ids / release dates and reveal nothing alone;
            # the full address, phone and name are still searched
            if isinstance(v, str) and len(v) >= 5 and not re.fullmatch(r"\d{1,6}|\d{4}-\d\d-\d\d", v):
                vals.append(v)
                local = v.split("@")[0]
                if "@" in v and len(local) >= 5:
                    vals.append(local)
    return sorted(set(vals), key=len, reverse=True)


def _skip(rel: str) -> bool:
    return (db.ROOT / rel).suffix in SKIP_SUFFIXES or rel.startswith("tests/fixtures/")


def _read(path) -> str:
    """Text of a published file; a frozen archive part (.xz, R331) is checked inside, not skipped as binary."""
    if path.suffix == ".xz":
        with lzma.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    return path.read_text(encoding="utf-8")


def _scan_text(rel: str, text: str, private_vals: list[str]) -> list[str]:
    problems = []
    low = text.lower()   # case-insensitive: «Frist…» and «frist…» are the same person (review 2026-10-24)
    for v in private_vals:
        if v.lower() in low:
            problems.append(f"{rel}: private value «{v[:4]}…» (config/private.toml)")
    for m in ([] if rel.startswith("tests/") else USER_PATH_RE.findall(text)):   # a real Windows user name in a path (tests use fake ones)
        problems.append(f"{rel}: Windows user name in a path «…/Users/{m[:3]}…» (backup._redact hides it)")
    for m in EMAIL_RE.findall(text):
        if m not in ALLOWED_EMAILS and not m.endswith(("example.com", "gserviceaccount.com")):
            problems.append(f"{rel}: e-mail {m}")
    for name, rx in SECRET_RES:
        if rx.search(text):
            problems.append(f"{rel}: looks like a {name}")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=db.ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout


def _base() -> str | None:
    """The last pushed commit: everything up to it passed the check before its push. A history squashed into one root
    commit (R311, R331) has no common ancestor with it — then its tree itself is the base (what is on GitHub now)."""
    for args in (("merge-base", "HEAD", "origin/main"), ("rev-parse", "--verify", "-q", "origin/main")):
        try:
            if out := _git(*args).strip():
                return out
        except subprocess.CalledProcessError:
            continue
    return None


def _added_lines(*diff_args: str) -> dict[str, list[str]]:
    """{file: [added lines]} of a `git diff` (text files only; binary ones are listed without lines)."""
    out: dict[str, list[str]] = {}
    cur, header = None, False
    for line in _git("-c", "core.quotepath=off", "diff", "--no-color", "--no-ext-diff", "--no-renames", "-U0",
                     *diff_args).splitlines():
        if line.startswith("diff --git "):
            cur, header = None, True
        elif header and line.startswith("+++ "):
            name = line[4:]
            cur = None if name == "/dev/null" else (name[2:] if name.startswith("b/") else name)
            if cur is not None and _skip(cur):
                cur = None
            if cur is not None:
                out.setdefault(cur, [])
        elif header and line.startswith("@@"):
            header = False
        elif not header and cur is not None and line.startswith("+"):
            out[cur].append(line[1:])
    return out


def _values_hash(vals: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(vals)).encode()).hexdigest()


def full_needed(private_vals: list[str] | None = None) -> str | None:
    """Why the changed-only check is not enough, or None."""
    vals = _private_values() if private_vals is None else private_vals
    if _base() is None:
        return "no pushed commit (origin/main) to compare with"
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "no clean full pass recorded yet"
    if st.get("values") != _values_hash(vals):
        return "config/private.toml changed since the last full pass"
    return None


def check_changed(base: str | None = None) -> list[str]:
    """Only what is new since the last push: added lines of committed and uncommitted changes + untracked files."""
    private_vals = _private_values()
    base = base or _base()
    problems = []
    added = _added_lines(base, "HEAD")
    for rel, lines in _added_lines("HEAD").items():   # not committed yet
        added.setdefault(rel, []).extend(lines)
    for rel, lines in added.items():
        problems += _scan_text(rel, "\n".join(lines), private_vals)
    # whole files: untracked ones, and changed binary ones (a new frozen .xz part has no text lines in a diff)
    whole = set(_git("ls-files", "-o", "--exclude-standard").splitlines())
    for args in ((base, "HEAD"), ("HEAD",)):
        whole |= {f for f in _git("-c", "core.quotepath=off", "diff", "--name-only", "--no-renames", "--diff-filter=d",
                                  *args).splitlines() if f.endswith(".xz")}
    for rel in sorted(whole):
        path = db.ROOT / rel
        if not rel or _skip(rel) or not path.is_file():
            continue
        try:
            problems += _scan_text(rel, _read(path), private_vals)
        except (UnicodeDecodeError, lzma.LZMAError):
            problems.append(f"{rel}: cannot be read to check it")
    return problems


def check() -> list[str]:
    """Full pass (`--full`): every publishable file and the whole database."""
    problems = []
    private_vals = _private_values()
    for rel in _publishable_files():
        path = db.ROOT / rel
        if _skip(rel) or not path.is_file():
            continue
        try:
            text = _read(path)
        except UnicodeDecodeError:
            continue
        except lzma.LZMAError:
            problems.append(f"{rel}: cannot be read to check it")
            continue
        problems += _scan_text(rel, text, private_vals)
    # data/films.db itself is not in git (data/*.db is ignored); its text copy db/ is checked above.
    # The scan of the live database stays in the full pass only.
    if db.DB_PATH.exists():
        conn = db.register_functions(sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True))
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for v in private_vals:
            for t in tables:
                if t in CACHE_TABLES:   # third-party caches: not published (films/backup.py), db/ files are checked above
                    continue
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                for col in cols:
                    # releases.parsed is zlib-compressed (R308): searched in its JSON text
                    expr = f"parsed_json({col})" if (t, col) in db.COMPRESSED_COLUMNS else f"CAST({col} AS TEXT)"
                    n = conn.execute(f"SELECT count(*) FROM {t} WHERE instr({expr}, ?) > 0", (v,)).fetchone()[0]
                    if n:
                        problems.append(f"data/films.db {t}.{col}: private value «{v[:4]}…» in {n} rows")
    if not problems:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"values": _values_hash(private_vals)}), encoding="utf-8")
    return problems


def main(full: bool = False) -> int:
    why = "--full" if full else full_needed()
    if why:
        print(f"privacy check: full pass ({why})", flush=True)
        problems = check()
    else:
        print("privacy check: only what changed since origin/main (R330; full pass: --full)", flush=True)
        problems = check_changed()
    for p in problems:
        print("PRIVACY:", p)
    print("privacy check:", "FAILED" if problems else "ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main("--full" in sys.argv[1:]))
