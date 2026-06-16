#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# seed-all.sh — import all 12 M3U sources in one shot.
#
# Active (serves as winners):
#   11 BuddyChewChew generators (Pluto US/CA, Plex US/CA, Xumo, Tubi,
#   Samsung KR, Airy, TCL, DistroTV US, DistroTV CA) + Local Now (apsatt).
#
# Backup (status='disabled', does NOT serve traffic, NOT pruned):
#   distrotv-proxy container at http://127.0.0.1:8787/playlist.m3u
#
# Idempotent — re-running the script is safe. The engine's UNIQUE
# (source_url) constraint dedupes; re-imports just bump the
# `imports` audit row count and leave the streams alone.
#
# Run on the VPS:
#   cd /opt/treefrogfree
#   bash engine/scripts/seed-all.sh
#
# After this finishes, run a health cycle so the new streams get
# probed and the dead ones get pruned:
#   docker compose -f engine/docker-compose.yml exec tf-engine \
#     python -m engine check-once
#   docker compose -f engine/docker-compose.yml exec tf-engine \
#     python -m engine prune
#   docker compose -f engine/docker-compose.yml exec tf-engine \
#     python -m engine publish
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Use the same compose file + service name everywhere. Operators can
# override via env if their stack lives elsewhere.
: "${COMPOSE_FILE:=engine/docker-compose.yml}"
: "${SERVICE:=tf-engine}"

dc() {
  docker compose -f "$COMPOSE_FILE" exec -T "$SERVICE" python -m engine "$@"
}

# Each row: <label> <url> [extra seed args]
# All BuddyChewChew generators and Local Now are active.
# The distrotv-proxy container M3U is imported with --disable
# (warm backup; sits in the DB ready to be enabled if needed).
SOURCES=(
  "Pluto US|https://raw.githubusercontent.com/BuddyChewChew/pluto/main/pluto_us.m3u"
  "Pluto CA|https://raw.githubusercontent.com/BuddyChewChew/pluto/main/pluto_ca.m3u"
  "Local Now|https://www.apsattv.com/localnow.m3u"
  "Plex US|https://raw.githubusercontent.com/BuddyChewChew/plex-alt-fast-channels/main/playlists/plex_us.m3u"
  "Plex CA|https://raw.githubusercontent.com/BuddyChewChew/plex-alt-fast-channels/main/playlists/plex_ca.m3u"
  "Xumo|https://raw.githubusercontent.com/BuddyChewChew/xumo-playlist-generator/main/playlists/xumo_playlist.m3u"
  "Tubi|https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/main/tubi_playlist.m3u"
  "Samsung TV Plus KR|https://raw.githubusercontent.com/BuddyChewChew/samsungtvplus/main/output/samsung_tvplus.m3u"
  "Airy|https://raw.githubusercontent.com/BuddyChewChew/airy-playlist-generator/main/airy_channels.m3u"
  "TCL|https://raw.githubusercontent.com/BuddyChewChew/tcl-playlist-generator/main/tcl.m3u8"
  "DistroTV US|https://raw.githubusercontent.com/BuddyChewChew/distro-playlist-generator/main/playlists/distrotv_US.m3u"
  "DistroTV CA|https://raw.githubusercontent.com/BuddyChewChew/distro-playlist-generator/main/playlists/distrotv_CA.m3u"
  # Backup: the distrotv-proxy container's M3U, imported as disabled
  # so it doesn't serve but stays ready to be enabled.
  "DistroTV (container backup)|http://127.0.0.1:8787/playlist.m3u"
)

# Pin EPG sources that the M3U files don't bring along themselves.
# These imports don't change between runs (XMLTV is cached locally).
EPG_SOURCES=(
  # Local Now's M3U does NOT include a url-tvg= header, so we pin it.
  "https://github.com/matthuisman/i.mjh.nz/raw/master/LocalNow/us.xml.gz"
  # Samsung TV Plus KR has no EPG in the M3U; pin i.mjh.nz.
  "https://github.com/matthuisman/i.mjh.nz/raw/master/SamsungTV/kr.xml.gz"
  # DistroTV's M3U also ships its EPG separately.
  "https://raw.githubusercontent.com/BuddyChewChew/distro-playlist-generator/main/playlists/distrotv.xml"
)

# EPG imports only need to happen once — but importing is idempotent
# and the engine re-imports only if the source is stale. Cheaper to
# always run them than to track "did we already do this".
run_epg() {
  local url="$1"
  echo ""
  echo "▶ EPG import: $url"
  if ! dc epg-import --url "$url" --publish-nownext; then
    echo "  ! EPG import failed for $url — continuing (operator can retry)"
  fi
}

echo "════════════════════════════════════════════════════════════════"
echo " Tree Frog Streams — bulk seed"
echo " $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "════════════════════════════════════════════════════════════════"
echo ""

ok=0
fail=0
backup=0
for row in "${SOURCES[@]}"; do
  label="${row%%|*}"
  url="${row#*|}"
  is_backup=0
  extra=()
  if [[ "$label" == *"(container backup)"* ]]; then
    is_backup=1
    extra=(--disable)
  fi
  echo "────────────────────────────────────────────────────────────────"
  echo "▶ $label"
  echo "  URL:  $url"
  if (( is_backup )); then
    echo "  mode: DISABLED BACKUP (won't serve, won't be pruned)"
    backup=$((backup+1))
  else
    echo "  mode: ACTIVE"
  fi
  echo ""
  if dc seed --m3u "$url" --label "$label" "${extra[@]}"; then
    ok=$((ok+1))
  else
    fail=$((fail+1))
    echo "  ! FAILED — continuing (operator can retry this URL)"
  fi
done

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " EPG imports"
echo "════════════════════════════════════════════════════════════════"
for url in "${EPG_SOURCES[@]}"; do
  run_epg "$url"
done

echo ""
echo "════════════════════════════════════════════════════════════════"
echo " Summary"
echo "════════════════════════════════════════════════════════════════"
echo "  Active sources imported:  $((ok - backup)) / $((ok + fail - backup))"
echo "  Backup sources imported:  $backup"
echo "  Failures:                 $fail"
echo ""
echo " Next steps:"
echo "   1. Probe: docker compose -f $COMPOSE_FILE exec -T $SERVICE \\"
echo "             python -m engine check-once"
echo "   2. Sweep dead labels:"
echo "             python -m engine prune"
echo "   3. Republish:"
echo "             python -m engine publish"
echo "   4. Verify: docker compose -f $COMPOSE_FILE exec -T $SERVICE \\"
echo "             python -m engine stats"
echo ""
if (( fail > 0 )); then
  echo "  ⚠ $fail source(s) failed. Common causes:"
  echo "    - 404 (M3U URL changed in the BuddyChewChew repo)"
  echo "    - Network from VPS blocked (geo-blocked by source CDN)"
  echo "    - Malformed M3U (the parser will return 0 entries — the"
  echo "      pruner's empty-label sweep will annotate the import"
  echo "      row on the next cycle)"
  exit 1
fi
