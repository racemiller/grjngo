"""Match uploads to TMDB movies.

Every upload is scored on its own (not per title) because the channel reuses
titles for different films, e.g. the 64-min 1952 "Legend of the Lone Ranger"
and the 98-min 1981 one.

Signals, strongest first:
  * title similarity against TMDB title, original title and alternative titles
  * cast: actor names in the YouTube tags vs TMDB credits (settles California 1947 vs 1977)
  * runtime: uploads are often TV cuts (Roy Rogers films trimmed to ~54 min), so
    "shorter" is only a mild penalty while "much longer" is a strong one
  * Western genre, year hints
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

from .config import Config
from .titles import extract_people, match_key
from .tmdb import TMDB

WESTERN_GENRE = 37
TWIN_SECONDS = 4  # re-uploads of the same file are within a couple of seconds of each other


@dataclass
class Candidate:
    tmdb_id: int
    title: str
    year: int | None
    runtime: int | None
    score: float
    why: str
    title_sim: float = 0.0
    western: bool = False
    ratio: float | None = None
    cast_hits: int = 0

    def as_dict(self) -> dict:
        return {k: self.__dict__[k] for k in ("tmdb_id", "title", "year", "runtime", "score", "why")}


def _year(movie: dict) -> int | None:
    d = movie.get("release_date") or ""
    return int(d[:4]) if d[:4].isdigit() else None


def _names(movie: dict) -> list[str]:
    names = [movie.get("title") or "", movie.get("original_title") or ""]
    for t in (movie.get("alternative_titles") or {}).get("titles", []):
        names.append(t.get("title") or "")
    return [n for n in dict.fromkeys(names) if n]


def _credit_names(movie: dict) -> list[str]:
    credits = movie.get("credits") or {}
    names = []
    for c in (credits.get("cast") or [])[:40]:
        names.append(c.get("name") or "")
        names.extend(re.split(r"\s*/\s*", c.get("character") or ""))  # "The Cisco Kid", "Hopalong Cassidy"
    for c in credits.get("crew") or []:
        if c.get("job") == "Director":
            names.append(c.get("name") or "")
    return [match_key(n) for n in names if n]


def cast_hits(people: list[str], movie: dict) -> int:
    credits = _credit_names(movie)
    hits = 0
    for p in people:
        pk = match_key(p)
        if any(fuzz.ratio(pk, c) >= 88 for c in credits):
            hits += 1
    return hits


def title_similarity(queries: list[str], names: list[str]) -> float:
    best = 0.0
    for q in queries:
        qk = match_key(q)
        for n in names:
            nk = match_key(n)
            if not qk or not nk:
                continue
            best = max(best, fuzz.ratio(qk, nk), fuzz.token_sort_ratio(qk, nk) - 3)
    return best


def query_variants(q: str) -> list[str]:
    out = [q]
    for pat, rep in ((r"\bcalled\b", "call"), (r"\bamongst\b", "among"), (r"\s*&\s*", " and "),
                     (r"\bcolour", "color")):
        v = re.sub(pat, rep, q, flags=re.I)
        if v != q:
            out.append(v)
    return out


def score_movie(queries: list[str], duration_s: int | None, year_hint: int | None, movie: dict,
                people: list[str] | None = None) -> Candidate:
    tsim = title_similarity(queries, _names(movie))
    score = tsim * 0.7
    why = [f"title {tsim:.0f}"]

    runtime = movie.get("runtime") or None
    ratio = None
    if runtime and duration_s:
        ratio = (duration_s / 60) / runtime
        if 0.93 <= ratio <= 1.06:        # PAL speed-up makes uploads ~4% short
            score += 15; why.append("runtime ok")
        elif 0.85 <= ratio <= 1.10:
            score += 7; why.append(f"runtime close ({ratio:.2f})")
        elif 0.62 <= ratio < 0.85:       # TV / US-release cut
            score -= 3; why.append(f"shorter cut? ({ratio:.2f})")
        elif 1.10 < ratio <= 1.20:
            score -= 10; why.append(f"runtime long ({ratio:.2f})")
        else:
            score -= 30; why.append(f"runtime wrong ({ratio:.2f})")

    year = _year(movie)
    if year_hint and year:
        if abs(year - year_hint) <= 1:
            score += 10; why.append("year ok")
        else:
            score -= 20; why.append(f"year {year}≠{year_hint}")

    hits = cast_hits(people or [], movie)
    if hits:
        score += 12 + 6 * min(hits - 1, 1); why.append(f"cast x{hits}")

    genres = {g.get("id") for g in movie.get("genres", [])}
    western = WESTERN_GENRE in genres
    if western:
        score += 8; why.append("western")
    elif genres:
        score -= 5; why.append("not western")

    return Candidate(movie["id"], movie.get("title", ""), year, runtime,
                     round(max(0.0, min(100.0, score)), 1), ", ".join(why),
                     title_sim=tsim, western=western, ratio=ratio, cast_hits=hits)


def find_candidates(tmdb: TMDB, queries: list[str], year_hint: int | None, people: list[str],
                    max_details: int = 6) -> list[dict]:
    seen: dict[int, dict] = {}
    for q in queries:
        for v in query_variants(q):
            searches = [tmdb.search(v, year_hint)] if year_hint else []
            searches.append(tmdb.search(v))
            for results in searches:
                for r in results:
                    seen.setdefault(r["id"], r)

    def quick(r):
        return title_similarity(queries, [r.get("title", ""), r.get("original_title", "")])

    ranked = sorted(seen.values(), key=quick, reverse=True)
    picked = ranked[:max_details]

    # Fallback: nothing title-like found, but we know who's in it -> look through their westerns.
    if people and (not picked or quick(picked[0]) < 70):
        ids = [i for i in (tmdb.person_id(p) for p in people[:3]) if i]
        pool: list[dict] = []
        if len(ids) >= 2:
            pool = tmdb.movies_with_cast(ids[:2])
        if not pool and ids:
            pool = tmdb.movies_with_cast(ids[:1])
        extra = sorted((r for r in pool if r["id"] not in {p["id"] for p in picked}), key=quick, reverse=True)
        picked += extra[:6]
    return [m for m in (tmdb.movie(r["id"]) for r in picked) if m]


def decide(cfg: Config, cands: list[Candidate]) -> tuple[str, int | None, float]:
    if not cands:
        return "unmatched", None, 0.0
    best = cands[0]
    second = cands[1].score if len(cands) > 1 else 0.0
    if best.score >= cfg.accept_score and best.score - second >= cfg.accept_margin:
        return "matched", best.tmdb_id, best.score
    # Only one film on TMDB carries this title and it's a western: a shorter upload is a TV cut,
    # not a different movie.
    rivals = [c for c in cands[1:] if c.title_sim >= 85]
    if (best.title_sim >= 95 and best.western and not rivals
            and (best.ratio is None or 0.6 <= best.ratio <= 1.10)):
        return "matched", best.tmdb_id, best.score
    # Same title, but cast names pick exactly one of them.
    if best.title_sim >= 90 and best.cast_hits and all(c.cast_hits == 0 for c in rivals) \
            and (best.ratio is None or best.ratio <= 1.15):
        return "matched", best.tmdb_id, best.score
    if best.score >= cfg.review_floor or (best.cast_hits >= 2 and (best.ratio is None or 0.75 <= best.ratio <= 1.12)):
        return "review", best.tmdb_id, best.score
    return "unmatched", None, best.score


def match_video(cfg: Config, tmdb: TMDB, row: sqlite3.Row) -> tuple[str, int | None, float, list[dict]]:
    alts = json.loads(row["alt_titles"] or "[]")
    queries = [row["clean_title"]] + alts
    people = extract_people(row["title"], row["clean_title"], alts)
    movies = find_candidates(tmdb, queries, row["year_hint"], people)
    cands = sorted((score_movie(queries, row["duration"], row["year_hint"], m, people) for m in movies),
                   key=lambda c: c.score, reverse=True)
    status, tmdb_id, score = decide(cfg, cands)
    return status, tmdb_id, score, [c.as_dict() for c in cands[:4]]


def propagate_twins(conn: sqlite3.Connection, log=print) -> int:
    """An undecided upload whose length matches a matched upload to within seconds, and whose
    title is related, is the same file re-posted. e.g. "Shanghai Joe" (5641s) and
    "My Name Is Shanghai Joe" (5638s)."""
    decided = conn.execute("""SELECT video_id, clean_title, duration, tmdb_id, score FROM videos
                              WHERE match_status IN ('matched','manual') AND duration IS NOT NULL""").fetchall()
    pending = conn.execute("""SELECT video_id, clean_title, duration, candidates FROM videos
                              WHERE match_status IN ('review','unmatched') AND duration IS NOT NULL""").fetchall()
    n = 0
    for p in pending:
        cand_ids = {c["tmdb_id"] for c in json.loads(p["candidates"] or "[]")}
        pk = match_key(p["clean_title"])
        twins = [d for d in decided if abs(d["duration"] - p["duration"]) <= TWIN_SECONDS
                 and (fuzz.partial_ratio(pk, match_key(d["clean_title"])) >= 70 or d["tmdb_id"] in cand_ids)]
        tmdb_ids = {t["tmdb_id"] for t in twins}
        if len(tmdb_ids) == 1:
            t = twins[0]
            conn.execute("""UPDATE videos SET match_status='matched', tmdb_id=?, score=?,
                            candidates=json_insert(COALESCE(candidates,'[]'), '$[#]',
                                json_object('tmdb_id', ?, 'title', 'duration twin of ' || ?, 'year', NULL,
                                            'runtime', NULL, 'score', ?, 'why', 'same length as matched upload'))
                            WHERE video_id=?""",
                         (t["tmdb_id"], t["score"], t["tmdb_id"], t["video_id"], t["score"], p["video_id"]))
            n += 1
    conn.commit()
    if n:
        log(f"  {n} uploads matched as duration twins of already-matched uploads")
    return n


def sibling_tiebreak(cfg: Config, conn: sqlite3.Connection, log=print) -> int:
    """Two "California" uploads: one lists Giuliano Gemma and matched the 1977 film, the other has no
    names and ties 1947 vs 1977. If a same-title sibling matched one of the tied films, take it."""
    rows = conn.execute("""SELECT video_id, clean_title, candidates FROM videos
                           WHERE match_status='review' AND candidates IS NOT NULL""").fetchall()
    n = 0
    for r in rows:
        cands = json.loads(r["candidates"])
        if len(cands) < 2:
            continue
        tied = {c["tmdb_id"] for c in cands if cands[0]["score"] - c["score"] < cfg.accept_margin}
        sib = conn.execute("""SELECT DISTINCT tmdb_id FROM videos WHERE clean_title=? COLLATE NOCASE
                              AND match_status IN ('matched','manual') AND video_id<>?""",
                           (r["clean_title"], r["video_id"])).fetchall()
        hit = tied & {s["tmdb_id"] for s in sib}
        if len(hit) == 1:
            tid = hit.pop()
            conn.execute("UPDATE videos SET match_status='matched', tmdb_id=? WHERE video_id=?", (tid, r["video_id"]))
            n += 1
    conn.commit()
    if n:
        log(f"  {n} tied uploads resolved by a same-title sibling's match")
    return n


def run_matching(cfg: Config, conn: sqlite3.Connection, tmdb: TMDB, rematch: bool = False, limit: int | None = None,
                 log=print) -> dict[str, int]:
    # Manual decisions always win.
    conn.execute("""
        UPDATE videos SET match_status='skipped', tmdb_id=NULL
        WHERE video_id IN (SELECT video_id FROM overrides WHERE skip=1)""")
    conn.execute("""
        UPDATE videos SET match_status='manual',
            tmdb_id=(SELECT tmdb_id FROM overrides o WHERE o.video_id=videos.video_id)
        WHERE video_id IN (SELECT video_id FROM overrides WHERE skip=0 AND tmdb_id IS NOT NULL)""")
    conn.execute("UPDATE videos SET match_status='extra' WHERE is_extra=1 AND match_status='pending'")
    conn.commit()

    statuses = ("pending", "review", "unmatched", "matched") if rematch else ("pending",)
    rows = conn.execute(
        f"SELECT * FROM videos WHERE match_status IN ({','.join('?' * len(statuses))}) ORDER BY first_seen",
        statuses,
    ).fetchall()
    if limit:
        rows = rows[:limit]

    counts: dict[str, int] = {}
    for i, row in enumerate(rows, 1):
        status, tmdb_id, score, top = match_video(cfg, tmdb, row)
        conn.execute(
            """UPDATE videos SET match_status=?, tmdb_id=?, score=?, candidates=?, matched_at=datetime('now')
               WHERE video_id=?""",
            (status, tmdb_id, score, json.dumps(top), row["video_id"]),
        )
        counts[status] = counts.get(status, 0) + 1
        if i % 25 == 0:
            conn.commit()
            log(f"  {i}/{len(rows)} matched so far: {counts}")
    conn.commit()
    twins = propagate_twins(conn, log)
    if twins:
        counts["twin"] = twins
    sibs = sibling_tiebreak(cfg, conn, log)
    if sibs:
        counts["sibling"] = sibs
    return counts
