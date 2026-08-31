#!/usr/bin/env bash
# Publish a running preview at https://ghlyms.com/preview/ and take it
# down again.
#
#   sudo ./avian/scripts/preview-expose.sh install [port]   # default 8080
#   sudo ./avian/scripts/preview-expose.sh remove
#   sudo ./avian/scripts/preview-expose.sh status
#
# Why a script rather than "edit the overlay": /etc/caddy/avian-site-overlay.caddy
# already carries the /stream route that keeps live audio working on the
# public host. Hand-editing a file Caddy is actively serving, to add
# something temporary, is how that route gets lost. This appends and
# removes a delimited block and leaves everything else byte-identical -
# and validates the config before reloading, so a mistake never takes the
# site down.
#
# A path on the existing host rather than a subdomain, deliberately: no
# DNS record, no tunnel hostname, no second Access application. The site
# is written with relative URLs throughout, so it runs happily under a
# prefix.
#
# The preview it points at must be started with --expose, which keeps the
# admin password gate on and narrows the API to the read-only endpoints.
# Cloudflare Access still guards the host, so this is only visible to the
# people already on your Access policy.
set -uo pipefail

OVERLAY=/etc/caddy/avian-site-overlay.caddy
BEGIN='# >>> avian preview (temporary)'
END='# <<< avian preview'
# Bumped whenever the block below changes. install writes it; status
# compares it, so a block left over from an older checkout is reported
# rather than quietly serving the old rules - which is invisible
# otherwise, since install only ever writes the block once.
BLOCK_VERSION=3
ACTION="${1:-status}"
PORT="${2:-8080}"

if [ "$ACTION" != status ] && [ "$(id -u)" != 0 ]; then
  echo "run this with sudo" >&2
  exit 1
fi

have_block() { [ -r "$OVERLAY" ] && grep -qF "$BEGIN" "$OVERLAY"; }

# The whole block, in one place, so install and status cannot disagree.
block_text() {
  cat <<EOF
$BEGIN v$BLOCK_VERSION
# Added by avian/scripts/preview-expose.sh. Safe to delete by hand; the
# preview server it points at is a throwaway on localhost.
redir /preview /preview/
handle_path /preview/* {
	reverse_proxy 127.0.0.1:$1 {
		# The admin session cookie is scoped to /avian/, so under this
		# prefix the browser would set it and then never send it back -
		# unlocking would appear to work and silently not. Move it to
		# where the preview actually lives. This also keeps the two
		# sessions apart: same cookie name, different paths, so signing
		# in to the preview cannot log you out of the real site.
		#
		# (?i) is load-bearing. PHP writes the attribute lower case -
		# "path=/avian/" - and Go's regexp is case sensitive, so a
		# capitalised pattern here matches nothing at all and fails
		# exactly the way it would if the line were absent.
		header_down Set-Cookie "(?i)path=/avian/" "path=/preview/avian/"
	}
}
$END
EOF
}

installed_version() {
  [ -r "$OVERLAY" ] || return 1
  sed -n "s/^$(printf '%s' "$BEGIN" | sed 's/[]\/$*.^[]/\\&/g') v\([0-9]*\)$/\1/p" \
    "$OVERLAY" | head -1
}

strip_block() {
  # Delete our delimited block and nothing else. Everything outside the
  # markers is copied through untouched.
  awk -v b="$BEGIN" -v e="$END" '
    index($0, b) { skip = 1 }
    !skip { print }
    index($0, e) { skip = 0 }
  ' "$1"
}

reload() {
  # Validate before reloading. An invalid Caddyfile that is never loaded
  # is a non-event; one that is loaded is an outage.
  if ! caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    echo "the Caddy config does not validate - restoring and stopping" >&2
    return 1
  fi
  systemctl reload caddy
}

case "$ACTION" in
  status)
    if have_block; then
      have=$(installed_version)
      if [ "${have:-0}" != "$BLOCK_VERSION" ]; then
        echo "the published block is from an older version of this script"
        echo "  installed: v${have:-1}   current: v$BLOCK_VERSION"
        echo "  re-run:    sudo $0 install <port>"
        echo
      fi
      echo "published:"
      sed -n "/$(printf '%s' "$BEGIN" | sed 's/[]\/$*.^[]/\\&/g')/,/$(printf '%s' "$END" | sed 's/[]\/$*.^[]/\\&/g')/p" "$OVERLAY"
    else
      echo "not published"
    fi
    [ -r "$OVERLAY" ] && echo && echo "the rest of the overlay:" \
      && strip_block "$OVERLAY" | sed 's/^/  /'
    ;;

  install)
    case "$PORT" in ''|*[!0-9]*) echo "port must be a number" >&2; exit 64 ;; esac
    if ! curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/"; then
      echo "nothing is answering on 127.0.0.1:$PORT" >&2
      echo "start it first:  ./avian/scripts/preview.sh --expose --port $PORT" >&2
      exit 1
    fi

    backup="$OVERLAY.before-preview.$(date +%s)"
    [ -r "$OVERLAY" ] && cp -p "$OVERLAY" "$backup"

    tmp="$(mktemp)"
    { [ -r "$OVERLAY" ] && strip_block "$OVERLAY"; } >"$tmp"
    block_text "$PORT" >>"$tmp"

    install -o root -g caddy -m 0640 "$tmp" "$OVERLAY"
    rm -f "$tmp"

    if ! reload; then
      if [ -n "${backup:-}" ] && [ -r "$backup" ]; then
        install -o root -g caddy -m 0640 "$backup" "$OVERLAY"
        systemctl reload caddy
        echo "rolled back to $backup" >&2
      fi
      exit 1
    fi
    echo "published at /preview/ -> 127.0.0.1:$PORT"
    [ -n "${backup:-}" ] && echo "overlay backed up to $backup"
    echo
    echo "Cloudflare Access still guards the host, so only people on your"
    echo "Access policy can see it. Remove it when you are done:"
    echo "  sudo $0 remove"
    ;;

  remove)
    if ! have_block; then
      echo "not published; nothing to remove"
      exit 0
    fi
    tmp="$(mktemp)"
    strip_block "$OVERLAY" >"$tmp"
    install -o root -g caddy -m 0640 "$tmp" "$OVERLAY"
    rm -f "$tmp"
    reload || exit 1
    echo "removed; the rest of the overlay is untouched"
    ;;

  *)
    echo "usage: $0 install [port] | remove | status" >&2
    exit 64
    ;;
esac
