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

[ -r "$INDEX" ] || { echo "error: cannot read $INDEX" >&2; exit 2; }

# The highest rN currently referenced. Mixed tokens are normal on the first
# run - upstream ships several - and this collapses them onto one.
current=$(grep -oE '\?v=r[0-9]+' "$INDEX" | grep -oE '[0-9]+' | sort -n | tail -1)
if [ -z "$current" ]; then
  echo "error: no ?v=rN tokens found in index.html" >&2
  exit 1
fi

distinct=$(grep -oE '\?v=r[0-9]+' "$INDEX" | sort -u | tr '\n' ' ')
if [ "${1:-}" = "--check" ]; then
  echo "current: r$current"
  echo "tokens in use: $distinct"
  grep -c '?v=r' "$INDEX" | xargs printf "versioned refs: %s\n"
  exit 0
fi

if [ -n "${1:-}" ]; then
  next="${1#r}"
  case "$next" in
    ''|*[!0-9]*) echo "error: version must look like r27" >&2; exit 2 ;;
  esac
else
  next=$((current + 1))
fi

if [ "$next" -lt "$current" ]; then
  echo "warning: r$next is older than the current r$current - browsers that" >&2
  echo "         already cached r$current will not refetch." >&2
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

echo "now hard-refresh (Ctrl-Shift-R), and purge the Cloudflare cache for the public site"
