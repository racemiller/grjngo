#!/usr/bin/env bash
# Scheduled entry point. Each attempt gets a fresh NordVPN server; if YouTube blocks the IP
# (worker exits 75) we tear the tunnel down, pick a new server, and carry on where we left off.
set -uo pipefail
cd "$(dirname "$0")"

MAX_TRIES="${MAX_TRIES:-4}"
exec 9>/tmp/grjngo.lock
flock -n 9 || { echo "another run is in progress"; exit 0; }

wait_healthy() {
  for _ in $(seq 1 60); do
    s=$(docker inspect -f '{{.State.Health.Status}}' grjngo-gluetun 2>/dev/null || echo missing)
    [ "$s" = healthy ] && return 0
    sleep 5
  done
  echo "VPN never became healthy:"; docker logs --tail 30 grjngo-gluetun
  return 1
}

rc=1
extra=()
for try in $(seq 1 "$MAX_TRIES"); do
  echo "=== $(date '+%F %T') attempt $try/$MAX_TRIES: new VPN connection"
  docker compose up -d --force-recreate gluetun >/dev/null
  if ! wait_healthy; then rc=1; continue; fi
  docker compose run --rm worker pipeline "${extra[@]}" "$@"
  rc=$?
  [ "$rc" -ne 75 ] && break
  echo "YouTube blocked this server; rotating."
  extra=(--no-scan)
  sleep 30
done

docker compose down >/dev/null 2>&1
echo "=== $(date '+%F %T') finished (exit $rc)"
exit "$rc"
