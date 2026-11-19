from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from .config import Config, load_config
from .db import connect
from .titles import clean_title

YT_ID = re.compile(r"(?:youtu\.be/|v=)([\w-]{11})|^([\w-]{11})$")


# ---------------------------------------------------------------- ingest
def upsert_videos(conn, rows: list[tuple[str, str, int | None]]) -> tuple[int, int]:
    new = seen = 0
    for vid, title, dur in rows:
        p = clean_title(title, dur)
        cur = conn.execute("SELECT title FROM videos WHERE video_id=?", (vid,)).fetchone()
        if cur is None:
            new += 1
            conn.execute(
                """INSERT INTO videos(video_id,title,duration,clean_title,alt_titles,year_hint,colorized,
                                      claimed_res,is_extra,extra_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (vid, title, dur, p.title, json.dumps(p.alt_titles), p.year_hint, int(p.colorized),
                 p.claimed_res, int(p.is_extra), p.extra_reason),
            )
        else:
            seen += 1
            # Channel sometimes retitles uploads; re-parse if so, and send it back through matching.
            if cur["title"] != title:
                conn.execute(
                    """UPDATE videos SET title=?, clean_title=?, alt_titles=?, year_hint=?, colorized=?,
                       claimed_res=?, is_extra=?, extra_reason=?,
                       match_status=CASE WHEN match_status IN ('manual','skipped') THEN match_status ELSE 'pending' END
                       WHERE video_id=?""",
                    (title, p.title, json.dumps(p.alt_titles), p.year_hint, int(p.colorized), p.claimed_res,
                     int(p.is_extra), p.extra_reason, vid),
                )
            conn.execute("UPDATE videos SET last_seen=datetime('now'), duration=COALESCE(?,duration) WHERE video_id=?",
                         (dur, vid))
    conn.commit()
    return new, seen


def parse_tsv_lines(lines) -> list[tuple[str, str, int | None]]:
    out = []
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 2 or not parts[0].strip():
            continue
        dur = parts[2].strip() if len(parts) > 2 else ""
        out.append((parts[0].strip(), parts[1].strip(),
                    int(float(dur)) if dur and dur not in ("NA", "None") else None))
    return out


def cmd_import_tsv(cfg: Config, args) -> None:
    conn = connect(cfg.db_path)
    rows = parse_tsv_lines(Path(args.file).read_text(encoding="utf-8").splitlines())
    new, seen = upsert_videos(conn, rows)
    print(f"Imported {len(rows)} rows: {new} new, {seen} already known")


def cmd_scan(cfg: Config, args) -> None:
    cmd = ["yt-dlp", "--flat-playlist", "--ignore-errors", *cfg.ytdlp_args,
           "--print", "%(id)s\t%(title)s\t%(duration)s", cfg.channel_url]
    print("Running:", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    rows = parse_tsv_lines(res.stdout.splitlines())
    if not rows:
        from .ytdlp import Blocked, classify
        if classify(res.stderr or "") is Blocked:
            raise Blocked("channel scan refused")
        sys.stderr.write(res.stderr[-2000:])
        raise SystemExit("Scan returned no videos (blocked? see yt-dlp output above)")
    conn = connect(cfg.db_path)
    new, seen = upsert_videos(conn, rows)
    print(f"Scanned {len(rows)} videos: {new} new, {seen} already known")


# ---------------------------------------------------------------- matching
def cmd_match(cfg: Config, args) -> None:
    from .matcher import run_matching
    from .tmdb import TMDB
    conn = connect(cfg.db_path)
    counts = run_matching(cfg, conn, TMDB(cfg.tmdb_api_key, conn, cfg.tmdb_language),
                          rematch=args.rematch, limit=args.limit)
    print("Done:", counts or "nothing pending")
    print_summary(conn)


def print_summary(conn) -> None:
    rows = conn.execute("SELECT match_status, COUNT(*) n FROM videos GROUP BY 1 ORDER BY 2 DESC").fetchall()
    print("Status: " + ", ".join(f"{r['match_status']}={r['n']}" for r in rows))
    movies = conn.execute(
        "SELECT COUNT(DISTINCT tmdb_id) FROM videos WHERE match_status IN ('matched','manual')").fetchone()[0]
    print(f"Distinct movies identified: {movies}")


# ---------------------------------------------------------------- review loop
REVIEW_COLS = ["url", "yt_title", "minutes", "cleaned_title", "status", "score",
               "suggested_tmdb_id", "suggested_title", "suggested_year", "suggested_runtime", "why",
               "other_candidates", "set_tmdb_id", "skip", "note"]


def cmd_review(cfg: Config, args) -> None:
    conn = connect(cfg.db_path)
    rows = conn.execute("""SELECT * FROM videos WHERE match_status IN ('review','unmatched')
                           ORDER BY clean_title, duration""").fetchall()
    out = Path(args.out or cfg.data_dir / "review.csv")
    with out.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        w = csv.DictWriter(f, fieldnames=REVIEW_COLS)
        w.writeheader()
        for r in rows:
            cands = json.loads(r["candidates"] or "[]")
            best = cands[0] if cands else {}
            others = "; ".join(f"{c['tmdb_id']}: {c['title']} ({c['year']}, {c['runtime']}m) [{c['score']}]"
                               for c in cands[1:])
            w.writerow({
                "url": f"https://youtu.be/{r['video_id']}",
                "yt_title": r["title"], "minutes": round((r["duration"] or 0) / 60),
                "cleaned_title": r["clean_title"], "status": r["match_status"], "score": r["score"],
                "suggested_tmdb_id": best.get("tmdb_id", ""), "suggested_title": best.get("title", ""),
                "suggested_year": best.get("year", ""), "suggested_runtime": best.get("runtime", ""),
                "why": best.get("why", ""), "other_candidates": others,
                "set_tmdb_id": "", "skip": "", "note": "",
            })
    print(f"Wrote {len(rows)} rows to {out}")
    print("Fill in set_tmdb_id (or 'ok' to accept the suggestion, or put x in skip), then run apply-review.")


def _video_id(cell: str) -> str | None:
    m = YT_ID.search(cell.strip())
    return (m.group(1) or m.group(2)) if m else None


def cmd_apply_review(cfg: Config, args) -> None:
    conn = connect(cfg.db_path)
    applied = 0
    with open(args.file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            vid = _video_id(row.get("url") or row.get("video_id") or "")
            if not vid:
                continue
            setv = (row.get("set_tmdb_id") or "").strip().lower()
            skip = (row.get("skip") or "").strip().lower() in ("x", "y", "yes", "1", "true", "skip")
            note = (row.get("note") or "").strip()
            if setv in ("ok", "y", "yes"):
                setv = str(row.get("suggested_tmdb_id") or "").strip()
            if skip:
                set_override(conn, vid, None, True, note)
            elif setv.isdigit():
                set_override(conn, vid, int(setv), False, note)
            else:
                continue
            applied += 1
    conn.commit()
    print(f"Applied {applied} decisions. Run `match` to fold them in.")


def set_override(conn, vid: str, tmdb_id: int | None, skip: bool, note: str = "") -> None:
    conn.execute("INSERT OR REPLACE INTO overrides(video_id,tmdb_id,skip,note) VALUES (?,?,?,?)",
                 (vid, tmdb_id, int(skip), note))


def cmd_override(cfg: Config, args) -> None:
    conn = connect(cfg.db_path)
    vid = _video_id(args.video) or args.video
    if args.tmdb_id.lower() == "skip":
        set_override(conn, vid, None, True, args.note or "")
    else:
        set_override(conn, vid, int(args.tmdb_id), False, args.note or "")
    conn.commit()
    print(f"Override saved for {vid}. Run `match` to apply.")


# ---------------------------------------------------------------- report
def cmd_report(cfg: Config, args) -> None:
    from .pipeline import load_groups, rank
    conn = connect(cfg.db_path)
    print_summary(conn)
    groups = load_groups(cfg, conn)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    lines = [f"{len(groups)} movies, {len(multi)} with multiple uploads "
             f"({sum(len(v) - 1 for v in multi.values())} redundant)\n"]
    for key, vids in sorted(multi.items(), key=lambda kv: kv[1][0]["clean_title"]):
        row = conn.execute("SELECT json FROM tmdb_movies WHERE tmdb_id=?", (key[0],)).fetchone()
        t = json.loads(row[0]) if row else {}
        lines.append(f"{t.get('title', vids[0]['clean_title'])} ({(t.get('release_date') or '????')[:4]})"
                     f"{' [' + key[1] + ']' if key[1] else ''} tmdb={key[0]} runtime={t.get('runtime')}m")
        for i, v in enumerate(rank(cfg, conn, key, vids)):
            res = f"{v['p_height']}p" if v["p_height"] else "unprobed"
            lines.append(f"   {'KEEP' if i == 0 else '    '} {v['video_id']}  {round((v['duration'] or 0) / 60):>3}m  "
                         f"{'color' if v['colorized'] else '     '}  {res:>8}  {v['title']}")
        lines.append("")
    out = Path(args.out or cfg.data_dir / "duplicates.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(lines[0].strip())
    print(f"Full duplicate report: {out}")


# ---------------------------------------------------------------- downloads
def _tmdb(cfg, conn):
    from .tmdb import TMDB
    return TMDB(cfg.tmdb_api_key, conn, cfg.tmdb_language)


def cmd_probe(cfg: Config, args) -> None:
    from .pipeline import run_probes
    conn = connect(cfg.db_path)
    if args.limit:
        cfg.max_probes_per_run = args.limit
    with _blocked_exit():
        print(f"Probed {run_probes(cfg, conn)} uploads")


def cmd_plan(cfg: Config, args) -> None:
    from .library import base_name
    from .pipeline import plan
    conn = connect(cfg.db_path)
    tmdb = _tmdb(cfg, conn)
    choices, waiting = plan(cfg, conn)
    for c in choices[: args.show]:
        m = tmdb.movie(c.key[0])
        extra = f" (replacing {c.current['video_id']} {c.current['height']}p)" if c.current else ""
        print(f"{c.action:8} {base_name(m, c.key[1])}  <- {c.video['video_id']} "
              f"{c.video['p_height'] or '?'}p{extra}")
    n_new = sum(c.action == "new" for c in choices)
    print(f"\n{n_new} to download, {len(choices) - n_new} upgrades, {waiting} movies waiting on probes")


def cmd_download(cfg: Config, args) -> None:
    from .pipeline import plan, run_downloads
    conn = connect(cfg.db_path)
    if args.limit is not None:
        cfg.max_downloads_per_run = args.limit
    choices, _ = plan(cfg, conn)
    with _blocked_exit():
        print(run_downloads(cfg, conn, _tmdb(cfg, conn), choices))


def cmd_pipeline(cfg: Config, args) -> None:
    """The scheduled job: scan -> match -> fix library -> probe -> download."""
    from .matcher import run_matching
    from .pipeline import plan, reconcile, run_downloads, run_probes
    conn = connect(cfg.db_path)
    tmdb = _tmdb(cfg, conn)
    with _blocked_exit():
        if not args.no_scan:
            cmd_scan(cfg, args)
        counts = run_matching(cfg, conn, tmdb)
        if counts:
            print("Matched new uploads:", counts)
        if reconcile(cfg, conn, tmdb):
            print("Library re-filed after manual fixes")
        run_probes(cfg, conn)
        choices, waiting = plan(cfg, conn)
        print(f"Plan: {sum(c.action == 'new' for c in choices)} new, "
              f"{sum(c.action == 'upgrade' for c in choices)} upgrades, {waiting} waiting on probes")
        stats = run_downloads(cfg, conn, tmdb, choices)
        print("Downloads:", stats)
    review = conn.execute("SELECT COUNT(*) FROM videos WHERE match_status IN ('review','unmatched')").fetchone()[0]
    if review:
        print(f"{review} uploads need review: run `grjngo review`")
    cmd_status(cfg, args)


def cmd_status(cfg: Config, args) -> None:
    conn = connect(cfg.db_path)
    print_summary(conn)
    lib = conn.execute("SELECT COUNT(*), SUM(height>=1080), SUM(colorized) FROM library").fetchone()
    print(f"Library: {lib[0]} movies on disk ({lib[1] or 0} at 1080p, {lib[2] or 0} colorized)")
    gone = conn.execute("SELECT COUNT(*) FROM probes WHERE unavailable=1").fetchone()[0]
    stuck = conn.execute("SELECT COUNT(*) FROM (SELECT video_id FROM failures GROUP BY video_id HAVING COUNT(*)>=?)",
                         (cfg.max_failures,)).fetchone()[0]
    if gone or stuck:
        print(f"Unavailable uploads: {gone}; given up after repeated failures: {stuck}")


def cmd_nfo(cfg: Config, args) -> None:
    """Rewrite .nfo + artwork for everything in the library (e.g. after a TMDB data fix)."""
    from .library import write_metadata
    conn = connect(cfg.db_path)
    tmdb = _tmdb(cfg, conn)
    rows = conn.execute("SELECT * FROM library").fetchall()
    for r in rows:
        if args.refresh:
            conn.execute("DELETE FROM tmdb_movies WHERE tmdb_id=?", (r["tmdb_id"],))
        video = dict(conn.execute("SELECT * FROM videos WHERE video_id=?", (r["video_id"],)).fetchone())
        write_metadata(Path(r["path"]).parent, tmdb.movie(r["tmdb_id"]), video, bool(r["colorized"]))
    conn.commit()
    print(f"Rewrote metadata for {len(rows)} movies")


class _blocked_exit:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        from .ytdlp import EXIT_BLOCKED, Blocked
        if exc_type is Blocked:
            print(f"YouTube is blocking this IP: {exc}\nExiting {EXIT_BLOCKED} so run.sh rotates the VPN server.")
            raise SystemExit(EXIT_BLOCKED)
        return False


# ---------------------------------------------------------------- entry
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="grjngo", description="GrjngoWesternMovies -> ErsatzTV pipeline")
    ap.add_argument("-c", "--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import-tsv", help="load a yt-dlp id/title/duration TSV")
    s.add_argument("file")
    sub.add_parser("scan", help="list channel uploads with yt-dlp (no downloads)")
    s = sub.add_parser("match", help="match pending uploads to TMDB")
    s.add_argument("--rematch", action="store_true", help="also re-score review/unmatched/matched rows")
    s.add_argument("--limit", type=int)
    s = sub.add_parser("review", help="export uploads needing a human decision to CSV")
    s.add_argument("--out")
    s = sub.add_parser("apply-review", help="read decisions back from the review CSV")
    s.add_argument("file")
    s = sub.add_parser("override", help="pin one upload to a TMDB id, or 'skip'")
    s.add_argument("video", help="video id or URL")
    s.add_argument("tmdb_id", help="TMDB id or 'skip'")
    s.add_argument("--note")
    s = sub.add_parser("report", help="summary + duplicate groups and which upload is kept")
    s.add_argument("--out")
    s = sub.add_parser("probe", help="check real resolutions of competing uploads")
    s.add_argument("--limit", type=int)
    s = sub.add_parser("plan", help="show what would be downloaded/upgraded (dry run)")
    s.add_argument("--show", type=int, default=50)
    s = sub.add_parser("download", help="download per the plan")
    s.add_argument("--limit", type=int, help="max downloads this run")
    s = sub.add_parser("pipeline", help="scheduled job: scan, match, probe, download")
    s.add_argument("--no-scan", action="store_true")
    sub.add_parser("status", help="one-screen summary")
    s = sub.add_parser("nfo", help="rewrite .nfo/artwork for the whole library")
    s.add_argument("--refresh", action="store_true", help="re-fetch TMDB data first")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    {
        "import-tsv": cmd_import_tsv, "scan": cmd_scan, "match": cmd_match, "review": cmd_review,
        "apply-review": cmd_apply_review, "override": cmd_override, "report": cmd_report,
        "probe": cmd_probe, "plan": cmd_plan, "download": cmd_download, "pipeline": cmd_pipeline,
        "status": cmd_status, "nfo": cmd_nfo,
    }[args.cmd](cfg, args)
