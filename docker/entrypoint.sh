#!/bin/sh
set -e
# YouTube changes often; yt-dlp nightly usually has the fix first. Update every run (through the VPN).
pip install --no-cache-dir -q -U --pre "yt-dlp[default]" >/dev/null 2>&1 \
  || echo "warning: yt-dlp update failed, using $(yt-dlp --version)"
echo "yt-dlp $(yt-dlp --version), public IP $(python -c 'import requests;print(requests.get("https://ipinfo.io/ip",timeout=10).text.strip())' 2>/dev/null || echo unknown)"

mkdir -p /opt/grjngo/data/cache /opt/grjngo/staging
chown -R "${PUID:-1000}:${PGID:-1000}" /opt/grjngo/data /opt/grjngo/staging 2>/dev/null || true

# Drop root so files on the NAS get the right owner.
exec setpriv --reuid="${PUID:-1000}" --regid="${PGID:-1000}" --clear-groups python -m grjngo "$@"
