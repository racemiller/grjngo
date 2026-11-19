"""yt-dlp wrapper: probing, downloading, and telling 'YouTube blocked this IP' apart from
'this video is gone'."""
from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

from .config import Config

BLOCK_SIGNS = (
    "confirm you're not a bot", "confirm you’re not a bot", "http error 429", "too many requests",
    "try again later", "rate-limited", "rate limited", "sign in to confirm",
)
GONE_SIGNS = (
    "video unavailable", "private video", "has been removed", "account associated with this video has been terminated",
    "copyright claim", "not available in your country", "members-only", "this video is no longer available",
)
EXIT_BLOCKED = 75  # tells run.sh to rotate the VPN server and try again


class Blocked(Exception):
    """YouTube is refusing this IP. Stop the run; the wrapper rotates the VPN."""


class Gone(Exception):
    """The upload itself can't be fetched (deleted/private). Try another upload of the same film."""


def classify(stderr: str) -> type[Exception] | None:
    low = stderr.lower()
    if any(s in low for s in GONE_SIGNS) and "try again later" not in low:
        return Gone
    if any(s in low for s in BLOCK_SIGNS):
        return Blocked
    return None


def _base(cfg: Config) -> list[str]:
    args = ["yt-dlp", "--no-playlist", "--ignore-config", "--no-warnings",
            "--sleep-requests", str(cfg.sleep_requests), *cfg.ytdlp_args]
    return args


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _raise_for(res: subprocess.CompletedProcess, what: str) -> None:
    if res.returncode == 0:
        return
    err = (res.stderr or "")[-3000:]
    kind = classify(err)
    if kind:
        raise kind(err.strip().splitlines()[-1] if err.strip() else what)
    raise RuntimeError(f"{what}: {err.strip().splitlines()[-1] if err.strip() else res.returncode}")


def probe(cfg: Config, video_id: str) -> dict:
    """What would we actually get? Returns height/vcodec/etc. of the selected formats."""
    cmd = [*_base(cfg), "-J", "--skip-download", "-f", "bv*+ba/b", "-S", cfg.format_sort,
           f"https://www.youtube.com/watch?v={video_id}"]
    res = _run(cmd, timeout=180)
    _raise_for(res, f"probe {video_id}")
    info = json.loads(res.stdout)
    fmts = info.get("requested_formats") or [info]
    v = next((f for f in fmts if f.get("vcodec") not in (None, "none")), fmts[0])
    a = next((f for f in fmts if f.get("acodec") not in (None, "none")), {})
    size = sum((f.get("filesize") or f.get("filesize_approx") or 0) for f in fmts)
    return {"height": v.get("height"), "width": v.get("width"), "vcodec": v.get("vcodec"),
            "acodec": a.get("acodec"), "fps": v.get("fps"), "filesize": size or None,
            "duration": info.get("duration")}


def download(cfg: Config, video_id: str) -> dict:
    cfg.staging_dir.mkdir(parents=True, exist_ok=True)
    cmd = [*_base(cfg), "-f", "bv*+ba/b", "-S", cfg.format_sort, "--merge-output-format", "mkv",
           "--remux-video", "mkv", "--no-mtime", "--retries", "10", "--fragment-retries", "10",
           "--sleep-interval", str(cfg.sleep_interval), "--max-sleep-interval", str(cfg.max_sleep_interval),
           "-o", str(cfg.staging_dir / "%(id)s.%(ext)s"),
           "--print", "after_move:%(.{filepath,height,width,vcodec,acodec,duration,fps})j"]
    if cfg.limit_rate:
        cmd += ["--limit-rate", cfg.limit_rate]
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")
    res = _run(cmd, timeout=6 * 3600)
    _raise_for(res, f"download {video_id}")
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("{")][-1]
    info = json.loads(line)
    if not Path(info["filepath"]).exists():
        raise RuntimeError(f"yt-dlp reported {info['filepath']} but it isn't there")
    return info


def pause(cfg: Config) -> float:
    lo, hi = cfg.probe_pause
    return random.uniform(lo, hi)
