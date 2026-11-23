"""Commit + push as ONE root commit: GitHub keeps no history (owner, 2026-11-14: «Надо будет после пуша сделать так чтобы в
гитхабе был один пуш и история потерта, чтобы мы не занимали там лишнее место в не актуальной структуре»; before that
R306 / R311, 2026-11-03: «сократи все комиты в гите до одного», «старая история тоже не нужна на компе»).

    python -X utf8 -m films.publish -m "сообщение коммита"

Only with the owner's permission (the same data/cache/locks/git_permit.json the hook git-permit.sh writes from his answer
«Да, коммитить и пушить»: younger than 2 h, no commit made since): stage everything → one commit without a parent
(`git commit-tree`) → main and the tag v1.0.0 on it (a tag left on an old commit would keep that history alive) → push
+main +v1.0.0 (pre-push runs `films privacy-check`) → the old commits are dropped locally too (reflog, gc --prune=now).
After the push GitHub still serves the replaced commits by their hash until its own garbage collection runs — see
docs/github-history.md.
"""
import argparse
import json
import subprocess
import sys
import time

from . import db

PERMIT = db.ROOT / "data" / "cache" / "locks" / "git_permit.json"
TTL = 2 * 3600
TAG = "v1.0.0"
CO_AUTHOR = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


def git(*args: str, cwd=None) -> str:
    return subprocess.run(["git", *args], cwd=cwd or db.ROOT, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=True).stdout.strip()


def _head(cwd) -> str | None:
    try:
        return git("rev-parse", "HEAD", cwd=cwd) or None
    except subprocess.CalledProcessError:
        return None


def permitted(cwd=None) -> str | None:
    """None when the owner's «да» is fresh and unused, else why not (the rule of .claude/hooks/git-permit.sh)."""
    try:
        st = json.loads(PERMIT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "нет ответа владельца «Да, коммитить и пушить»"
    if not st.get("granted"):
        return f"владелец ответил «{st.get('answer')}»"
    if time.time() - float(st.get("at") or 0) >= float(st.get("ttl") or TTL):   # a chat permission lives 24 h
        return "разрешение старше 2 часов — спросить заново"
    if st.get("commit_used") or ("head" in st and _head(cwd) != st.get("head")):
        return "по этому разрешению коммит уже сделан — спросить заново"
    return None


def _keep_permit(commit: str) -> None:
    """The permission stays unused: its HEAD mark moves to the unpushed local commit."""
    try:
        st = json.loads(PERMIT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if st.get("granted"):
        st["head"] = commit
        st["push_failed_at"] = time.time()
        PERMIT.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def publish(message: str, cwd=None, push: bool = True, prune: bool = True) -> dict:
    cwd = cwd or db.ROOT
    if why := permitted(cwd):
        raise SystemExit(f"коммит и пуш не сделаны: {why}")
    if CO_AUTHOR not in message:
        message = message.rstrip() + "\n\n" + CO_AUTHOR + "\n"
    git("add", "-A", cwd=cwd)
    tree = git("write-tree", cwd=cwd)
    old = _head(cwd)
    if old and git("rev-parse", f"{old}^{{tree}}", cwd=cwd) == tree and not git("rev-list", "--parents", "-n1", old,
                                                                                    cwd=cwd).count(" "):
        commit = old              # nothing changed and it is already a lone root commit
    else:
        commit = subprocess.run(["git", "commit-tree", tree], input=message, cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", check=True).stdout.strip()
    git("reset", "-q", "--soft", commit, cwd=cwd)
    git("tag", "-f", TAG, commit, cwd=cwd)
    res = {"commit": commit[:7], "replaced": (old or "")[:7], "tag": TAG}
    if push:
        p = subprocess.run(["git", "push", "origin", "+HEAD:refs/heads/main", f"+refs/tags/{TAG}"], cwd=cwd,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        res["push"] = (p.stdout + p.stderr).strip()[-600:]
        if p.returncode:
            # the owner allowed a commit WITH its push: a commit that never reached GitHub does not spend his «да» — the
            # retry after the fix (privacy-check, network) replaces this local commit (2026-11-24: privacy-check stopped
            # the push for «git@github.com», and the retry was refused «по этому разрешению коммит уже сделан»)
            _keep_permit(commit)
            raise SystemExit(f"пуш не прошёл (коммит {commit[:7]} сделан локально, разрешение не потрачено — "
                             f"исправить и запустить снова):\n{res['push']}")
        remote = git("ls-remote", "origin", "refs/heads/main", cwd=cwd).split()[:1]
        res["github_main"] = remote[0][:7] if remote else None
        if cwd == db.ROOT:   # этап на GitHub — до конца следующего этапа хук не спрашивает
            from .stage import clear_closed
            clear_closed()
    if prune:
        git("reflog", "expire", "--expire=now", "--all", cwd=cwd)
        git("gc", "-q", "--prune=now", cwd=cwd)
    res["commits"] = int(git("rev-list", "--count", "--all", cwd=cwd))
    return res


def main() -> int:
    from .locks import lock
    ap = argparse.ArgumentParser(prog="films.publish", description=__doc__.splitlines()[0])
    ap.add_argument("-m", "--message", required=True)
    a = ap.parse_args()
    with lock("git"):                     # the same semaphore as `films.locks run git -- git …`
        print(json.dumps(publish(a.message), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
