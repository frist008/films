"""Entry point: python -m films <command>."""
import argparse

from . import db


def cmd_init_db(_args):
    conn = db.connect()
    version = db.migrate(conn)
    print(f"schema version {version}: {db.DB_PATH}")


def cmd_status(_args):
    conn = db.connect()
    db.migrate(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        print(f"{t:16} {conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")


def cmd_rutor(args):
    import json
    from .sources import rutor
    conn = db.connect()
    db.migrate(conn)
    if args.action == "collect":
        print(json.dumps(rutor.collect(conn, incremental=args.incremental), ensure_ascii=False, indent=1))
    elif args.action == "parse":
        print(json.dumps(rutor.parse_pages(conn, reparse=args.reparse), ensure_ascii=False))
    else:
        i, n = (int(x) for x in args.shard.split("/"))
        print(f"fetched {rutor.fetch_pages(conn, args.limit, (i, n))} release pages")


def cmd_match(args):
    import json
    from . import match
    conn = db.connect()
    db.migrate(conn)
    print(json.dumps(match.match_releases(conn, retry_issues=args.retry_issues), ensure_ascii=False, indent=1))
    print(json.dumps({"exact_name_year": match.link_exact_names(conn)}, ensure_ascii=False))   # R144
    # owner 2026-10-25: unidentified releases go to the sheet without data; tracker-only rows are identified again first
    from . import tracker_only
    res = tracker_only.run(conn)
    res["create"].pop("made", None)
    print(json.dumps({"tracker_only": res}, ensure_ascii=False, default=str))


def cmd_enrich(args):
    import json
    from . import enrich
    conn = db.connect()
    db.migrate(conn)
    print(json.dumps(enrich.enrich(conn, only_missing=not args.all), ensure_ascii=False, indent=1))


def cmd_ratings(args):
    import json
    from . import ratings
    conn = db.connect()
    db.migrate(conn)
    if args.source in ("imdb", "all"):
        print(json.dumps(ratings.imdb_ratings(conn), ensure_ascii=False))
    if args.source in ("rt", "all"):
        print(json.dumps(ratings.rt_ratings(conn, args.limit, args.recheck_days, recent_days=args.recent_days),
                         ensure_ascii=False))


def cmd_link(args):
    import json
    from . import link
    conn = db.connect()
    db.migrate(conn)
    if args.what in ("jellyfin", "all"):
        from .sources import jellyfin
        print(json.dumps(jellyfin.sync(conn), ensure_ascii=False, indent=1))
        print(json.dumps(link.link_jellyfin(conn), ensure_ascii=False, indent=1))
    if args.what in ("plex", "all"):  # after jellyfin: Plex files are identified through Jellyfin's ids
        print(json.dumps(link.link_plex(conn), ensure_ascii=False, indent=1))
    if args.what in ("stores", "all"):
        print(json.dumps(link.link_stores(conn), ensure_ascii=False, indent=1))


def cmd_crew(_args):
    import json
    from . import crew
    conn = db.connect()
    db.migrate(conn)
    print(json.dumps(crew.refresh(conn), ensure_ascii=False, indent=1))


def cmd_rank(args):
    from . import rank
    conn = db.connect()
    db.migrate(conn)
    rank.preview(conn, args.top)


def cmd_privacy(_args):
    from . import privacy
    raise SystemExit(privacy.main())


def cmd_sheet(args):
    from . import sheet
    if args.action == "login":
        email = sheet.whoami()
        expected = db.private()["google"]["main"]
        print("signed in as the owner" if email == expected else f"WRONG ACCOUNT: signed in as {email}")
    else:
        import json
        from . import gate
        gate.require_db()          # all pages of all sources, no open questions — only then the sheet (owner, 2026-10-26)
        conn = db.connect()
        db.migrate(conn)
        res = sheet.rebuild(conn)
        gate.record_ok()
        print(json.dumps(res, ensure_ascii=False, indent=1))


def cmd_watched(args):
    import json
    from . import watched
    conn = db.connect()
    db.migrate(conn)
    if args.action == "find":
        for r in watched.find(conn, " ".join(args.query)):
            print(json.dumps({k: r[k] for k in ("id", "title_ru", "title_original", "year", "season_number", "imdb_id")},
                             ensure_ascii=False))
    elif args.action == "mark":
        print(json.dumps(watched.mark(conn, int(args.query[0]), dry_run=args.dry_run), ensure_ascii=False, indent=1))
    elif args.action == "from-jellyfin":
        print(json.dumps(watched.sync_from_jellyfin(conn, dry_run=args.dry_run), ensure_ascii=False, indent=1))


def cmd_disks(args):
    import json
    from . import disks
    conn = db.connect()
    db.migrate(conn)
    drives = [d.strip(":").upper() for d in args.drives.split(",")] if args.drives else disks.DRIVES
    if args.action == "scan":
        res = disks.scan(conn, drives)
    elif args.action == "link":
        res = disks.link(conn) | {"report": str(disks.REPORT_PATH)}
        disks.report(conn)
    else:
        r = disks.report(conn)
        res = {k: v for k, v in r.items() if k in ("per_drive", "linked_by", "titles_on_disk",
                                                    "titles_on_disk_not_in_plex_jellyfin")}
        res["unmatched_by_folder"] = [f"{u['files']:4} {u['folder']} — {(u['why'] or '')[:120]}" for u in r["unmatched_by_folder"]]
        res["on_disk_not_in_plex_jellyfin"] = [
            f"{x['title']} ({x['year']}){' S' + str(x['season']) if x['season'] else ''} — {x['files']} file(s) in {x['folders'][0]}"
            for x in r["on_disk_not_in_plex_jellyfin"]]
        res["report"] = str(disks.REPORT_PATH)
    print(json.dumps(res, ensure_ascii=False, indent=1))


def cmd_update(args):
    """«обнови базу» (skill update-db): --start opens the run and snapshots the state, --finish writes events,
    the update date (sync_state update_db, R99) and the owner's report (films/update.py)."""
    import json
    from . import update
    conn = db.connect()
    db.migrate(conn)
    if args.start:
        print(json.dumps(update.start(conn, force=args.force), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(update.finish(conn, args.run_id), ensure_ascii=False, indent=1))


def cmd_sync_status(_args):
    conn = db.connect()
    db.migrate(conn)
    for r in conn.execute("SELECT source, last_run_at, cursor FROM sync_state ORDER BY source"):
        print(f"{r[0]:20} {r[1] or '-':20} {r[2] or ''}")


def main():
    parser = argparse.ArgumentParser(prog="films")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("rutor", help="collect rutor listings / release pages")
    p.add_argument("action", choices=["collect", "pages", "parse"])
    p.add_argument("--incremental", action="store_true", help="stop walking once pages bring nothing new")
    p.add_argument("--limit", type=int, help="pages: max release pages to fetch")
    p.add_argument("--reparse", action="store_true", help="parse: re-parse pages already parsed")
    p.add_argument("--shard", default="0/1", help="pages: worker i/n, run n workers in parallel")
    p.set_defaults(func=cmd_rutor)
    p = sub.add_parser("match", help="link parsed releases to titles (movie / series season)")
    p.add_argument("--retry-issues", action="store_true", help="drop open release issues and try again")
    p.set_defaults(func=cmd_match)
    p = sub.add_parser("enrich", help="fill titles from TMDB (names, genres, credits, seasons, trailers)")
    p.add_argument("--all", action="store_true", help="re-enrich titles already done")
    p.set_defaults(func=cmd_enrich)
    p = sub.add_parser("ratings", help="IMDb (datasets) and Rotten Tomatoes ratings onto titles")
    p.add_argument("source", choices=["imdb", "rt", "all"])
    p.add_argument("--limit", type=int)
    p.add_argument("--recheck-days", type=float, default=30)
    p.add_argument("--recent-days", type=int, default=None,
                   help="rt: update-db step 5 — released in the last N days + titles with releases and no RT score")
    p.set_defaults(func=cmd_ratings)
    p = sub.add_parser("link", help="link Jellyfin, Plex (downloaded/watched) and stores (owned/wishlist) to titles")
    p.add_argument("what", choices=["jellyfin", "plex", "stores", "all"])
    p.set_defaults(func=cmd_link)
    sub.add_parser("crew", help="creator / executive producer … jobs of `crew = true` people (people.toml) -> person_crew"
                   ).set_defaults(func=cmd_crew)
    p = sub.add_parser("rank", help="preview the sorted/filtered main list")
    p.add_argument("--top", type=int, default=40)
    p.set_defaults(func=cmd_rank)
    p = sub.add_parser("sheet", help="Google Sheets output")
    p.add_argument("action", choices=["login", "rebuild"])
    p.set_defaults(func=cmd_sheet)
    p = sub.add_parser("watched", help="«я видел X»: find a title / mark it watched in Plex + DB")
    p.add_argument("action", choices=["find", "mark", "from-jellyfin"])
    p.add_argument("query", nargs="*", help="find: title or IMDb id; mark: title_id; from-jellyfin: nothing")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_watched)
    p = sub.add_parser("disks", help="every video file on the local drives -> titles (R85; downloaded = not watched)")
    p.add_argument("action", choices=["scan", "link", "report"])
    p.add_argument("--drives", help="comma-separated, default C,D,E,F,G")
    p.set_defaults(func=cmd_disks)
    p = sub.add_parser("update", help="«обнови базу»: open (--start) / close (--finish) a run — events, update date, report")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--start", action="store_true", help="open a run and snapshot the state the report compares against")
    g.add_argument("--finish", action="store_true", help="events from the difference, sync_state update_db, report .md")
    p.add_argument("--force", action="store_true", help="--start: close a run left open as abandoned and start anew")
    p.add_argument("--run-id", type=int, help="--finish: this run instead of the open one")
    p.set_defaults(func=cmd_update)
    sub.add_parser("sync-status", help="last sync time and cursor per source").set_defaults(func=cmd_sync_status)
    sub.add_parser("privacy-check", help="fail if anything personal/secret would be published").set_defaults(func=cmd_privacy)
    args = parser.parse_args()
    args.func(args)
