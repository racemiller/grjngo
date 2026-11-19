# grjngo

Automatically turns the **GrjngoWesternMovies** YouTube channel into a 24/7 Western channel on **ErsatzTV**.

Each run finds new uploads, works out which movie each one is, keeps only the best copy of each film,
downloads it through NordVPN, names it `Title (Year)`, writes metadata ErsatzTV understands, and drops it into
the ErsatzTV library folder on the NAS. It runs on a systemd timer, with no hands-on work beyond reviewing
the occasional upload it can't identify.

---

## Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Setting up from scratch](#setting-up-from-scratch)
  - [1. Proxmox LXC](#1-proxmox-lxc)
  - [2. NAS share](#2-nas-share)
  - [3. Clone and configure](#3-clone-and-configure)
  - [4. Secrets](#4-secrets)
  - [5. Build and test the VPN](#5-build-and-test-the-vpn)
  - [6. First matching pass and review](#6-first-matching-pass-and-review)
  - [7. First download run](#7-first-download-run)
  - [8. Schedule it](#8-schedule-it)
  - [9. ErsatzTV](#9-ersatztv)
- [Restoring from a backup](#restoring-from-a-backup)
- [Everyday use](#everyday-use)
- [Configuration reference](#configuration-reference)
- [How decisions are made](#how-decisions-are-made)
- [Troubleshooting](#troubleshooting)
- [Design notes](#design-notes)

---

## How it works

Every run of `docker/run.sh` does this:

| Step | What happens |
|---|---|
| **Scan** | `yt-dlp --flat-playlist` lists every upload on the channel (titles, IDs, durations; no downloads). New uploads go into a SQLite database. |
| **Clean** | Marketing junk is stripped from titles (`Title \| 4K \| Actor \| Western \| English` → `Title`). Actor names, "aka" titles, year hints and "colorized" are kept as clues. Trailers, Shorts and anything under 40 minutes are set aside. |
| **Match** | Each upload is matched to a TMDB movie by title, alternate titles, cast (actor names from the YouTube tags vs. TMDB credits), runtime, year and the Western genre. Confident matches are accepted; uncertain ones go to a review list. |
| **Dedupe** | Uploads that match the same TMDB movie are one film. The channel re-uploads constantly (913 uploads ≈ 627 films at last count). |
| **Probe** | For films with several uploads, yt-dlp checks the real resolution of each one (titles that say "4K" are often 720p). |
| **Download** | The best upload of each film is downloaded: VP9 video + Opus audio in MKV, capped at 1080p. |
| **Organize** | Saved as `Title (Year)/Title (Year).mkv` with a Kodi-style `.nfo`, `poster.jpg` and `fanart.jpg`. |
| **Upgrade** | If a better upload of a film you already have appears later, it replaces the old file in place. |

All YouTube and TMDB traffic goes through a dedicated **gluetun** container connected to NordVPN. Every run gets
a random server, and if YouTube blocks it partway through, `run.sh` switches to a new server and picks up
where it left off.

---

## Architecture

```
Proxmox host
├── LXC: grjngo (Debian 13, Docker)
│     ├── /opt/grjngo                ← this repo + data/ (SQLite state) + staging/
│     ├── /mnt/westerns              ← SMB mount of the NAS share (the movie library)
│     └── docker compose (only while a run is active)
│           ├── gluetun  (NordVPN, WireGuard; firewall blocks non-VPN traffic)
│           └── worker   (python + yt-dlp nightly + deno + ffmpeg; shares gluetun's network)
│
├── LXC: ErsatzTV (systemd service, Intel i5-8500 iGPU passed through)
│     └── Local Movies library → same NAS share via its own SMB mount
│
NAS (OpenMediaVault) ── SMB share holding the movie library
Plex ── Live TV / DVR pointed at ErsatzTV → LG TV
```

---

## Repository layout

```
grjngo/                 Python package (the actual pipeline)
  titles.py             title cleaning, actor-name extraction, normalisation
  tmdb.py               cached TMDB client (search, details, cast lookups)
  matcher.py            scoring, accept/review decisions, re-upload pairing
  pipeline.py           keeper ranking, probing, downloads, upgrades, re-filing after manual fixes
  library.py            folder/file naming, .nfo writer, artwork
  ytdlp.py              yt-dlp wrapper; tells "IP blocked" apart from "video deleted"
  db.py                 SQLite schema
  config.py             settings (config.toml)
  cli.py                `grjngo <command>` entry points
docker/
  Dockerfile            worker image
  entrypoint.sh         updates yt-dlp each run, drops root, runs `python -m grjngo`
  docker-compose.yml    gluetun + worker
  run.sh                scheduled entry point (VPN rotation, lock, cleanup)
  grjngo.service/.timer systemd units
  .env.example          template for secrets
config.example.toml     template for settings
requirements.txt        for running the CLI outside Docker (optional)
```

**Not in git** (see `.gitignore`): `config.toml`, `docker/.env`, `data/`, `staging/`.
`data/grjngo.sqlite` holds all your match decisions. **Back it up** (see [Restoring](#restoring-from-a-backup)).

---

## Setting up from scratch

### Prerequisites

- A Proxmox host with a Debian 13 LXC (2 cores and 2 GB RAM is plenty; ~50 GB of local disk for staging)
- Docker + the compose plugin installed in that LXC
- An SMB share on the NAS for the movies (~1 TB for the full catalog at 1080p)
- A NordVPN subscription
- A free TMDB account and API key: <https://www.themoviedb.org/settings/api>
- ErsatzTV already installed (here: its own LXC with the iGPU passed through)

### 1. Proxmox LXC

gluetun needs the TUN device. On the **Proxmox host**, add to `/etc/pve/lxc/<CTID>.conf`:

```
features: nesting=1
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Restart the container, then inside it confirm `ls -l /dev/net/tun` exists.

Install Docker if it isn't already:

```bash
apt update && apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sh
```

### 2. NAS share

Mount the movie share in the LXC (and separately in the ErsatzTV LXC). Example `/etc/fstab` line:

```
//NAS_IP/westerns  /mnt/westerns  cifs  credentials=/root/.smb-westerns,uid=1000,gid=1000,iocharset=utf8,_netdev,x-systemd.automount  0  0
```

```bash
mkdir -p /mnt/westerns && mount -a && ls -ln /mnt/westerns
```

Note the uid/gid the files show; you'll use them as `PUID`/`PGID`.

### 3. Clone and configure

```bash
git clone https://github.com/<you>/<repo>.git /opt/grjngo
cd /opt/grjngo
cp config.example.toml config.toml
nano config.toml
```

At minimum, check `library_dir` (must be the SMB mount path) and `colorized_policy`.

### 4. Secrets

```bash
cd /opt/grjngo/docker
cp .env.example .env && chmod 600 .env
nano .env
```

| Variable | Value |
|---|---|
| `WIREGUARD_PRIVATE_KEY` | NordLynx key (see below) |
| `SERVER_COUNTRIES` | e.g. `United States`; a random server from these is used each run |
| `TMDB_API_KEY` | TMDB v3 key or v4 read-access token |
| `LIBRARY_DIR` | same path as `library_dir` in `config.toml`, e.g. `/mnt/westerns` |
| `PUID` / `PGID` | owner shown by `ls -ln` on the SMB mount |

**Getting the NordLynx (WireGuard) key:** in the Nord account dashboard, go to *NordVPN → Set up NordVPN manually*
and generate an access token, then:

```bash
curl -s -u token:<ACCESS_TOKEN> https://api.nordvpn.com/v1/users/services/credentials
```

Copy `nordlynx_private_key`. Alternatively set `VPN_TYPE=openvpn` with the *service credentials* from the same
page (not your Nord login).

> Use a **dedicated** gluetun for this stack, not one shared with other services. Rotation works by recreating
> the container, which would knock any other service sharing it offline. The dedicated one only runs during jobs.

### 5. Build and test the VPN

```bash
cd /opt/grjngo/docker
docker compose build
docker compose up -d gluetun
docker logs -f grjngo-gluetun        # wait for "healthy"; it prints the public IP
docker compose run --rm worker status
docker compose down
```

The worker prints the yt-dlp version and its public IP at startup; that IP must be a Nord IP, not your own.

Handy alias for the rest of this guide:

```bash
echo "alias grjngo='docker compose -f /opt/grjngo/docker/docker-compose.yml run --rm worker'" >> ~/.bashrc
source ~/.bashrc
```

### 6. First matching pass and review

```bash
grjngo scan              # list the channel (or: grjngo import-tsv file.tsv)
grjngo match             # first run ~10 min; TMDB answers are cached
grjngo report            # data/duplicates.txt: each film's uploads and which one is kept
grjngo review            # data/review.csv: uploads it wasn't sure about
```

Open `data/review.csv` in Excel/LibreOffice. For each row:

- `set_tmdb_id`: `ok` to accept the suggestion, or paste the correct TMDB ID (the number in
  `themoviedb.org/movie/<id>-...`)
- `skip`: `x` for anything that isn't a movie
- leave both blank to decide later

```bash
grjngo apply-review /opt/grjngo/data/review.csv
grjngo match
grjngo report            # skim duplicates.txt for wrong merges
grjngo plan              # dry run: what would be downloaded
```

Fix any single upload with `grjngo override <youtube-url-or-id> <tmdb_id|skip>`.
Manual decisions are permanent; later runs never overwrite them.

### 7. First download run

```bash
/opt/grjngo/docker/run.sh
```

Watch the first one. Expect roughly 15–20 seconds per probe and a sleep of 20–60 seconds between downloads. It's
safe to stop (Ctrl+C) and restart at any time: every probe and download is recorded as it completes, and nothing
appears in the library until it's fully downloaded. After stopping, clean up with
`docker compose --profile job down` in `docker/`.

### 8. Schedule it

```bash
cp /opt/grjngo/docker/grjngo.service /opt/grjngo/docker/grjngo.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now grjngo.timer
systemctl list-timers grjngo.timer
journalctl -u grjngo -f
```

The timer ships set to **daily** (03:30, ±30 min) for working through the back catalog at ~30 films per run.
Once `grjngo plan` shows nothing left, switch to **weekly** by editing `/etc/systemd/system/grjngo.timer`:

```
OnCalendar=Sun *-*-* 03:30:00
```

then `systemctl daemon-reload`.

### 9. ErsatzTV

**Library**
- *Media Sources → Local*: add/edit a **Movies** library whose path is the NAS share as mounted in the
  ErsatzTV LXC. ErsatzTV reads the `.nfo` files and artwork.
- *Settings*: set the library refresh interval (every few hours is fine). ErsatzTV sees the share over SMB,
  so it relies on this periodic rescan rather than instant change detection.
- Optional: trigger a scan at the end of `run.sh` with
  `curl -X POST http://<ersatztv-host>:8409/api/libraries/<id>/scan`. Test it by hand first.

**Collection.** Build the schedule from a **smart collection** so new films join automatically. Every file has
the tag `Grjngo`, and colorized ones also have `Colorized`:

```
tag:Grjngo
```

A manual collection only contains what was hand-picked; new downloads won't appear in it.

**FFmpeg profile** (for Plex Live TV on the LG with minimal re-transcoding):

| Setting | Value |
|---|---|
| Hardware acceleration | VAAPI (iHD driver, `/dev/dri/renderD128`) or QSV |
| Resolution | 1920×1080 |
| Video format | H.264 |
| Audio | AAC, 2 channels |

The UHD 630 decodes the VP9 source files and encodes H.264 in hardware.

**Playout.** ErsatzTV builds a few days ahead. New films enter the rotation as it extends; reset the playout
to include them immediately.

---

## Restoring from a backup

Everything worth keeping that isn't in git:

| Path | Why |
|---|---|
| `/opt/grjngo/data/grjngo.sqlite` | every upload, match, manual decision, probe and library entry |
| `/opt/grjngo/config.toml` | settings |
| `/opt/grjngo/docker/.env` | secrets |

Back these up together (e.g. nightly copy to the NAS):

```bash
sqlite3 /opt/grjngo/data/grjngo.sqlite ".backup '/mnt/westerns/.grjngo-backup/grjngo.sqlite'"
cp /opt/grjngo/config.toml /opt/grjngo/docker/.env /mnt/westerns/.grjngo-backup/
```

To rebuild: follow [setting up from scratch](#setting-up-from-scratch) steps 1–5, copy the three files back,
then run `grjngo status`. The movies on the NAS are already recorded in the database, so nothing is
downloaded again.

**Lost the database?** The movie files and `.nfo`s are fine, but the pipeline no longer knows what it has.
Run steps 6–7 again. Matching is automatic apart from the review list, and existing folders get the same names,
so files are re-downloaded over themselves. To avoid re-downloading ~1 TB, ask for a "rebuild library table from
the .nfo files" command, since each `.nfo` stores the TMDB ID and the YouTube ID it came from.

---

## Everyday use

All commands: `grjngo <command>` (Docker alias) or `python -m grjngo <command>`.

| Command | What it does |
|---|---|
| `status` | one-screen summary: matches, library size, failures |
| `plan [--show N]` | dry run of what the next run will download/upgrade |
| `review` | write `data/review.csv` with uploads needing a decision |
| `apply-review FILE` | read decisions back from the CSV |
| `override <url\|id> <tmdb_id\|skip> [--note ..]` | fix one upload; downloaded files are moved/removed on the next run |
| `report` | write `data/duplicates.txt`: films with several uploads and the chosen one |
| `match [--rematch]` | match pending uploads; `--rematch` re-scores everything not decided by hand |
| `scan` | list the channel's uploads |
| `probe [--limit N]` | check real resolutions of competing uploads |
| `download [--limit N]` | download per the plan |
| `pipeline [--no-scan]` | the full scheduled job (what `run.sh` calls) |
| `nfo [--refresh]` | rewrite every `.nfo` and artwork (`--refresh` re-fetches TMDB data first) |

Typical weekly check: `journalctl -u grjngo --since -7d | tail -40`, and if it says uploads need review,
run `review` → edit → `apply-review` → `match`.

---

## Configuration reference

`config.toml` (all optional; defaults shown in `config.example.toml`):

| Key | Default | Meaning |
|---|---|---|
| `library_dir` | `/mnt/westerns` | ErsatzTV library folder (SMB mount) |
| `data_dir` | `/opt/grjngo/data` | database, review CSV, reports, caches |
| `staging_dir` | `/opt/grjngo/staging` | local scratch for in-progress downloads |
| `channel_url` | channel `/videos` tab | what to scan |
| `tmdb_api_key` | | or set `TMDB_API_KEY` in `.env` |
| `accept_score` / `accept_margin` / `review_floor` | 80 / 8 / 45 | match confidence thresholds |
| `colorized_policy` | `prefer_original` | `prefer_original`, `prefer_colorized`, or `keep_both` (separate `Title (Year) - Colorized` folder) |
| `max_height` | 1080 | resolution cap |
| `max_downloads_per_run` | 30 | throttle per run |
| `max_probes_per_run` | 200 | throttle per run |
| `sleep_interval` / `max_sleep_interval` | 20 / 60 | random wait before each download (s) |
| `sleep_requests` | 1.5 | wait between yt-dlp's own requests (s) |
| `probe_pause` | `[3.0, 8.0]` | random wait between probes (s) |
| `limit_rate` | `""` | bandwidth cap, e.g. `"8M"` |
| `max_failures` | 3 | give up on an upload after this many non-block failures |
| `ytdlp_args` | `[]` | extra yt-dlp arguments |

Faster (but more likely to trip YouTube's rate limit): `sleep_requests = 0.75`, `probe_pause = [1.0, 3.0]`.
Blocks are handled automatically by rotating servers, so the cost is lost time, not breakage.

`run.sh` honours `MAX_TRIES` (default 4): the number of VPN servers to try per run.

---

## How decisions are made

**Matching** (per upload, never per title, since the channel reuses titles for different films):

- title similarity against TMDB title, original title and all alternate titles (×0.7)
- runtime: +15 within −7%/+6% (allows PAL speed-up); a **shorter** upload is only mildly penalised
  (TV cuts are common: Roy Rogers films trimmed from 74 to 51 min); a **longer** one is penalised hard
- cast: +12/+18 when actor names in the YouTube tags appear in TMDB credits (also matches character names like
  "The Cisco Kid", and directors)
- +8 Western genre, ±year hint
- **auto-accept** when the score is ≥ 80 and ≥ 8 points clear of the runner-up, or the title is unique on TMDB and
  it's a Western, or the cast picks exactly one of several same-titled films
- **re-upload pairing:** an uncertain upload within 4 seconds of a matched upload's length, with a related
  title, is the same file and takes its match. A same-title upload that ties between films follows its
  sibling's match
- fallback: if no title match is found but actors are listed, search those actors' Westerns on TMDB (finds
  films the channel posts under an alternate title)

**Keeper** (which upload of a film to download), best first:

1. colorized policy (B&W original by default)
2. real resolution, capped at 1080p (a "4K" upload and a 1080p one tie)
3. completeness: runtime closest to the full film (2-minute steps)
4. VP9 over H.264 over anything else
5. oldest upload

A new upload replaces an existing file only if it wins on 1–3. Equal-quality re-uploads are ignored.

**Formats:** `-S res:1080,vcodec:vp9,acodec:opus`, merged to MKV. YouTube doesn't offer HEVC; VP9 is in the
same efficiency class, so re-encoding would cost quality and days of GPU time for little space saved. AV1 is
ranked last because the i5-8500 can't hardware-decode it.

---

## Troubleshooting

**`run.sh` keeps rotating / "YouTube is blocking this IP"**
It tries `MAX_TRIES` servers, then gives up until the next run. If it happens every night: slow down
(`sleep_requests`, `probe_pause`, `max_downloads_per_run`), try another `SERVER_COUNTRIES`, and make sure
yt-dlp is current (the worker updates to nightly on start; check the version it prints).

**yt-dlp errors about signatures, n-challenge, or "no formats"**
YouTube changed something. Wait a day for a yt-dlp nightly fix (picked up automatically), or rebuild the image
(`docker compose build --no-cache`) if deno or ffmpeg need updating.

**gluetun never becomes healthy**
`docker logs grjngo-gluetun`. Usual causes: a wrong/expired WireGuard key, or `/dev/net/tun` missing in the
LXC (step 1).

**Files on the NAS have the wrong owner / permission denied**
`PUID`/`PGID` in `.env` don't match the SMB mount (`ls -ln /mnt/westerns`), or the mount is read-only.

**A film was matched to the wrong movie**
`grjngo override <url> <correct_tmdb_id>`. The next run moves the file to the right folder (or removes it if the
correct film already has a better copy).

**An upload keeps failing**
After 3 non-block failures it's skipped and another upload of the same film is used. `grjngo status` shows the
count; the errors are in the `failures` table:
`sqlite3 data/grjngo.sqlite "select * from failures order by failed_at desc limit 20"`.

**New films don't show up on the channel**
Check that ErsatzTV has rescanned (refresh interval / manual scan), that the schedule uses a smart collection
like `tag:Grjngo`, and reset the playout if you want them right away.

**"another run is in progress"**
A previous run is still going (or crashed holding the lock). Check `docker ps`; the lock frees itself when
the process exits.

---

## Design notes

- **Why match before deduping?** The channel re-uploads films under slightly different titles. Matching each
  upload to a TMDB ID makes duplicates trivial to group, and catches alternate titles (Dan Candy's Law =
  Alien Thunder, Guns for Dollars = They Call Me Hallelujah).
- **Why SQLite state instead of yt-dlp's `--download-archive`?** The archive only knows "downloaded this
  video ID". We need to know which *film* each upload is, which copy we kept, and whether a newer upload is
  better.
- **Why a fresh VPN server per run?** YouTube rate-limits and bot-flags IPs. A new server each run (and on every
  block) keeps things moving without cookies or a Google account, which would risk that account instead.
- **Why local folder instead of Plex/Jellyfin for ErsatzTV?** Keeps ~600 Westerns out of the main media
  libraries; ErsatzTV reads the `.nfo` files directly.
