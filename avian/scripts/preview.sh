#!/usr/bin/env bash
# Run the whole site against throwaway data, touching nothing the station
# reads or serves.
#
# Worth having because the obvious way to try a change is the dangerous
# one. The production webroot is a set of symlinks *into this checkout*,
# so on the Pi a plain `git pull` swaps the live index.html, apt.js and
# stylesheets the instant it finishes - no deploy step, no undo. And the
# submissions API adds columns to birds.db, which is the same database the
# collage, the stats and the BirdWeather export read.
#
# So this builds a second webroot in a scratch directory, points the API
# at a *copy* of birds.db and a synthetic birdnet.conf, and serves the lot
# on a local port with PHP's built-in server:
#
#   ./avian/scripts/preview.sh                  # port 8080, empty database
#   ./avian/scripts/preview.sh --db ~/BirdNET-Pi/scripts/birds.db
#                                               # copy of the real data
#   ./avian/scripts/preview.sh --port 8099 --seed
#                                               # invent some field catches
#   ./avian/scripts/preview.sh --as access      # arrive as a Cloudflare
#                                               # Access visitor, no admin
#                                               # password, real signature
#
# Safe to run on the Pi itself. It never writes inside the repository, the
# real webroot, the real recordings or the real database - the copy is
# made once at startup and everything after that lands in the scratch
# directory, which --keep will leave behind for you to inspect.
#
# What it cannot do is score a recording: that needs the BirdNET model, so
# the worker is a separate exercise. See --seed for the alternative.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT=8080
DB_SOURCE=""
SEED=0
KEEP=0
# admin - open, the way a LAN visitor sees the station with the gate off.
# access - the admin gate stays on and the preview signs a Cloudflare
#          Access assertion for every request, so what gets in is a
#          verified identity and nothing else. That is the mode that
#          answers "can the people on my Access policy record without
#          being given the admin password".
AS=admin

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)  PORT="${2:?--port needs a number}"; shift 2 ;;
    --db)    DB_SOURCE="${2:?--db needs a path}"; shift 2 ;;
    --seed)  SEED=1; shift ;;
    --keep)  KEEP=1; shift ;;
    --as)    AS="${2:?--as needs admin or access}"; shift 2 ;;
    -h|--help) sed -n '2,27p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

command -v php >/dev/null || { echo "php is not installed" >&2; exit 1; }

BASE="$(mktemp -d "${TMPDIR:-/tmp}/avian-preview-XXXXXX")"
ROOT="$BASE/webroot"
mkdir -p "$ROOT"

cleanup() {
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null
  if [ "$KEEP" = 1 ]; then
    echo
    echo "scratch directory kept at $BASE"
  else
    rm -rf "$BASE"
  fi
}
trap cleanup EXIT INT TERM

# ---- the webroot, from the real manifest -----------------------------
# Read out of link_webroot.sh rather than restated, so a file the manifest
# forgets is missing here too. That is the bug this preview exists to
# catch: it 404s in production while working from a checkout.
LINK="$REPO/scripts/link_webroot.sh"
readarray -t SOURCES < <(sed -n '/^  sources=(/,/^  )/p' "$LINK" | sed -n 's/^ *"\(.*\)"$/\1/p')
readarray -t TARGETS < <(sed -n '/^  targets=(/,/^  )/p' "$LINK" | sed -n 's/^ *"\(.*\)"$/\1/p')

if [ "${#SOURCES[@]}" -eq 0 ] || [ "${#SOURCES[@]}" -ne "${#TARGETS[@]}" ]; then
  echo "could not read the webroot manifest from $LINK" >&2
  exit 1
fi

: >"$BASE/manifest"
missing=0
for i in "${!SOURCES[@]}"; do
  src="${SOURCES[$i]}"
  src="${src//\$\{repo_dir\}/$REPO}"
  src="${src//\$\{frontend_dir\}/$REPO/avian/frontend}"
  if [ ! -e "$src" ]; then
    echo "manifest names a file that does not exist: $src" >&2
    missing=1
    continue
  fi
  ln -sfn "$src" "$ROOT/${TARGETS[$i]}"
  echo "${TARGETS[$i]}" >>"$BASE/manifest"
done
[ "$missing" = 0 ] || exit 1

# ---- the API allowlist, from the real Caddy generator -----------------
sed -n 's#.*not path /avian/api/\(.*\)#\1#p' "$REPO/scripts/update_caddyfile.sh" \
  | head -1 | tr ' ' '\n' | sed 's#^/avian/api/##' | grep '\.php$' \
  >"$BASE/api-allowlist"
[ -s "$BASE/api-allowlist" ] || { echo "could not read the API allowlist" >&2; exit 1; }

# ---- throwaway data ---------------------------------------------------
DB="$BASE/birds.db"
if [ -n "$DB_SOURCE" ]; then
  [ -r "$DB_SOURCE" ] || { echo "cannot read $DB_SOURCE" >&2; exit 1; }
  # sqlite3 .backup would be tidier, but a plain copy needs no extra
  # binary and a preview does not care about a torn WAL frame.
  cp "$DB_SOURCE" "$DB"
  for extra in "$DB_SOURCE-wal" "$DB_SOURCE-shm"; do
    [ -r "$extra" ] && cp "$extra" "$DB${extra##*"$DB_SOURCE"}"
  done
  chmod u+w "$DB"
else
  php -r '$d=new PDO("sqlite:".$argv[1]);
    $d->exec("CREATE TABLE IF NOT EXISTS detections (Date TEXT, Time TEXT, Sci_Name TEXT,
              Com_Name TEXT, Confidence REAL, Lat REAL, Lon REAL, Cutoff REAL, Week INT,
              Sens REAL, Overlap REAL, File_Name TEXT)");' "$DB"
fi

# A synthetic config: the station's own coordinates so "Home" resolves,
# and EXTRACTED pointed at the scratch webroot so uploaded clips land
# there instead of in the real recordings tree.
REAL_CONF=/etc/birdnet/birdnet.conf
lat=1.3690; lon=103.8480
if [ -r "$REAL_CONF" ]; then
  lat=$(sed -n 's/^[[:space:]]*LATITUDE[[:space:]]*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$REAL_CONF" | head -1)
  lon=$(sed -n 's/^[[:space:]]*LONGITUDE[[:space:]]*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$REAL_CONF" | head -1)
  [ -n "$lat" ] || lat=1.3690
  [ -n "$lon" ] || lon=103.8480
fi
cat >"$BASE/birdnet.conf" <<EOF
SITE_NAME="preview"
LATITUDE=$lat
LONGITUDE=$lon
EXTRACTED="$ROOT"
FIELD_MIN_CONFIDENCE=0.5
EOF

# ---- who the preview arrives as ---------------------------------------
case "$AS" in
  admin)
    export AV_REQUIRE_AUTH=0
    WHO="an admin on the LAN (the password gate is off)"
    ;;
  access)
    command -v openssl >/dev/null || { echo "--as access needs openssl" >&2; exit 1; }
    TEAM="preview.cloudflareaccess.com"
    AUD="preview-audience-tag"
    openssl req -x509 -newkey rsa:2048 -keyout "$BASE/access.key" -out "$BASE/access.crt" \
      -days 1 -nodes -subj "/CN=$TEAM" >/dev/null 2>&1 \
      || { echo "could not generate a preview signing key" >&2; exit 1; }
    php -r 'file_put_contents($argv[1], json_encode(["preview" => file_get_contents($argv[2])]));' \
      "$BASE/access-certs.json" "$BASE/access.crt"
    printf 'ACCESS_TEAM_DOMAIN="%s"\nACCESS_AUD="%s"\n' "$TEAM" "$AUD" >>"$BASE/birdnet.conf"
    export AV_PREVIEW_ACCESS_KEY="$BASE/access.key"
    export AV_PREVIEW_ACCESS_TEAM="$TEAM"
    export AV_PREVIEW_ACCESS_AUD="$AUD"
    export AV_PREVIEW_ACCESS_EMAIL="${AV_PREVIEW_ACCESS_EMAIL:-preview@example.com}"
    # Deliberately NOT set: the admin gate stays on, so anything that gets
    # through did so on the strength of the signature.
    unset AV_REQUIRE_AUTH
    WHO="a Cloudflare Access visitor, $AV_PREVIEW_ACCESS_EMAIL (no admin password)"
    ;;
  *) echo "--as takes admin or access" >&2; exit 64 ;;
esac

if [ "$SEED" = 1 ]; then
  php "$REPO/avian/scripts/preview-seed.php" "$DB" "$ROOT" || exit 1
fi

# ---- serve ------------------------------------------------------------
export AV_PREVIEW_ROOT="$ROOT"
export AV_DB_FILE="$DB"
export AV_BIRDNET_CONF="$BASE/birdnet.conf"
export AV_ACCESS_CONF="$BASE/birdnet.conf"
export AV_ACCESS_CERTS="$BASE/access-certs.json"
export PHP_CLI_SERVER_WORKERS=4

echo "preview   http://127.0.0.1:$PORT"
echo "arriving as $WHO"
echo "database  $DB  (a copy; the station's own is untouched)"
echo "recordings and uploads land in $ROOT"
echo "ctrl-c to stop"
echo

php -S "127.0.0.1:$PORT" -t "$ROOT" "$REPO/avian/scripts/preview-router.php" 2>&1 \
  | grep -vE 'Accepted|Closing' &
SERVER_PID=$!
wait "$SERVER_PID"
