<?php
// Cloudflare Access identity, verified.
//
// The station's own admin password guards the settings screens, and that
// is the right bar for changing how the station runs. It is the wrong bar
// for recording a bird: the people allowed to do that are already listed
// in the Cloudflare Access policy, and handing them the station password
// so they can hold up a phone is both a nuisance and a downgrade - one
// shared secret instead of named accounts that can be revoked one at a
// time.
//
// So an endpoint may accept an Access identity *instead of* the admin
// password. Access puts a signed JWT on every request it lets through, in
// the Cf-Access-Jwt-Assertion header. This file verifies that signature
// properly - fetching Cloudflare's public certificates for the team and
// checking RS256, audience, issuer and expiry - because an unverified
// header is not authentication, it is a request to be trusted.
//
// Configuration lives in /etc/birdnet/birdnet.conf:
//
//     ACCESS_TEAM_DOMAIN="yourteam.cloudflareaccess.com"
//     ACCESS_AUD="<the Application Audience tag from the Access app>"
//
// Both are required. With either missing, Access auth is simply off and
// callers fall back to the admin password - there is no half-configured
// state where an unchecked header gets somebody in.

declare(strict_types=1);

const AVIAN_ACCESS_HEADER = 'HTTP_CF_ACCESS_JWT_ASSERTION';
const AVIAN_ACCESS_COOKIE = 'CF_Authorization';
// Cloudflare rotates signing keys; six hours is well inside the rotation
// window and keeps all but a handful of requests off the network.
const AVIAN_ACCESS_CERT_TTL = 21600;
const AVIAN_ACCESS_CERT_TIMEOUT = 4;
// A little slack for clock drift between the Pi and Cloudflare.
const AVIAN_ACCESS_LEEWAY = 60;

const AVIAN_ACCESS_CONF_DEFAULT_PATH = '/etc/birdnet/birdnet.conf';

/**
 * Test fixtures and preview runs may point elsewhere; the station cannot.
 * FPM serves the real site, and neither SAPI below is FPM.
 */
function avian_access_conf_path(): string {
    $override = getenv('AV_ACCESS_CONF');
    if (in_array(PHP_SAPI, ['cli', 'cli-server'], true)
        && is_string($override) && $override !== '') {
        return $override;
    }
    return AVIAN_ACCESS_CONF_DEFAULT_PATH;
}

function avian_access_conf(string $key): string {
    static $conf = null;
    if ($conf === null) {
        $conf = [];
        $path = avian_access_conf_path();
        if (is_readable($path)) {
            foreach (file($path, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
                if (preg_match('/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/', $line, $m)) {
                    $value = trim($m[2]);
                    if (strlen($value) >= 2 && $value[0] === '"' && substr($value, -1) === '"') {
                        $value = substr($value, 1, -1);
                    }
                    $conf[$m[1]] = $value;
                }
            }
        }
    }
    return (string)($conf[$key] ?? '');
}

function avian_access_team_domain(): string {
    $domain = strtolower(trim(avian_access_conf('ACCESS_TEAM_DOMAIN')));
    $domain = preg_replace('#^https?://#', '', $domain) ?? '';
    $domain = rtrim($domain, '/');
    // A team domain is a hostname and nothing else. Anything with a path,
    // a port or a stray character is a misconfiguration, not a host.
    return preg_match('/^[a-z0-9.-]+\.[a-z]{2,}$/', $domain) ? $domain : '';
}

function avian_access_configured(): bool {
    return avian_access_team_domain() !== '' && trim(avian_access_conf('ACCESS_AUD')) !== '';
}

function avian_base64url_decode(string $value): string|false {
    $padded = strtr($value, '-_', '+/');
    $remainder = strlen($padded) % 4;
    if ($remainder) $padded .= str_repeat('=', 4 - $remainder);
    return base64_decode($padded, true);
}

function avian_access_cert_cache_path(): string {
    $override = getenv('AV_ACCESS_CERTS');
    if (in_array(PHP_SAPI, ['cli', 'cli-server'], true)
        && is_string($override) && $override !== '') {
        return $override;
    }
    foreach (['/var/lib/avian-visitors', sys_get_temp_dir()] as $dir) {
        if (is_dir($dir) && is_writable($dir)) return $dir . '/access-certs.json';
    }
    return sys_get_temp_dir() . '/avian-access-certs.json';
}

/**
 * Cloudflare's signing certificates for this team, PEM keyed by kid.
 *
 * @return array<string, string>
 */
function avian_access_certs(bool $forceRefresh = false): array {
    static $memo = null;
    if ($memo !== null && !$forceRefresh) return $memo;

    $domain = avian_access_team_domain();
    if ($domain === '') return $memo = [];

    $cache = avian_access_cert_cache_path();
    if (!$forceRefresh && is_file($cache)
        && (time() - (int)@filemtime($cache)) < AVIAN_ACCESS_CERT_TTL) {
        $cached = json_decode((string)@file_get_contents($cache), true);
        if (is_array($cached) && $cached) return $memo = $cached;
    }

    $url = "https://$domain/cdn-cgi/access/certs";
    $body = false;
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => AVIAN_ACCESS_CERT_TIMEOUT,
            CURLOPT_SSL_VERIFYPEER => true,
            CURLOPT_SSL_VERIFYHOST => 2,
        ]);
        $body = curl_exec($ch);
        if ((int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE) !== 200) $body = false;
        curl_close($ch);
    }
    if ($body === false) {
        $body = @file_get_contents($url, false, stream_context_create([
            'http' => ['timeout' => AVIAN_ACCESS_CERT_TIMEOUT],
        ]));
    }

    $decoded = is_string($body) ? json_decode($body, true) : null;
    $certs = [];
    if (is_array($decoded)) {
        foreach ($decoded['public_certs'] ?? [] as $entry) {
            $kid = (string)($entry['kid'] ?? '');
            $cert = (string)($entry['cert'] ?? '');
            if ($kid !== '' && $cert !== '') $certs[$kid] = $cert;
        }
    }

    if ($certs) {
        // Write via a temporary file so a request never reads a half-written
        // cache, and never let a cache-write failure break authentication.
        $tmp = $cache . '.' . getmypid();
        if (@file_put_contents($tmp, json_encode($certs)) !== false) {
            @chmod($tmp, 0600);
            if (!@rename($tmp, $cache)) @unlink($tmp);
        }
        return $memo = $certs;
    }

    // Cloudflare unreachable: a stale cache still proves who signed the
    // token, and the token's own expiry is what bounds the session.
    if (is_file($cache)) {
        $stale = json_decode((string)@file_get_contents($cache), true);
        if (is_array($stale) && $stale) return $memo = $stale;
    }
    return $memo = [];
}

function avian_access_token(?array $server = null): string {
    $server = $server ?? $_SERVER;
    $token = trim((string)($server[AVIAN_ACCESS_HEADER] ?? ''));
    if ($token === '') $token = trim((string)($_COOKIE[AVIAN_ACCESS_COOKIE] ?? ''));
    return $token;
}

/**
 * The verified identity behind this request, or null.
 *
 * Null means "no Access identity" for every reason there is - not
 * configured, no token, bad signature, wrong audience, expired. Callers
 * treat all of those the same way: fall back to the admin password.
 *
 * @return array{email: string, sub: string}|null
 */
function avian_access_identity(?array $server = null): ?array {
    static $memo = false;
    if ($memo !== false && $server === null) return $memo;

    $identity = avian_access_identity_uncached($server);
    if ($server === null) $memo = $identity;
    return $identity;
}

function avian_access_identity_uncached(?array $server = null): ?array {
    if (!avian_access_configured()) return null;
    $token = avian_access_token($server);
    if ($token === '' || substr_count($token, '.') !== 2) return null;

    [$head64, $body64, $sig64] = explode('.', $token);
    $head = json_decode((string)avian_base64url_decode($head64), true);
    $body = json_decode((string)avian_base64url_decode($body64), true);
    $signature = avian_base64url_decode($sig64);
    if (!is_array($head) || !is_array($body) || $signature === false) return null;

    // Only RS256. Naming the algorithm here is what stops a token that
    // asks to be verified with "none", or with a symmetric algorithm
    // keyed by the public certificate.
    if (($head['alg'] ?? '') !== 'RS256') return null;
    $kid = (string)($head['kid'] ?? '');
    if ($kid === '') return null;

    $certs = avian_access_certs();
    if (!isset($certs[$kid])) {
        // An unknown kid is what a key rotation looks like, so it is worth
        // one refresh - but only one, or an invalid token becomes a way to
        // make the station fetch on every request.
        $certs = avian_access_certs(true);
        if (!isset($certs[$kid])) return null;
    }

    $key = openssl_pkey_get_public($certs[$kid]);
    if ($key === false) return null;
    $signed = openssl_verify("$head64.$body64", $signature, $key, OPENSSL_ALGO_SHA256);
    if ($signed !== 1) return null;

    $now = time();
    $exp = (int)($body['exp'] ?? 0);
    $nbf = (int)($body['nbf'] ?? $body['iat'] ?? 0);
    if ($exp <= 0 || $now > $exp + AVIAN_ACCESS_LEEWAY) return null;
    if ($nbf && $now + AVIAN_ACCESS_LEEWAY < $nbf) return null;

    if (rtrim((string)($body['iss'] ?? ''), '/') !== 'https://' . avian_access_team_domain()) {
        return null;
    }

    // The audience tag is what ties the token to *this* application. A
    // valid token for a different app in the same team must not open this
    // one, which is exactly what checking aud prevents.
    $wanted = trim(avian_access_conf('ACCESS_AUD'));
    $aud = $body['aud'] ?? [];
    $audiences = array_map('strval', is_array($aud) ? $aud : [$aud]);
    $matched = false;
    foreach ($audiences as $candidate) {
        if (hash_equals($wanted, $candidate)) { $matched = true; break; }
    }
    if (!$matched) return null;

    $email = strtolower(trim((string)($body['email'] ?? '')));
    return [
        'email' => filter_var($email, FILTER_VALIDATE_EMAIL) ? $email : '',
        'sub'   => (string)($body['sub'] ?? ''),
    ];
}

/**
 * Guard an endpoint that a whitelisted Access user may use without the
 * station password. An Access identity is enough; anything else falls
 * through to the station's ordinary admin rules, so nothing gets *easier*
 * when Access is not in play.
 */
function avian_require_admin_or_access(): ?array {
    $identity = avian_access_identity();
    if ($identity !== null) return $identity;
    avian_require_admin();
    return null;
}
