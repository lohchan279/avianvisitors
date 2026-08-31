#!/usr/bin/env bash
# Bump the frontend cache-busting token.
#
# Every versioned asset in index.html carries the same ?v=rN token, and
# apt.js reads its SKETCH_VERSION and IMG_VERSION from its own script tag.
# So one edit here invalidates the browser's copy of the scripts, the
# stylesheets, the mask tables and every illustration URL at once.
#
# Run it after anything that changes pixels - a new illustration, a re-cut,
# a rebuilt mask table - or after editing the frontend.
#
#   ./bump_version.sh          # r26 -> r27
#   ./bump_version.sh r31      # set it explicitly
#   ./bump_version.sh --check  # report the current token, change nothing
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX="$HERE/../frontend/index.html"
# Highest token ever issued from this checkout. index.html is tracked, so a
# branch switch rewrites it to whatever token that branch carries - which can
# be lower than one already published. Reissuing a token means every cache
# holding the old response keeps serving it, and the change appears not to
# have happened. This file is untracked, so it survives branch switches and
# ratchets the token forward.
HIGHFILE="$HERE/../frontend/.cache-token-high"

[ -r "$INDEX" ] || { echo "error: cannot read $INDEX" >&2; exit 2; }

# The highest rN currently referenced. Mixed tokens are normal on the first
# run - upstream ships several - and this collapses them onto one.
current=$(grep -oE '\?v=r[0-9]+' "$INDEX" | grep -oE '[0-9]+' | sort -n | tail -1)
if [ -z "$current" ]; then
  echo "error: no ?v=rN tokens found in index.html" >&2
  exit 1
fi

distinct=$(grep -oE '\?v=r[0-9]+' "$INDEX" | sort -u | tr '\n' ' ')

high=0
if [ -r "$HIGHFILE" ]; then
  high=$(tr -cd '0-9' <"$HIGHFILE")
  [ -n "$high" ] || high=0
fi

if [ "${1:-}" = "--check" ]; then
  echo "current: r$current   (highest ever issued: r$high)"
  [ "$high" -gt "$current" ] && echo "note: index.html is BEHIND the high-water mark - a plain bump would reissue a used token"
  echo "tokens in use: $distinct"
  grep -c '?v=r' "$INDEX" | xargs printf "versioned refs: %s\n"
  exit 0
fi

force=0
if [ "${1:-}" = "--force" ]; then force=1; shift; fi

if [ -n "${1:-}" ]; then
  next="${1#r}"
  case "$next" in
    ''|*[!0-9]*) echo "error: version must look like r27" >&2; exit 2 ;;
  esac
else
  # Ratchet: step past the high-water mark, not merely past what this
  # branch happens to carry.
  base=$current
  [ "$high" -gt "$base" ] && base=$high
  next=$((base + 1))
  [ "$high" -gt "$current" ] && \
    echo "note: index.html carried r$current but r$high was already issued; going to r$next"
fi

if [ "$next" -le "$high" ] && [ "$force" != 1 ]; then
  echo "error: r$next has already been issued (high-water mark r$high)." >&2
  echo "       Reissuing it serves whatever caches still hold that response," >&2
  echo "       so the change will look like it did not happen." >&2
  echo "       Pick a higher number, or --force if you truly mean it." >&2
  exit 2
fi

sed -i -E "s/\?v=r[0-9]+/?v=r${next}/g" "$INDEX"

echo "r$current -> r$next   ($(grep -c '?v=r' "$INDEX") refs in index.html)"
[ "$(echo "$distinct" | wc -w)" -gt 1 ] && echo "(collapsed mixed tokens: $distinct)"

# apt.js should no longer carry a literal - if it does, something has
# reintroduced one and the bump above will not reach it.
if grep -qE "var (SKETCH|IMG)_VERSION = 'r[0-9]+'" "$HERE/../frontend/apt.js" 2>/dev/null; then
  echo "warning: apt.js still has a hardcoded version literal; it will not" >&2
  echo "         pick up this bump. Expected 'var SKETCH_VERSION = ASSET_VERSION;'" >&2
fi

printf '%s\n' "$next" >"$HIGHFILE" 2>/dev/null \
  || echo "warning: could not record the high-water mark in $HIGHFILE" >&2

echo "now hard-refresh (Ctrl-Shift-R), and purge the Cloudflare cache for the public site"

# index.html is tracked, so bumping it leaves the working tree dirty and
# the next `git pull` refuses to run. Say so here rather than leaving it
# to be discovered halfway through an update. Discarding the edit is safe:
# the token itself lives in the untracked high-water file above, so a bump
# after the pull reissues a correct one.
if command -v git >/dev/null \
  && git -C "$HERE/../.." rev-parse --git-dir >/dev/null 2>&1 \
  && ! git -C "$HERE/../.." diff --quiet -- avian/frontend/index.html 2>/dev/null; then
  echo
  echo "note: index.html is tracked, so it now shows as modified and a"
  echo "      git pull will refuse. Before the next pull:"
  echo "          git checkout -- avian/frontend/index.html"
  echo "      then run this script again afterwards. Nothing is lost - the"
  echo "      high-water mark is in $(basename "$HIGHFILE"), which is untracked."
fi
