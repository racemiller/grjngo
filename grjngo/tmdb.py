"""Small cached TMDB client. Accepts a v3 API key or a v4 read-access token."""
from __future__ import annotations

import json
import sqlite3
import time

import requests

API = "https://api.themoviedb.org/3"


class TMDB:
    def __init__(self, api_key: str, conn: sqlite3.Connection, language: str = "en-US"):
        if not api_key:
            raise SystemExit("TMDB API key missing: set tmdb_api_key in config.toml or TMDB_API_KEY")
        self.conn = conn
        self.language = language
        self.s = requests.Session()
        if api_key.startswith("eyJ"):  # v4 read access token (JWT)
            self.s.headers["Authorization"] = f"Bearer {api_key}"
            self.params = {}
        else:
            self.params = {"api_key": api_key}

    def _get(self, path: str, **params) -> dict:
        for attempt in range(5):
            r = self.s.get(f"{API}{path}", params={**self.params, **params}, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 2)) + attempt)
                continue
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            time.sleep(0.03)  # stay well under TMDB's ~40 req/s
            return r.json()
        raise RuntimeError(f"TMDB kept rate-limiting {path}")

    def search(self, query: str, year: int | None = None) -> list[dict]:
        key = f"{query.lower()}|{year or ''}"
        row = self.conn.execute("SELECT json FROM tmdb_search_cache WHERE query_key=?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
        params = {"query": query, "include_adult": "false", "language": self.language}
        if year:
            params["year"] = year
        results = self._get("/search/movie", **params).get("results", [])[:8]
        self.conn.execute(
            "INSERT OR REPLACE INTO tmdb_search_cache(query_key, json) VALUES (?,?)",
            (key, json.dumps(results)),
        )
        return results

    def _cached(self, key: str, fetch) -> list | dict:
        row = self.conn.execute("SELECT json FROM tmdb_search_cache WHERE query_key=?", (key,)).fetchone()
        if row:
            return json.loads(row[0])
        data = fetch()
        self.conn.execute("INSERT OR REPLACE INTO tmdb_search_cache(query_key, json) VALUES (?,?)",
                          (key, json.dumps(data)))
        return data

    def person_id(self, name: str) -> int | None:
        res = self._cached(f"person|{name.lower()}",
                           lambda: self._get("/search/person", query=name).get("results", [])[:3])
        return res[0]["id"] if res else None

    def movies_with_cast(self, person_ids: list[int], western_only: bool = True) -> list[dict]:
        """Films featuring ALL of the given people (TMDB discover, comma = AND)."""
        ids = ",".join(str(i) for i in person_ids)
        params = {"with_cast": ids, "sort_by": "primary_release_date.asc", "include_adult": "false"}
        if western_only:
            params["with_genres"] = "37"

        def fetch():
            out = []
            for page in (1, 2, 3):
                data = self._get("/discover/movie", page=page, **params)
                out.extend(data.get("results", []))
                if page >= data.get("total_pages", 1):
                    break
            return out
        return self._cached(f"discover|{ids}|{int(western_only)}", fetch)

    def movie(self, tmdb_id: int) -> dict:
        row = self.conn.execute("SELECT json FROM tmdb_movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()
        if row:
            return json.loads(row[0])
        data = self._get(
            f"/movie/{tmdb_id}",
            language=self.language,
            append_to_response="alternative_titles,credits,release_dates,external_ids",
        )
        if data:
            self.conn.execute(
                "INSERT OR REPLACE INTO tmdb_movies(tmdb_id, json) VALUES (?,?)",
                (tmdb_id, json.dumps(data)),
            )
        return data
