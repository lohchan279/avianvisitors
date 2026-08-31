<?php
// Cloudflare Access identity verification, and the place naming that
// keeps coordinates out of the API's answers.
//
// The Access half is the part worth testing hardest. Accepting a header
// that says who somebody is, without checking who signed it, is the
// classic way to turn "authenticated" into "asked nicely" - so every
// forgery this file can think of gets its own case, and each one has to
// come back rejected.
//
//     php tests/test_field_access_auth.php
//
// Runs entirely on a throwaway RSA key pair and a fixture cert cache, so
// it never touches the network or the station's own configuration.

declare(strict_types=1);

$checks = 0;
$failures = 0;

function check(bool $condition, string $label): void {
    global $checks, $failures;
    $checks++;
    if ($condition) return;
    $failures++;
    fwrite(STDERR, "FAIL: $label\n");
}

function keypair(): array {
    $key = openssl_pkey_new([
        'private_key_bits' => 2048,
        'private_key_type' => OPENSSL_KEYTYPE_RSA,
    ]);
    if ($key === false) {
        fwrite(STDERR, "SKIP: openssl is unavailable\n");
        exit(0);
    }
    openssl_pkey_export($key, $private);
    $csr = openssl_csr_new(['commonName' => 'access-test'], $key, ['digest_alg' => 'sha256']);
    $cert = openssl_csr_sign($csr, null, $key, 2, ['digest_alg' => 'sha256']);
    openssl_x509_export($cert, $pem);
    return [$private, $pem];
}

function b64(string $value): string {
    return rtrim(strtr(base64_encode($value), '+/', '-_'), '=');
}

function token(array $head, array $body, string $privateKey): string {
    $encoded = b64((string)json_encode($head)) . '.' . b64((string)json_encode($body));
    openssl_sign($encoded, $signature, $privateKey, OPENSSL_ALGO_SHA256);
    return $encoded . '.' . b64($signature);
}

$tmp = sys_get_temp_dir() . '/avian-access-' . bin2hex(random_bytes(6));
mkdir($tmp, 0700, true);

[$privateKey, $cert] = keypair();
[$otherPrivate, $otherCert] = keypair();

$confPath = "$tmp/birdnet.conf";
file_put_contents($confPath,
    "SITE_NAME=\"test\"\n"
    . "ACCESS_TEAM_DOMAIN=\"ghlyms.cloudflareaccess.com\"\n"
    . "ACCESS_AUD=\"aud-tag-1234\"\n");

$certPath = "$tmp/access-certs.json";
file_put_contents($certPath, (string)json_encode([
    'kid-good'  => $cert,
    'kid-other' => $otherCert,
]));

putenv("AV_ACCESS_CONF=$confPath");
putenv("AV_ACCESS_CERTS=$certPath");

require_once dirname(__DIR__) . '/avian/api/access-auth.php';

check(avian_access_configured(), 'a team domain and audience count as configured');
check(avian_access_team_domain() === 'ghlyms.cloudflareaccess.com', 'team domain parses');

$now = time();
$head = ['alg' => 'RS256', 'kid' => 'kid-good', 'typ' => 'JWT'];
$body = [
    'iss'   => 'https://ghlyms.cloudflareaccess.com',
    'aud'   => ['aud-tag-1234'],
    'exp'   => $now + 3600,
    'iat'   => $now - 10,
    'email' => 'Somebody@Example.com',
    'sub'   => 'user-1',
];

function identity(string $jwt): ?array {
    return avian_access_identity_uncached(['HTTP_CF_ACCESS_JWT_ASSERTION' => $jwt]);
}

$valid = identity(token($head, $body, $privateKey));
check($valid !== null, 'a properly signed token is accepted');
check(($valid['email'] ?? '') === 'somebody@example.com', 'the email is normalised to lower case');
check(($valid['sub'] ?? '') === 'user-1', 'the subject comes through');

// Every one of these is somebody trying to get in. None may succeed.
check(identity(token($head, ['exp' => $now - 400] + $body, $privateKey)) === null,
    'an expired token is rejected');
check(identity(token($head, ['nbf' => $now + 600] + $body, $privateKey)) === null,
    'a token that is not valid yet is rejected');
check(identity(token($head, ['aud' => ['another-app']] + $body, $privateKey)) === null,
    'a token for a different Access application is rejected');
check(identity(token($head, ['iss' => 'https://evil.cloudflareaccess.com'] + $body, $privateKey)) === null,
    'a token from a different team is rejected');
check(identity(token($head, $body, $otherPrivate)) === null,
    'a token signed by a key we do not trust is rejected');
check(identity(token(['alg' => 'RS256', 'kid' => 'kid-other', 'typ' => 'JWT'], $body, $privateKey)) === null,
    'a kid pointing at a different certificate is rejected');
check(identity(token(['alg' => 'none', 'kid' => 'kid-good'], $body, $privateKey)) === null,
    'alg none is rejected');
check(identity(token(['alg' => 'HS256', 'kid' => 'kid-good'], $body, $privateKey)) === null,
    'a symmetric algorithm is rejected');
check(identity(token(['alg' => 'RS256'], $body, $privateKey)) === null,
    'a token with no kid is rejected');

$tampered = explode('.', token($head, $body, $privateKey));
$tampered[1] = b64((string)json_encode(['email' => 'attacker@example.com'] + $body));
check(identity(implode('.', $tampered)) === null, 'a body edited after signing is rejected');

check(identity('not-a-jwt') === null, 'a token that is not a JWT is rejected');
check(avian_access_identity_uncached([]) === null, 'no token means no identity');

// Unconfigured is off, not open: with no team domain the header is
// ignored entirely and the caller falls back to the admin password.
file_put_contents($confPath, "SITE_NAME=\"test\"\n");
$reload = shell_exec(sprintf(
    'AV_ACCESS_CONF=%s AV_ACCESS_CERTS=%s php -r %s',
    escapeshellarg($confPath), escapeshellarg($certPath),
    escapeshellarg(
        'require "' . dirname(__DIR__) . '/avian/api/access-auth.php";'
        . ' echo avian_access_configured() ? "configured" : "off";'
    )
));
check(trim((string)$reload) === 'off', 'a missing team domain turns Access auth off');

// ---- place naming ----------------------------------------------------
require_once dirname(__DIR__) . '/avian/api/places.php';

check(avian_area_at(1.3690, 103.8480) === 'Ang Mo Kio', 'a town centre resolves to its planning area');
check(avian_area_at(1.4100, 103.9600) === 'North-Eastern Islands', 'Pulau Ubin resolves offshore');
check(avian_area_at(1.2494, 103.8303) === 'Southern Islands', 'Sentosa resolves offshore');
check(avian_area_at(3.1390, 101.6869) === null, 'a point in another country has no area');

$home = avian_place_for(1.3690, 103.8480, 1.3692, 103.8482);
check($home['place'] === 'Home', 'a recording at the station is called Home');
check($home['area'] === 'Ang Mo Kio', 'Home still carries a real area, so the map has an anchor');

$away = avian_place_for(1.3810, 103.9530, 1.3692, 103.8482);
check($away['place'] === 'Pasir Ris', 'a recording away from the station uses the real place name');

$nowhere = avian_place_for(null, null, 1.3692, 103.8482);
check($nowhere['place'] === null && $nowhere['area'] === null,
    'a submission with no position gets no invented place');

// The API must never hand a coordinate back. This is a property of the
// source, so read it rather than trusting a comment about it.
$api = (string)file_get_contents(dirname(__DIR__) . '/avian/api/submissions.php');
check(!preg_match("/'lat'\s*=>/", $api), 'no response field carries a latitude');
check(!preg_match("/'lon'\s*=>/", $api), 'no response field carries a longitude');
check(str_contains($api, 'avian_require_admin_or_access()'),
    'submissions accept an Access identity as well as the admin password');

array_map('unlink', glob("$tmp/*") ?: []);
rmdir($tmp);
putenv('AV_ACCESS_CONF');
putenv('AV_ACCESS_CERTS');

if ($failures > 0) {
    fwrite(STDERR, "$failures of $checks checks failed\n");
    exit(1);
}
echo "field access tests passed ($checks checks)\n";
