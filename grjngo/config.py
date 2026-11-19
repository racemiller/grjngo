from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PATH_KEYS = {"data_dir", "library_dir", "staging_dir"}


@dataclass
class Config:
    data_dir: Path = Path("/opt/grjngo/data")
    library_dir: Path = Path("/mnt/westerns")      # ErsatzTV local movie library (NAS)
    staging_dir: Path = Path("/opt/grjngo/staging")  # local scratch space for in-progress downloads
    channel_url: str = "https://www.youtube.com/@GrjngoWesternMovies/videos"
    tmdb_api_key: str = ""
    tmdb_language: str = "en-US"
    # Matching thresholds (0-100)
    accept_score: float = 80.0
    accept_margin: float = 8.0
    review_floor: float = 45.0
    # "prefer_original" | "prefer_colorized" | "keep_both"
    colorized_policy: str = "prefer_original"
    # Downloads
    max_height: int = 1080
    max_downloads_per_run: int = 30
    max_probes_per_run: int = 200
    sleep_interval: int = 20          # seconds before each download (random up to max_sleep_interval)
    max_sleep_interval: int = 60
    sleep_requests: float = 1.5       # between yt-dlp's own HTTP requests
    probe_pause: list[float] = field(default_factory=lambda: [3.0, 8.0])
    limit_rate: str = ""              # e.g. "8M"; empty = unlimited
    max_failures: int = 3             # give up on an upload after this many non-block failures
    ytdlp_args: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "grjngo.sqlite"

    @property
    def format_sort(self) -> str:
        # Largest resolution <= cap; VP9 before H.264, AV1 last (i5-8500 iGPU can't decode AV1); Opus audio.
        return f"res:{self.max_height},vcodec:vp9,acodec:opus"


def load_config(path: str | None) -> Config:
    cfg = Config()
    p = Path(path or os.environ.get("GRJNGO_CONFIG", "/opt/grjngo/config.toml"))
    if p.exists():
        raw = tomllib.loads(p.read_text())
        for k, v in raw.items():
            if hasattr(cfg, k):
                setattr(cfg, k, Path(v) if k in PATH_KEYS else v)
    cfg.tmdb_api_key = os.environ.get("TMDB_API_KEY", cfg.tmdb_api_key)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
