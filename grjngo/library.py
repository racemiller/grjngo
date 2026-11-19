"""Naming, Kodi-style .nfo files and artwork for the ErsatzTV local movie library.

Layout:
    <library_dir>/Stagecoach (1939)/Stagecoach (1939).mkv
                                   /Stagecoach (1939).nfo
                                   /poster.jpg
                                   /fanart.jpg
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape

import requests

IMG = "https://image.tmdb.org/t/p/original"


def safe_name(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = s.replace(": ", " - ").replace(":", "-").replace("/", "-").replace("\\", "-")
    s = re.sub(r'[<>"|?*\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s or "Untitled"


def movie_year(movie: dict) -> str:
    d = movie.get("release_date") or ""
    return d[:4] if d[:4].isdigit() else ""


def base_name(movie: dict, edition: str = "") -> str:
    year = movie_year(movie)
    name = safe_name(movie.get("title") or movie.get("original_title") or f"tmdb {movie['id']}")
    name = f"{name} ({year})" if year else name
    return f"{name} - {edition}" if edition else name


def us_certification(movie: dict) -> str:
    for r in (movie.get("release_dates") or {}).get("results", []):
        if r.get("iso_3166_1") == "US":
            for d in r.get("release_dates", []):
                if d.get("certification"):
                    return d["certification"]
    return ""


def _el(tag: str, value, **attrs) -> str:
    if value in (None, ""):
        return ""
    a = "".join(f' {k}="{escape(str(v))}"' for k, v in attrs.items())
    return f"  <{tag}{a}>{escape(str(value))}</{tag}>\n"


def build_nfo(movie: dict, video: dict, colorized: bool) -> str:
    credits = movie.get("credits") or {}
    crew = credits.get("crew") or []
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<movie>\n']
    out.append(_el("title", movie.get("title")))
    if movie.get("original_title") and movie.get("original_title") != movie.get("title"):
        out.append(_el("originaltitle", movie.get("original_title")))
    out.append(_el("year", movie_year(movie)))
    out.append(_el("premiered", movie.get("release_date")))
    out.append(_el("runtime", movie.get("runtime")))
    out.append(_el("plot", movie.get("overview")))
    out.append(_el("outline", movie.get("overview")))
    out.append(_el("tagline", movie.get("tagline")))
    out.append(_el("mpaa", us_certification(movie)))
    for g in movie.get("genres") or []:
        out.append(_el("genre", g.get("name")))
    for c in movie.get("production_countries") or []:
        out.append(_el("country", c.get("name")))
    for s in movie.get("production_companies") or []:
        out.append(_el("studio", s.get("name")))
    for c in crew:
        if c.get("job") == "Director":
            out.append(_el("director", c.get("name")))
    for c in crew:
        if c.get("department") == "Writing":
            out.append(_el("credits", c.get("name")))
    out.append(_el("tag", "Grjngo"))
    if colorized:
        out.append(_el("tag", "Colorized"))
    out.append(_el("uniqueid", movie["id"], type="tmdb", default="true"))
    imdb = (movie.get("external_ids") or {}).get("imdb_id") or movie.get("imdb_id")
    out.append(_el("uniqueid", imdb, type="imdb"))
    out.append(_el("uniqueid", video["video_id"], type="youtube"))
    if movie.get("poster_path"):
        out.append(_el("thumb", IMG + movie["poster_path"], aspect="poster"))
    if movie.get("backdrop_path"):
        out.append(f"  <fanart>\n  {_el('thumb', IMG + movie['backdrop_path']).strip()}\n  </fanart>\n")
    for i, a in enumerate((credits.get("cast") or [])[:15]):
        out.append("  <actor>\n")
        out.append("  " + _el("name", a.get("name")))
        out.append("  " + _el("role", a.get("character")))
        out.append("  " + _el("order", i))
        if a.get("profile_path"):
            out.append("  " + _el("thumb", IMG + a["profile_path"]))
        out.append("  </actor>\n")
    out.append("</movie>\n")
    return "".join(x for x in out if x)


def fetch_image(url_path: str | None, dest: Path) -> None:
    if not url_path or dest.exists():
        return
    try:
        r = requests.get(IMG + url_path, timeout=30)
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(r.content)
        os.replace(tmp, dest)
    except Exception as e:  # artwork is nice-to-have
        print(f"    artwork failed ({dest.name}): {e}")


def folder_for(library_dir: Path, movie: dict, edition: str, taken_by_other: bool) -> Path:
    name = base_name(movie, edition)
    if taken_by_other:
        name = f"{name} [tmdbid-{movie['id']}]"
    return library_dir / name


def install(src: Path, folder: Path, movie: dict, video: dict, colorized: bool, edition: str = "") -> Path:
    """Move a finished download into place (atomically replacing an older copy) and write metadata."""
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder.name
    dest = folder / f"{stem}{src.suffix}"
    tmp = folder / f".{stem}.incoming{src.suffix}"
    shutil.move(str(src), tmp)          # NAS: copy then delete
    os.replace(tmp, dest)
    for old in folder.glob(f"{glob_escape(stem)}.*"):  # drop an old copy with a different extension
        if old.suffix not in (dest.suffix, ".nfo") and old.is_file() and old.suffix in (".mkv", ".mp4", ".webm"):
            old.unlink()
    write_metadata(folder, movie, video, colorized)
    return dest


def write_metadata(folder: Path, movie: dict, video: dict, colorized: bool) -> None:
    stem = folder.name
    nfo = folder / f"{stem}.nfo"
    tmp = folder / f".{stem}.nfo.part"
    tmp.write_text(build_nfo(movie, video, colorized), encoding="utf-8")
    os.replace(tmp, nfo)
    fetch_image(movie.get("poster_path"), folder / "poster.jpg")
    fetch_image(movie.get("backdrop_path"), folder / "fanart.jpg")


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)
