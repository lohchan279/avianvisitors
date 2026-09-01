#!/usr/bin/env bash
# Find and install the two Cloudflare Access settings the field-recording
# API needs, and check they work.
#
#   ./avian/scripts/access-setup.sh check [url] # what is configured now,
#                                               # cross-checked against the
#                                               # edge if a url is given
#   ./avian/scripts/access-setup.sh discover https://ghlyms.com
#   ./avian/scripts/access-setup.sh read        # read them off a real token
#   sudo ./avian/scripts/access-setup.sh install <team-domain> <aud>
#
# The two values are:
#
#   ACCESS_TEAM_DOMAIN   yourteam.cloudflareaccess.com - whose signature to
#                        trust. Zero Trust dashboard -> Settings -> Custom
#                        Pages, or the host your login redirect passes
#                        through.
#   ACCESS_AUD           the Application Audience tag of the Access
#                        application in front of this site. Zero Trust ->
#                        Access -> Applications -> your app -> Overview.
#                        A 64-character hex string.
#
# Both are needed. With either missing, Access authentication is simply
# off and the API falls back to the station's admin password.
#
# The AUD tag matters more than it looks: it is what stops a valid token
# for a *different* application in the same Access team from opening this
# one. Copy it, do not guess it - which is what `read` is for.
#
# `read` takes a real assertion and tells you both values from it, so
# there is nothing to transcribe. Get one from a browser already signed in
# to the site: devtools -> Application -> Cookies -> CF_Authorization.
# It is pasted on stdin rather than passed as an argument, because an
# argument is visible to every process on the machine, and it is never
# echoed back.
set -uo pipefail

CONF=${AV_ACCESS_CONF:-/etc/birdnet/birdnet.conf}
ACTION="${1:-check}"

command -v php >/dev/null || { echo "php is not installed" >&2; exit 1; }

conf_value() {
  [ -r "$CONF" ] || return 1
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\{0,1\}\([^\"]*\)\"\{0,1\}[[:space:]]*$/\1/p" \
    "$CONF" | head -1
}

valid_team() { [[ "$1" =~ ^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$ ]]; }
valid_aud()  { [[ "$1" =~ ^[A-Za-z0-9_-]{16,128}$ ]]; }

# Where an unauthenticated request to a protected site gets bounced to.
# The host is the team domain and the kid parameter is the application's
# audience, so this one lookup answers both "what are the values" and
# "are the values I installed the right ones".
login_redirect() {
  curl -sS -o /dev/null -D - --max-time 15 "$1" 2>/dev/null \
    | tr -d '\r' | sed -n 's/^[Ll]ocation:[[:space:]]*//p' | head -1
}

redirect_field() {
  php -r '
    $url = trim(stream_get_contents(STDIN));
    if ($argv[1] === "host") { echo parse_url($url, PHP_URL_HOST) ?: ""; exit; }
    parse_str((string)parse_url($url, PHP_URL_QUERY), $query);
    echo (string)($query["kid"] ?? "");
  ' "$2" <<<"$1"
}

case "$ACTION" in
  discover)
    # An unauthenticated request to a protected site is bounced to the
    # team's login page, and that redirect names both values: the host is
    # the team domain, and Access carries the application's audience tag
    # in the query string. No sign-in, no cookie, no devtools.
    url="${2:-}"
    [ -n "$url" ] || { echo "usage: $0 discover https://your.site" >&2; exit 64; }
    location=$(login_redirect "$url")
    if [ -z "$location" ]; then
      echo "no redirect from $url - is it actually behind Access?" >&2
      echo "If you are already signed in, your browser may be sending a" >&2
      echo "cookie; try again from a private window, or use: $0 read" >&2
      exit 1
    fi
    php -r '
      $url = trim(stream_get_contents(STDIN));
      $host = parse_url($url, PHP_URL_HOST) ?: "";
      if (!str_contains($host, "cloudflareaccess.com")) {
        fwrite(STDERR, "the redirect does not point at Cloudflare Access:\n  $url\n");
        exit(1);
      }
      parse_str((string)parse_url($url, PHP_URL_QUERY), $query);
      printf("ACCESS_TEAM_DOMAIN=%s\n", $host);
      $aud = (string)($query["kid"] ?? "");
      if ($aud !== "") {
        printf("ACCESS_AUD=%s\n", $aud);
      } else {
        fwrite(STDERR, "the redirect carried no audience tag; take it from\n"
          . "Zero Trust -> Access -> Applications -> your app -> Overview.\n");
      }
    ' <<<"$location" || exit $?
    echo >&2
    echo "Confirm the audience against the application Overview page if you" >&2
    echo "have more than one Access application, then:" >&2
    echo "  sudo $0 install <team-domain> <aud>" >&2
    ;;

  read)
    if [ -t 0 ]; then
      echo "Paste the CF_Authorization cookie (or a Cf-Access-Jwt-Assertion"
      echo "header value), then press enter:"
    fi
    IFS= read -r token
    token=${token//[[:space:]]/}
    token=${token#CF_Authorization=}
    if [ "$(awk -F. '{print NF-1}' <<<"$token")" != 2 ]; then
      echo "that does not look like a JWT (expected three dot-separated parts)" >&2
      exit 65
    fi

    # Decode only. This deliberately does not verify the signature: the
    # point here is to read the issuer and audience out of a token you
    # already have, and avian/api/access-auth.php is what verifies one
    # when it actually matters.
    php -r '
      $parts = explode(".", trim(stream_get_contents(STDIN)));
      $pad = strtr($parts[1], "-_", "+/");
      $pad .= str_repeat("=", (4 - strlen($pad) % 4) % 4);
      $body = json_decode((string)base64_decode($pad, true), true);
      if (!is_array($body)) { fwrite(STDERR, "could not decode the token\n"); exit(65); }
      $iss = rtrim((string)($body["iss"] ?? ""), "/");
      $team = preg_replace("#^https?://#", "", $iss);
      $aud = $body["aud"] ?? [];
      $aud = is_array($aud) ? ($aud[0] ?? "") : $aud;
      $exp = (int)($body["exp"] ?? 0);
      if ($team === "" || $aud === "") {
        fwrite(STDERR, "the token carries no issuer or audience\n"); exit(65);
      }
      fwrite(STDERR, sprintf("signed in as %s, token %s\n",
        $body["email"] ?? "(no email claim)",
        $exp && $exp < time() ? "EXPIRED (fine - only iss and aud matter here)" : "current"));
      printf("ACCESS_TEAM_DOMAIN=%s\nACCESS_AUD=%s\n", $team, $aud);
    ' <<<"$token" || exit $?
    token=''
    echo >&2
    echo "Install them with:" >&2
    echo "  sudo $0 install <team-domain> <aud>" >&2
    ;;

  install)
    team="${2:-}"; aud="${3:-}"
    [ -n "$team" ] && [ -n "$aud" ] || {
      echo "usage: sudo $0 install <team-domain> <aud>" >&2; exit 64; }
    team=${team#https://}; team=${team%/}
    valid_team "$team" || { echo "team domain looks wrong: $team" >&2; exit 65; }
    valid_aud "$aud" || { echo "audience tag looks wrong: $aud" >&2; exit 65; }
    [ -w "$CONF" ] || { echo "cannot write $CONF - run this with sudo" >&2; exit 1; }

    backup="$CONF.before-access.$(date +%s)"
    cp -p "$CONF" "$backup" || exit 1

    # Replace in place if present, append otherwise - the same shape as
    # the station's own config writer, so every other line is preserved
    # byte for byte.
    tmp=$(mktemp "$(dirname "$CONF")/.birdnet.conf.XXXXXX") || exit 1
    awk -v team="$team" -v aud="$aud" '
      /^[[:space:]]*ACCESS_TEAM_DOMAIN[[:space:]]*=/ { print "ACCESS_TEAM_DOMAIN=" team; t=1; next }
      /^[[:space:]]*ACCESS_AUD[[:space:]]*=/         { print "ACCESS_AUD=" aud;         a=1; next }
      { print }
      END {
        if (!t) print "ACCESS_TEAM_DOMAIN=" team
        if (!a) print "ACCESS_AUD=" aud
      }
    ' "$CONF" >"$tmp" || { rm -f "$tmp"; exit 1; }

    chown --reference="$CONF" "$tmp" 2>/dev/null
    chmod --reference="$CONF" "$tmp" 2>/dev/null
    mv "$tmp" "$CONF" || exit 1
    echo "written to $CONF (backup: $backup)"
    echo
    exec "$0" check
    ;;

  check)
    team=$(conf_value ACCESS_TEAM_DOMAIN)
    aud=$(conf_value ACCESS_AUD)
    if [ -z "$team" ] || [ -z "$aud" ]; then
      echo "Access authentication: OFF"
      echo "  ACCESS_TEAM_DOMAIN ${team:-(not set)}"
      echo "  ACCESS_AUD         ${aud:+set}${aud:-(not set)}"
      echo
      echo "The API falls back to the station admin password. To turn it on:"
      echo "  $0 discover https://ghlyms.com"
      exit 1
    fi
    valid_team "$team" || { echo "ACCESS_TEAM_DOMAIN looks wrong: $team" >&2; exit 65; }
    valid_aud "$aud"   || { echo "ACCESS_AUD looks wrong" >&2; exit 65; }

    echo "ACCESS_TEAM_DOMAIN  $team"
    echo "ACCESS_AUD          ${aud:0:8}...${aud: -4} (${#aud} chars)"

    # The one thing that can be wrong without looking wrong: a team domain
    # that does not publish certificates. Without them nothing verifies,
    # and every request falls back to the password.
    problems=0
    certs=$(curl -fsS --max-time 6 "https://$team/cdn-cgi/access/certs" 2>/dev/null)
    count=0
    [ -n "$certs" ] && count=$(php -r '
      $j = json_decode(stream_get_contents(STDIN), true);
      echo is_array($j) ? count($j["public_certs"] ?? []) : 0;
    ' <<<"$certs")
    if [ "${count:-0}" -lt 1 ]; then
      echo "signing certificates: NOT AVAILABLE from https://$team/cdn-cgi/access/certs"
      echo "  check the team domain, or the station's internet access"
      problems=$((problems + 1))
    else
      echo "signing certificates: $count fetched from $team"
    fi

    # The one thing that can be wrong and still look right: an audience
    # tag from a different application, or from before the app was
    # recreated. Nothing rejects it loudly - Access auth simply never
    # matches and every visitor falls back to the password. Compare it
    # against what the edge actually sends.
    site="${2:-}"
    if [ -n "$site" ]; then
      location=$(login_redirect "$site")
      if [ -z "$location" ]; then
        echo "audience: could not check - $site did not redirect to a login"
        echo "  (running this from a signed-in browser or an Access bypass"
        echo "   rule both look like this; it does not mean the tag is wrong)"
      else
        edge_team=$(redirect_field "$location" host)
        edge_aud=$(redirect_field "$location" kid)
        if [ "$edge_team" != "$team" ]; then
          echo "team domain: MISMATCH - $site is behind $edge_team, not $team"
          problems=$((problems + 1))
        fi
        if [ -z "$edge_aud" ]; then
          echo "audience: the redirect carried no tag to compare against"
        elif [ "$edge_aud" != "$aud" ]; then
          echo "audience: MISMATCH - $site sends ${edge_aud:0:8}..., configured ${aud:0:8}..."
          echo "  fix with: sudo $0 install $edge_team $edge_aud"
          problems=$((problems + 1))
        else
          echo "audience: matches what $site sends"
        fi
      fi
    fi


    echo
    if [ "$problems" -gt 0 ]; then
      echo "Access authentication: NOT WORKING ($problems problem(s) above)"
      echo "Until those are fixed the API falls back to the admin password,"
      echo "which is silent - visitors just see the unlock message."
      exit 1
    fi
    echo "Access authentication: ON"
    echo "People on your Access policy can record without the admin password."
    ;;

  *)
    echo "usage: $0 check [url] | discover <url> | read | install <team> <aud>" >&2
    exit 64
    ;;
esac
