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
#   ./avian/scripts/preview.sh --expose         # behind ghlyms.com/preview/
#                                               # via avian/scripts/preview-expose.sh
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
ORIGINAL_ARGS="$*"
PORT=8080
PORT_GIVEN=0
DB_SOURCE=""
SEED=0
KEEP=0
# Who the preview thinks is knocking.
#
# admin    - open. The password gate is off, the way a LAN visitor sees the
#            station. Free on localhost, where anyone who can reach
#            127.0.0.1 already has a shell. Refused with --expose.
# password - the gate stays on and you unlock with the station admin
#            password, exactly as on the real site. Needs no Cloudflare
#            configuration at all, so it is what --expose defaults to.
# access   - the gate stays on and the preview signs a Cloudflare Access
#            assertion with a throwaway key, which answers "can the people
#            on my Access policy record without the admin password".
#            With --expose it uses the station's real Access settings, so
#            a genuine assertion from the edge verifies.
# open     - no gate. Allowed with --expose only because --expose narrows
#            the API to endpoints that cannot change the station, so what
#            is reachable is a copy of the database and some read-only
#            views - and Cloudflare Access still decides who gets that
#            far. Use it when the password gate is in the way of simply
#            looking at something.
AS=admin
AS_GIVEN=0
# Exposing the preview through the tunnel is a different risk from running
# it on localhost, so it is a different mode rather than a flag on the
# existing ones. See the block below for what it hardens.
EXPOSE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --port)  PORT="${2:?--port needs a number}"; PORT_GIVEN=1; shift 2 ;;
    --db)    DB_SOURCE="${2:?--db needs a path}"; shift 2 ;;
    --seed)  SEED=1; shift ;;
    --keep)  KEEP=1; shift ;;
    --as)    AS="${2:?--as needs admin, password, access or open}"; AS_GIVEN=1; shift 2 ;;
    --expose) EXPOSE=1; shift ;;
    -h|--help) sed -n '2,/^set -/p' "${BASH_SOURCE[0]}" | sed '$d;s/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

if [ "$EXPOSE" = 1 ] && [ "$AS_GIVEN" = 1 ] && [ "$AS" = admin ]; then
  echo "--expose will not run with --as admin: that turns the password" >&2
  echo "gate off, and the mutating endpoints act on the real station." >&2
  echo "Use --expose on its own; the gate stays on and Cloudflare Access" >&2
  echo "identifies the visitor." >&2
  exit 64
fi

command -v php >/dev/null || { echo "php is not installed" >&2; exit 1; }

# ---- somewhere to listen ----------------------------------------------
# Check before building anything. php -S only discovers a taken port at
# the very end, after the webroot is linked and the database seeded, which
# is a lot of work to throw away over a number nobody chose deliberately.
port_free() {
  php -r '$s = @stream_socket_server("tcp://127.0.0.1:" . $argv[1], $n, $m);
          if ($s) { fclose($s); exit(0); } exit(1);' "$1" 2>/dev/null
}

# Best effort. ss only names the process for ports the caller owns, so
# this is often blank - the port number is the useful part either way.
port_holder() {
  local who=''
  command -v ss >/dev/null \
    && who=$(ss -ltnp "sport = :$1" 2>/dev/null | awk 'NR > 1 { print $NF }' | head -1)
  [ -n "$who" ] && printf '(%s) ' "$who"
}

if ! port_free "$PORT"; then
  if [ "$PORT_GIVEN" = 1 ]; then
    echo "port $PORT is already in use $(port_holder "$PORT")- pick another with --port" >&2
    exit 1
  fi
  # Nobody asked for 8080 specifically - it is only the default, and on a
  # BirdNET-Pi it is often already spoken for. Move along quietly.
  taken=$PORT
  found=0
  for candidate in $(seq $((PORT + 1)) $((PORT + 20))); do
    if port_free "$candidate"; then PORT=$candidate; found=1; break; fi
  done
  if [ "$found" = 0 ]; then
    echo "ports $taken-$((taken + 20)) are all in use; pick one with --port" >&2
    exit 1
  fi
  echo "port $taken is taken $(port_holder "$taken")- using $PORT instead"
fi

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

# ---- reachable from the internet? -------------------------------------
# On localhost, --as admin turning the password gate off costs nothing:
# anyone who can reach 127.0.0.1 already has a shell. Behind the tunnel it
# would cost a great deal, because only birds.db and birdnet.conf are
# redirected for this feature - config.php still writes the *real* station
# config, generate.php still spawns real work. So --expose refuses that
# mode outright and narrows the API surface to the endpoints the site
# needs to render, which are the read-only ones.
if [ "$EXPOSE" = 1 ]; then
  # Default to the password gate: it needs nothing set up in Cloudflare,
  # and the admin password already exists. --as access opts into the
  # identity path instead.
  if [ "$AS_GIVEN" = 0 ]; then
    AS=password
  elif [ "$AS" = access ]; then
    AS=edge
  fi

  # Belt and braces for every exposed mode. Each of these endpoints
  # already guards itself with the admin password, but a preview
  # reachable from the internet should not offer a door to knock on.
  grep -E '^(birdnet-api|submissions|wiki|recording|spectrogram|cutout|menu)\.php$' \
    "$BASE/api-allowlist" >"$BASE/api-allowlist.narrow"
  mv "$BASE/api-allowlist.narrow" "$BASE/api-allowlist"
fi

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
  password)
    # The station's own password gate, unchanged. The one requirement is
    # that this process can read the admin credential state, which is
    # root:caddy 0640 - the same group the real site runs as.
    state=${AV_ADMIN_STATE_FILE:-/var/lib/avian-visitors/admin-auth.state}
    if [ ! -r "$state" ]; then
      me=$(id -un)
      {
        if [ ! -e "$state" ]; then
          echo "--as password needs the station's admin credential state:"
          echo "  $state"
          echo "It is not there at all. Set the admin password from SSH first:"
          echo "    sudo /usr/local/sbin/avian-admin-control password-reset"
          exit 77
        fi
        echo "--as password needs to read"
        echo "  $state"
        echo "which is root:caddy 0640 so that only the web server can see it."
        echo "This shell cannot, so every request would answer 401 no matter"
        echo "what password you typed."
        echo
        echo "Run the preview with that group. Either just for now:"
        echo
        echo "    sudo -u $me -g caddy $0 $ORIGINAL_ARGS"
        echo
        echo "or once and for all, after logging out and back in:"
        echo
        echo "    sudo usermod -aG caddy $me"
        echo
        echo "Nothing is granted beyond reading the file the real site reads."
        echo "(sg caddy does not work here: it asks for a group password that"
        echo "does not exist unless you are already a member.)"
      } >&2
      exit 77
    fi
    unset AV_REQUIRE_AUTH
    WHO="whoever unlocks with the station admin password"
    ;;
  edge)
    # The real team and audience, so a real assertion from the real edge
    # verifies. Nothing else is borrowed from the live config.
    team=$(sed -n 's/^[[:space:]]*ACCESS_TEAM_DOMAIN[[:space:]]*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$REAL_CONF" 2>/dev/null | head -1)
    aud=$(sed -n 's/^[[:space:]]*ACCESS_AUD[[:space:]]*=[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$REAL_CONF" 2>/dev/null | head -1)
    if [ -z "$team" ] || [ -z "$aud" ]; then
      cat >&2 <<'WHY'
--expose --as access needs Cloudflare Access configured, or every request
arrives anonymous and the whole preview answers 401.

    ./avian/scripts/access-setup.sh discover https://ghlyms.com
    sudo ./avian/scripts/access-setup.sh install <team-domain> <aud>

Or drop the --as and use the station admin password instead, which needs
nothing set up:

    ./avian/scripts/preview.sh --expose
WHY
      exit 78
    fi
    printf 'ACCESS_TEAM_DOMAIN="%s"\nACCESS_AUD="%s"\n' "$team" "$aud" >>"$BASE/birdnet.conf"
    unset AV_REQUIRE_AUTH
    WHO="whoever Cloudflare Access lets through to $team"
    ;;
  open)
    if [ "$EXPOSE" != 1 ]; then
      echo "--as open is for --expose; on localhost use --as admin" >&2
      exit 64
    fi
    export AV_REQUIRE_AUTH=0
    WHO="anyone Cloudflare Access lets through - NO station password"
    ;;
  *) echo "--as takes admin, password, access or open" >&2; exit 64 ;;
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
if [ "$EXPOSE" = 1 ]; then
  if [ "${AV_REQUIRE_AUTH:-}" = 0 ]; then
    echo "admin gate OFF; API narrowed to: $(tr '\n' ' ' <"$BASE/api-allowlist")"
  else
    echo "admin gate ON; API narrowed to: $(tr '\n' ' ' <"$BASE/api-allowlist")"
  fi
  echo
  echo "publish it with:  sudo $REPO/avian/scripts/preview-expose.sh install $PORT"
  echo "take it down:     sudo $REPO/avian/scripts/preview-expose.sh remove"
fi
echo "database  $DB  (a copy; the station's own is untouched)"
echo "recordings and uploads land in $ROOT"
echo "ctrl-c to stop"
echo

php -S "127.0.0.1:$PORT" -t "$ROOT" "$REPO/avian/scripts/preview-router.php" 2>&1 \
  | grep -vE 'Accepted|Closing' &
SERVER_PID=$!
wait "$SERVER_PID"
