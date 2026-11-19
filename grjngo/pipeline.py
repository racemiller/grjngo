"""Decide which upload of each movie to keep, download it, and keep the library in sync.

Keeper ranking (best first):
  1. colorized policy (B&W original preferred by default)
  2. real resolution, capped at max_height (a 4K upscale and a 1080p upload tie at 1080)
  3. completeness: runtime closest to the full film (2-minute buckets so seconds don't decide)
  4. VP9 before H.264 before anything else
  5. oldest upload (stable choice)
An existing library copy is only replaced when 1-3 say the new upload is genuinely better.
"""
from __future__ import annotations

import json
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import library, ytdlp
from .config import Config
from .tmdb import TMDB

UNKNOWN = object()


@dataclass
class Choice:
    key: tuple[int, str]          # (tmdb_id, edition)
    video: dict
    probe: dict | None
    action: str                   # "new" | "upgrade"
    current: dict | None = None


def edition_for(cfg: Config, colorized: int) -> str:
    return "Colorized" if cfg.colorized_policy == "keep_both" and colorized else ""


def _codec_rank(vcodec: str | None) -> int:
    v = (vcodec or "").lower()
    return 0 if v.startswith("vp9") or v.startswith("vp09") else 1 if v.startswith("avc") else 2


def quality(cfg: Config, colorized: int, height: int | None, duration: int | None, runtime: int | None) -> tuple:
    """Lower is better. Used for both ranking and upgrade decisions."""
    if cfg.colorized_policy == "prefer_original":
        color = int(bool(colorized))
    elif cfg.colorized_policy == "prefer_colorized":
        color = int(not colorized)
    else:
        color = 0
    h = min(height or 0, cfg.max_height)
    dur = duration or 0
    if runtime:
        dur = min(dur, int(runtime * 60 * 1.05))  # padding/intros beyond the real runtime don't count
    return (color, -h, -(dur // 120))


def load_groups(cfg: Config, conn) -> dict[tuple[int, str], list[dict]]:
    rows = conn.execute("""
        SELECT v.*, p.height AS p_height, p.vcodec AS p_vcodec, p.unavailable AS p_unavail,
               p.video_id AS p_id,
               (SELECT COUNT(*) FROM failures f WHERE f.video_id = v.video_id) AS fails
        FROM videos v LEFT JOIN probes p USING(video_id)
        WHERE v.match_status IN ('matched','manual') AND v.tmdb_id IS NOT NULL""").fetchall()
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in rows:
        r = dict(r)
        if r["p_unavail"] or r["fails"] >= cfg.max_failures:
            continue
        groups[(r["tmdb_id"], edition_for(cfg, r["colorized"]))].append(r)
    return groups


def runtime_of(conn, tmdb_id: int) -> int | None:
    row = conn.execute("SELECT json FROM tmdb_movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()
    return (json.loads(row[0]).get("runtime") or None) if row else None


def rank(cfg: Config, conn, key, vids: list[dict]) -> list[dict]:
    runtime = runtime_of(conn, key[0])
    return sorted(vids, key=lambda v: (*quality(cfg, v["colorized"], v["p_height"], v["duration"], runtime),
                                       _codec_rank(v["p_vcodec"]), v["first_seen"] or "", v["video_id"]))


def needs_probe(conn, key, vids: list[dict]) -> list[dict]:
    """Probe only where a choice exists: several uploads, or a new upload of a movie we already have."""
    lib = conn.execute("SELECT video_id FROM library WHERE tmdb_id=? AND edition=?", key).fetchone()
    if len(vids) < 2 and not (lib and vids and vids[0]["video_id"] != lib["video_id"]):
        return []
    return [v for v in vids if v["p_id"] is None]


def run_probes(cfg: Config, conn, log=print) -> int:
    budget = cfg.max_probes_per_run
    todo = [v for key, vids in load_groups(cfg, conn).items() for v in needs_probe(conn, key, vids)]
    if not todo:
        return 0
    log(f"Probing {min(len(todo), budget)} of {len(todo)} uploads that compete for the same movie...")
    done = 0
    for v in todo[:budget]:
        try:
            info = ytdlp.probe(cfg, v["video_id"])
            conn.execute("""INSERT OR REPLACE INTO probes(video_id,height,width,vcodec,acodec,fps,filesize)
                            VALUES (?,?,?,?,?,?,?)""",
                         (v["video_id"], info["height"], info["width"], info["vcodec"], info["acodec"],
                          info["fps"], info["filesize"]))
        except ytdlp.Gone as e:
            conn.execute("INSERT OR REPLACE INTO probes(video_id,unavailable,error) VALUES (?,1,?)",
                         (v["video_id"], str(e)[:500]))
        except RuntimeError as e:
            conn.execute("INSERT INTO failures(video_id,error) VALUES (?,?)", (v["video_id"], str(e)[:500]))
        conn.commit()
        done += 1
        if done % 20 == 0:
            log(f"  probed {done}")
        time.sleep(ytdlp.pause(cfg))
    return done


def plan(cfg: Config, conn) -> tuple[list[Choice], int]:
    choices, waiting = [], 0
    for key, vids in load_groups(cfg, conn).items():
        if needs_probe(conn, key, vids):
            waiting += 1          # decide once every contender has been probed
            continue
        best = rank(cfg, conn, key, vids)[0]
        cur = conn.execute("SELECT * FROM library WHERE tmdb_id=? AND edition=?", key).fetchone()
        if cur is None:
            choices.append(Choice(key, best, None, "new"))
        elif cur["video_id"] != best["video_id"]:
            runtime = runtime_of(conn, key[0])
            new_q = quality(cfg, best["colorized"], best["p_height"], best["duration"], runtime)
            cur_q = quality(cfg, cur["colorized"], cur["height"], cur["duration"], runtime)
            if new_q < cur_q:
                choices.append(Choice(key, best, None, "upgrade", dict(cur)))
    choices.sort(key=lambda c: (c.action != "new", c.video["first_seen"] or ""))
    return choices, waiting


def _taken_by_other(conn, folder: Path, tmdb_id: int) -> bool:
    for r in conn.execute("SELECT tmdb_id, path FROM library WHERE tmdb_id<>?", (tmdb_id,)):
        if Path(r["path"]).parent == folder:
            return True
    return False


def target_folder(cfg: Config, conn, movie: dict, edition: str) -> Path:
    cur = conn.execute("SELECT path FROM library WHERE tmdb_id=? AND edition=?", (movie["id"], edition)).fetchone()
    if cur:
        return Path(cur["path"]).parent
    folder = library.folder_for(cfg.library_dir, movie, edition, False)
    if _taken_by_other(conn, folder, movie["id"]):
        folder = library.folder_for(cfg.library_dir, movie, edition, True)
    return folder


def run_downloads(cfg: Config, conn, tmdb: TMDB, choices: list[Choice], log=print) -> dict[str, int]:
    stats = {"new": 0, "upgrade": 0, "gone": 0, "failed": 0}
    for c in choices[: cfg.max_downloads_per_run]:
        v = c.video
        movie = tmdb.movie(c.key[0])
        label = f"{library.base_name(movie, c.key[1])}  <- {v['video_id']}"
        log(f"{'Upgrading' if c.action == 'upgrade' else 'Downloading'}: {label}")
        try:
            info = ytdlp.download(cfg, v["video_id"])
        except ytdlp.Gone as e:
            conn.execute("INSERT OR REPLACE INTO probes(video_id,unavailable,error) VALUES (?,1,?)",
                         (v["video_id"], str(e)[:500]))
            conn.commit()
            stats["gone"] += 1
            log(f"  gone: {e}")
            continue
        except (RuntimeError, IndexError) as e:
            conn.execute("INSERT INTO failures(video_id,error) VALUES (?,?)", (v["video_id"], str(e)[:500]))
            conn.commit()
            stats["failed"] += 1
            log(f"  failed: {e}")
            continue
        folder = target_folder(cfg, conn, movie, c.key[1])
        dest = library.install(Path(info["filepath"]), folder, movie, v, bool(v["colorized"]), c.key[1])
        conn.execute("""INSERT OR REPLACE INTO probes(video_id,height,width,vcodec,acodec,fps)
                        VALUES (?,?,?,?,?,?)""",
                     (v["video_id"], info.get("height"), info.get("width"), info.get("vcodec"),
                      info.get("acodec"), info.get("fps")))
        conn.execute("""INSERT OR REPLACE INTO library(tmdb_id,edition,video_id,path,height,vcodec,duration,colorized)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (c.key[0], c.key[1], v["video_id"], str(dest), info.get("height"), info.get("vcodec"),
                      int(info.get("duration") or v["duration"] or 0), v["colorized"]))
        conn.commit()
        stats[c.action] += 1
        log(f"  -> {dest} ({info.get('height')}p {info.get('vcodec')})")
    return stats


def reconcile(cfg: Config, conn, tmdb: TMDB, log=print) -> int:
    """After a manual fix, a file on the NAS may belong to a different movie (or none). Move or remove it."""
    fixed = 0
    rows = conn.execute("""
        SELECT l.*, v.tmdb_id AS now_tmdb, v.match_status, v.colorized AS v_col
        FROM library l LEFT JOIN videos v USING(video_id)""").fetchall()
    for r in rows:
        now_edition = edition_for(cfg, r["v_col"] or 0)
        still_ok = r["match_status"] in ("matched", "manual") and r["now_tmdb"] == r["tmdb_id"] \
            and now_edition == r["edition"]
        if still_ok:
            continue
        old_file, old_folder = Path(r["path"]), Path(r["path"]).parent
        dest = None
        conn.execute("DELETE FROM library WHERE tmdb_id=? AND edition=?", (r["tmdb_id"], r["edition"]))
        target_free = r["match_status"] in ("matched", "manual") and r["now_tmdb"] and not conn.execute(
            "SELECT 1 FROM library WHERE tmdb_id=? AND edition=?", (r["now_tmdb"], now_edition)).fetchone()
        if target_free and old_file.exists():
            movie = tmdb.movie(r["now_tmdb"])
            video = dict(conn.execute("SELECT * FROM videos WHERE video_id=?", (r["video_id"],)).fetchone())
            cfg.staging_dir.mkdir(parents=True, exist_ok=True)
            staged = cfg.staging_dir / f"{r['video_id']}{old_file.suffix}"
            shutil.move(str(old_file), staged)
            dest = library.install(staged, target_folder(cfg, conn, movie, now_edition), movie, video,
                                   bool(video["colorized"]), now_edition)
            conn.execute("""INSERT OR REPLACE INTO library(tmdb_id,edition,video_id,path,height,vcodec,duration,colorized)
                            VALUES (?,?,?,?,?,?,?,?)""",
                         (r["now_tmdb"], now_edition, r["video_id"], str(dest), r["height"], r["vcodec"],
                          r["duration"], r["colorized"]))
            log(f"Re-filed {old_folder.name} -> {dest.parent.name}")
        else:
            log(f"Removed {old_folder.name} (upload no longer maps to it)")
        if old_folder.exists() and old_folder != cfg.library_dir and (dest is None or dest.parent != old_folder):
            shutil.rmtree(old_folder, ignore_errors=True)
        conn.commit()
        fixed += 1
    return fixed
