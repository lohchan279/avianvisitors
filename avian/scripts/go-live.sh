#!/usr/bin/env bash
# Put the current checkout live, and prove it actually landed.
#
#   ./avian/scripts/go-live.sh              # check only, changes nothing
#   sudo ./avian/scripts/go-live.sh --apply # link, bump, then check
#
# Deploying this fork is four steps and every one of them has failed
# quietly at least once:
#
#   - a pull swaps the live frontend the instant it finishes, because the
#     webroot is symlinks into the checkout - there is no release step;
#   - a file new to the manifest is not linked, so it 404s on the site
#     while working perfectly from the checkout;
#   - the cache token has to move, or browsers keep the old scripts;
#   - the worker is a service, and nothing scores a field recording until
#     it is installed.
#
# So this checks the *served* site rather than the repository. Reading
# files off disk would agree with itself and tell you nothing.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SITE=${AV_SITE:-http://localhost}
APPLY=0
FAILED=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

say()  { printf '  %-46s %s\n' "$1" "$2"; }
good() { say "$1" "ok${2:+  $2}"; }
bad()  { say "$1" "NO   $2"; FAILED=$((FAILED + 1)); }

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$SITE$1"; }
body() { curl -s --max-time 8 "$SITE$1"; }

# ---- apply ------------------------------------------------------------
if [ "$APPLY" = 1 ]; then
  [ "$(id -u)" = 0 ] || { echo "--apply needs sudo" >&2; exit 1; }
  owner=$(stat -c '%U' "$REPO")

  echo "publishing the webroot manifest"
  "$REPO/scripts/link_webroot.sh" || { echo "link_webroot failed" >&2; exit 1; }

  # The token must move or browsers keep the old apt.js. Run it as the
  # repo owner so the high-water file does not end up owned by root.
  echo "bumping the cache token"
  sudo -u "$owner" "$REPO/avian/scripts/bump_version.sh" | sed 's/^/  /'

  # The frontend swaps the instant the pull lands. The worker is the
  # opposite: it goes on running whatever Python it started with, so a
  # deploy that changes how recordings are scored leaves the old scorer
  # in memory indefinitely.
  if systemctl is-active --quiet submission_worker 2>/dev/null; then
    echo "restarting the field-recording worker"
    systemctl restart submission_worker
  fi
  echo
fi

echo "checking $SITE"

# ---- the frontend -----------------------------------------------------
index=$(body /)
if [ -z "$index" ]; then
  bad "the site answers" "nothing came back from $SITE"
else
  good "the site answers"
  case "$index" in
    *'data-i="3">map'*) good "the Map tab is published" ;;
    *) bad "the Map tab is published" "index.html has no map button" ;;
  esac

  # The token the site serves against the token in the checkout. These
  # differ when a pull landed but nothing bumped, which browsers then
  # paper over with a cached apt.js.
  served=$(printf '%s' "$index" | grep -oE '\?v=r[0-9]+' | head -1)
  ondisk=$(grep -oE '\?v=r[0-9]+' "$REPO/avian/frontend/index.html" | head -1)
  if [ -n "$served" ] && [ "$served" = "$ondisk" ]; then
    good "the cache token matches the checkout" "${served#\?v=}"
  else
    bad "the cache token matches the checkout" "served ${served:-none}, checkout ${ondisk:-none}"
  fi

  # Compared here rather than in the asset loop below, because "/" is what
  # a browser actually asks for and /index.html can be a redirect. Compared
  # as strings rather than by digest: $( ) has already eaten the trailing
  # newline off the response, so it has to eat it off the file too, and a
  # digest taken either side of that would never agree.
  if [ "$index" = "$(cat "$REPO/avian/frontend/index.html")" ]; then
    good "/ matches the checkout"
  else
    bad "/ matches the checkout" "the served index.html is not the checkout's"
  fi
fi

# Not "does it answer 200" - that passes for a stale copy, a leftover
# regular file shadowing the link, or anything else that happens to sit
# at the path. Compare the bytes. This is the check that separates "the
# station is serving the old script" from "the station is fine and the
# staleness is downstream", and without it that question cannot be
# settled from here at all.
digest() { md5sum | cut -d' ' -f1; }
for asset in apt.js styles.css field.js field.css sg-map.js; do
  status=$(code "/$asset")
  if [ "$status" != 200 ]; then
    bad "/$asset is served" "$status - add it to the manifest in scripts/link_webroot.sh, then --apply"
    continue
  fi
  live=$(body "/$asset" | digest)
  disk=$(digest <"$REPO/avian/frontend/$asset")
  if [ "$live" = "$disk" ]; then
    good "/$asset matches the checkout"
  else
    bad "/$asset matches the checkout" \
      "served copy differs from avian/frontend/$asset - re-run with --apply"
  fi
done

# grep -q closes the pipe at the first match and pipefail then reports the
# whole thing as failed. Same trap as the worker check below.
if [ "$(body /sg-map.js | grep -c 'AVIAN_SG_MAP')" -gt 0 ]; then
  good "the map data is the real thing"
else
  bad "the map data is the real thing" "sg-map.js does not define AVIAN_SG_MAP"
fi

# ---- the API ----------------------------------------------------------
# 401 is a pass: it proves the endpoint is routed and guarded. 404 is the
# failure, and it means the Caddy allowlist has not been regenerated.
status=$(code "/avian/api/submissions.php?action=list")
case "$status" in
  200|401) good "the submissions endpoint is routed" "$status" ;;
  404) bad "the submissions endpoint is routed" "404 - run sudo /usr/local/sbin/avian-caddy-refresh" ;;
  *)   bad "the submissions endpoint is routed" "$status" ;;
esac

status=$(code /avian/api/sg-areas.php)
if [ "$status" = 404 ]; then
  good "the boundary data stays server-side" "404"
else
  bad "the boundary data stays server-side" "$status - it should not be reachable at all"
fi

# ---- the worker -------------------------------------------------------
# grep -q on a pipe is the obvious spelling and it is wrong here. It exits
# at the first match, systemctl dies of SIGPIPE writing the rest of the
# list, and pipefail turns that into a failed test - so a worker that is
# installed, enabled and has been running for a day gets reported as "not
# installed". Count instead: grep -c reads to the end, and the status
# being tested is grep's own.
installed=$(systemctl list-unit-files 2>/dev/null | grep -c '^submission_worker\.service')
if [ "${installed:-0}" -gt 0 ]; then
  if systemctl is-active --quiet submission_worker; then
    good "the field-recording worker is running"

    # A pull replaces the worker's source; the running process keeps the
    # code it started with, and systemd has no reason to notice. The
    # station then sits there writing statuses the new frontend does not
    # understand, and every recording comes back as an error - with the
    # worker green in every other check.
    started=$(date -d "$(systemctl show submission_worker -p ExecMainStartTimestamp --value 2>/dev/null)" +%s 2>/dev/null)
    changed=$(stat -c %Y "$REPO/scripts/submission_worker.py" 2>/dev/null)
    if [ -n "$started" ] && [ -n "$changed" ] && [ "$changed" -gt "$started" ]; then
      bad "the worker is running the current code" \
        "it started before the last change to submission_worker.py - sudo systemctl restart submission_worker"
    else
      good "the worker is running the current code"
    fi
  else
    bad "the field-recording worker is running" \
      "installed but not active - sudo systemctl start submission_worker"
  fi
else
  bad "the field-recording worker is running" \
    "not installed - sudo $REPO/scripts/install_submission_worker.sh"
fi

# ---- where the station thinks it is -----------------------------------
# Not a pass or a fail: the coordinates are the owner's to set, and the
# occurrence filter tolerates a few kilometres. The map does not - it puts
# Home in whichever district the coordinates land in, so a rough position
# set years ago shows up as a station in the wrong part of town.
where=$(php -r '
  require "'"$REPO"'/avian/api/places.php";
  $conf = "/etc/birdnet/birdnet.conf";
  $get = static function (string $key) use ($conf): ?float {
      if (!is_readable($conf)) return null;
      foreach (file($conf, FILE_IGNORE_NEW_LINES) as $line) {
          if (preg_match("/^\s*" . $key . "\s*=\s*\"?([^\"]*)\"?\s*$/", $line, $m)) {
              return is_numeric(trim($m[1])) ? (float)trim($m[1]) : null;
          }
      }
      return null;
  };
  $lat = $get("LATITUDE"); $lon = $get("LONGITUDE");
  if ($lat === null || $lon === null) { echo "no coordinates in birdnet.conf"; exit; }
  $area = avian_area_at($lat, $lon);
  printf("%s", $area ?? "nowhere this map knows about");
' 2>/dev/null)
say "the station sits in" "${where:-unknown}"

# ---- one config file, not two -----------------------------------------
# ~/BirdNET-Pi/birdnet.conf is meant to be a symlink to the file in /etc.
# When something replaces it with a real copy the two drift apart, and
# nothing says so: the settings panel writes /etc, half the API reads
# /etc, the other half reads the copy. A key saved in Settings then works
# in one place and not another, and the file that is wrong is the one
# that stopped being updated - which is invisible from the outside.
etc_conf=/etc/birdnet/birdnet.conf
repo_conf="$REPO/birdnet.conf"
if [ -e "$etc_conf" ] && [ -e "$repo_conf" ]; then
  if [ "$(readlink -f "$etc_conf")" = "$(readlink -f "$repo_conf")" ]; then
    good "one config file, not two"
  else
    bad "one config file, not two" \
      "$repo_conf is a separate copy, not a link to $etc_conf - back it up and run: ln -sfn $etc_conf $repo_conf"
  fi
fi

# ---- who may record ---------------------------------------------------
if "$REPO/avian/scripts/access-setup.sh" check >/dev/null 2>&1; then
  good "Cloudflare Access identities may record"
else
  say "Cloudflare Access identities may record" \
    "off - only the admin password works; $REPO/avian/scripts/access-setup.sh discover $SITE"
fi

# ---- leftovers --------------------------------------------------------
if [ -r /etc/caddy/avian-site-overlay.caddy ] \
  && grep -q 'avian preview' /etc/caddy/avian-site-overlay.caddy; then
  say "no preview left published" \
    "still up - sudo $REPO/avian/scripts/preview-expose.sh remove"
fi

echo
if [ "$APPLY" = 1 ]; then
  # Unconditional, and above the verdict. The token moved the moment we
  # bumped it, so from then on the edge is serving a stale index.html -
  # and an unrelated check failing below is no reason to swallow the one
  # instruction that decides whether anybody sees the new frontend. This
  # reminder used to live in the all-clear branch, where a single
  # unrelated NO hid it and the deploy looked done while every visitor
  # kept the old scripts.
  echo "Now purge the Cloudflare cache for the public site. Until you do,"
  echo "visitors keep the cached index.html and with it the old scripts."
  echo
fi
if [ "$FAILED" = 0 ]; then
  echo "live."
else
  echo "$FAILED check(s) failed; each line above says what to run."
  exit 1
fi
