<?php
// Routing rules for the preview server (avian/scripts/preview.sh).
//
// This exists to be *stricter* than serving a directory, because the two
// bugs a preview most needs to catch are both bugs of absence:
//
//   - a frontend file missing from the manifest in scripts/link_webroot.sh
//     404s on the real site while working perfectly from a checkout, so it
//     looks fine locally and is simply gone in production;
//   - an API endpoint missing from the allowlist in
//     scripts/update_caddyfile.sh gets `respond 404` on the real site for
//     the same invisible reason.
//
// So both lists are read out of those scripts rather than restated here,
// and anything not on them is refused. A preview that served whatever was
// on disk would answer requests the station will not.

declare(strict_types=1);

$root = getenv('AV_PREVIEW_ROOT') ?: __DIR__;
$path = (string)parse_url((string)($_SERVER['REQUEST_URI'] ?? '/'), PHP_URL_PATH);
$path = '/' . ltrim(rawurldecode($path), '/');

// A traversal cannot be allowed to escape the sandbox even here, since a
// preview usually runs beside the real recordings.
if (str_contains($path, '..')) {
    http_response_code(400);
    exit("bad path\n");
}

/** @return list<string> the names the webroot manifest publishes */
function preview_manifest(string $root): array {
    $file = "$root/../manifest";
    $names = is_file($file) ? file($file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) : [];
    return $names ?: [];
}

/** @return list<string> the API endpoints the Caddy policy publishes */
function preview_api_allowlist(string $root): array {
    $file = "$root/../api-allowlist";
    $names = is_file($file) ? file($file, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) : [];
    return $names ?: [];
}

function refuse(string $why): never {
    http_response_code(404);
    header('Content-Type: text/plain; charset=utf-8');
    exit("404 - $why\n");
}

/* One diagnostic, because the alternative is asking somebody to find
 * devtools on a phone. Reports whether the admin session cookie reached
 * the preview at all - which is the whole question when unlocking appears
 * to work and the API still answers 401. Names only, never values. */
if ($path === '/__preview') {
    header('Content-Type: text/plain; charset=utf-8');
    $names = array_keys($_COOKIE);
    sort($names);
    echo "preview is answering\n";
    echo 'cookies it received: ' . ($names ? implode(', ', $names) : '(none)') . "\n";
    echo 'admin session cookie: '
        . (isset($_COOKIE['avian_admin']) ? "yes\n" : "NO - this is why the API says unauthorized\n");
    echo 'password gate: ' . (getenv('AV_REQUIRE_AUTH') === '0' ? "off\n" : "on\n");
    exit;
}

if ($path === '/' || $path === '/index.html') {
    require "$root/index.html";
    exit;
}

/**
 * Stand in for Cloudflare Access: sign an assertion for this request the
 * way the edge would, using the throwaway key the preview generated.
 *
 * This is what makes `preview.sh --as access` worth running. It does not
 * skip the check - it feeds the real verifier a real signature, so the
 * question it answers is the one that matters: can somebody on the Access
 * policy use this without the station's admin password?
 */
function preview_access_header(): ?string {
    $key = getenv('AV_PREVIEW_ACCESS_KEY');
    $team = getenv('AV_PREVIEW_ACCESS_TEAM');
    $aud = getenv('AV_PREVIEW_ACCESS_AUD');
    $email = getenv('AV_PREVIEW_ACCESS_EMAIL') ?: 'preview@example.com';
    if (!$key || !$team || !$aud || !is_readable($key)) return null;

    $encode = static fn(array $part): string =>
        rtrim(strtr(base64_encode((string)json_encode($part)), '+/', '-_'), '=');
    $now = time();
    $signing = $encode(['alg' => 'RS256', 'kid' => 'preview', 'typ' => 'JWT'])
        . '.' . $encode([
            'iss'   => "https://$team",
            'aud'   => [$aud],
            'exp'   => $now + 600,
            'iat'   => $now - 5,
            'email' => $email,
            'sub'   => 'preview-user',
        ]);
    openssl_sign($signing, $signature, (string)file_get_contents($key), OPENSSL_ALGO_SHA256);
    return $signing . '.' . rtrim(strtr(base64_encode($signature), '+/', '-_'), '=');
}

// ---- the API ---------------------------------------------------------
if (str_starts_with($path, '/avian/api/')) {
    $name = substr($path, strlen('/avian/api/'));
    if (!in_array($name, preview_api_allowlist($root), true)) {
        refuse("$name is not on the served API allowlist in scripts/update_caddyfile.sh");
    }
    $script = "$root/avian/api/$name";
    if (!is_file($script)) refuse("$name does not exist");
    $assertion = preview_access_header();
    if ($assertion !== null) $_SERVER['HTTP_CF_ACCESS_JWT_ASSERTION'] = $assertion;
    $_SERVER['SCRIPT_FILENAME'] = $script;
    $_SERVER['SCRIPT_NAME'] = $path;
    require $script;
    exit;
}

// Artwork is served straight from the avian tree, as the real policy does.
if (str_starts_with($path, '/avian/assets/')) return false;

// Anything else under /avian/ is private tooling. The real policy 404s it,
// and so does this - which is what proves sg-areas.php is unreachable.
if (str_starts_with($path, '/avian/')) {
    refuse('the avian tree serves only reviewed endpoints and artwork');
}

// ---- recordings ------------------------------------------------------
if (preg_match('#^/(By_Date|Charts)/#', $path)) return false;

// ---- the frontend ----------------------------------------------------
$first = explode('/', ltrim($path, '/'))[0];
if (in_array($first, preview_manifest($root), true)) return false;

refuse("$first is not in the webroot manifest in scripts/link_webroot.sh");
