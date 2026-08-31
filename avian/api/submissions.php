<?php
// AvianVisitors - field recordings submitted from a phone.
//
// Somebody records a few seconds of a bird wherever they are, the
// station's own BirdNET scores it, and the best answer is kept if the
// model is confident enough to stand behind it. Nobody is asked to pick
// from a list of Latin names: the person holding the phone almost never
// knows which of five candidates it was, so asking them turns a lucky
// guess into a recorded fact. Below the bar the station says it could not
// make that one out, and the clip is discarded.
//
// Endpoints:
//   POST ?action=submit   multipart: audio, optional lat/lon/accuracy
//                         -> {id}. Row lands as 'pending'; the worker
//                         (scripts/submission_worker.py) picks it up.
//   GET  ?action=result&id=N -> {status, sci, com, conf, place}
//                         status is confirmed | unsure | pending |
//                         analysing | failed | rejected. A pure read.
//   GET  ?action=audio&id=N  -> the clip itself, range-aware.
//   POST ?action=reject   JSON {id} -> discards it, audio and all. This
//                         is the "that was not it" button.
//   GET  ?action=list&limit=N -> confirmed submissions, newest first,
//                         plus per-area totals for the map.
//
// Submissions live in their own table, deliberately not in `detections`:
// the collage, stats and the BirdWeather export all read that table, and
// nothing that reads it should have to learn about a second provenance.
//
// Coordinates go in and never come out. The database stores the fix
// because the model needs it - the occurrence filter judges a clip by
// where it was heard - but submit resolves it to a planning-area name
// once, on the way in, and every response afterwards speaks in names.
//
// Auth: an identity Cloudflare Access has already vouched for is enough,
// so the people on the Access policy can record without being handed the
// station's admin password. Failing that, the station's ordinary admin
// rules apply.

declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

require_once __DIR__ . '/admin-auth.php';
require_once __DIR__ . '/access-auth.php';
require_once __DIR__ . '/places.php';
require_once __DIR__ . '/submissions-schema.php';

$identity = avian_require_admin_or_access();

/**
 * A path a preview run may redirect, honoured only under the CLI and the
 * PHP built-in server. The station is served by FPM, so this can never
 * move anything in production - which matters, because the two paths
 * below are the live detections database and the live station config.
 *
 * avian/scripts/preview.sh uses these to point a throwaway copy of the
 * site at a throwaway copy of the data.
 */
function preview_path(string $variable, string $fallback): string {
    if (!in_array(PHP_SAPI, ['cli', 'cli-server'], true)) return $fallback;
    $override = getenv($variable);
    return is_string($override) && $override !== '' ? $override : $fallback;
}

$BIRDNETPI_DIR = dirname(__DIR__, 2);
$DB_PATH = preview_path('AV_DB_FILE', "$BIRDNETPI_DIR/scripts/birds.db");
$CONF    = preview_path('AV_BIRDNET_CONF', '/etc/birdnet/birdnet.conf');

const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;   // ~8 MB; 15s of phone audio is ~250 KB
const MAX_PENDING      = 20;                // backlog guard, not a rate limit

/** Read one value from birdnet.conf, tolerating the quoted PHP-ish format. */
function conf_value(string $conf, string $key, string $fallback = ''): string {
    if (!is_readable($conf)) return $fallback;
    foreach (file($conf, FILE_IGNORE_NEW_LINES) as $line) {
        if (preg_match('/^\s*' . preg_quote($key, '/') . '\s*=\s*(.*)$/', $line, $m)) {
            $v = trim($m[1]);
            if (strlen($v) >= 2 && $v[0] === '"' && substr($v, -1) === '"') {
                $v = substr($v, 1, -1);
            }
            return $v === '' ? $fallback : $v;
        }
    }
    return $fallback;
}

function fail(string $message, int $code = 400): never {
    http_response_code($code);
    echo json_encode(['ok' => false, 'error' => $message]);
    exit;
}

function db(string $path): PDO {
    if (!is_file($path)) fail('birds.db not found', 500);
    $pdo = new PDO('sqlite:' . $path, null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
    // WAL keeps a submission write from blocking the analyser's own writes.
    $pdo->exec('PRAGMA journal_mode=WAL');
    $pdo->exec('PRAGMA busy_timeout=5000');
    avian_submissions_schema(
        $pdo,
        num_or_null(conf_value($GLOBALS['CONF'], 'LATITUDE', ''), -90, 90),
        num_or_null(conf_value($GLOBALS['CONF'], 'LONGITUDE', ''), -180, 180)
    );
    return $pdo;
}

/** The body of a JSON POST, or [] for anything else. */
function json_body(): array {
    $raw = file_get_contents('php://input');
    if ($raw === false || $raw === '') return [];
    $j = json_decode($raw, true);
    return is_array($j) ? $j : [];
}

function num_or_null($v, float $min, float $max): ?float {
    if ($v === null || $v === '' || !is_numeric($v)) return null;
    $f = (float)$v;
    if (!is_finite($f) || $f < $min || $f > $max) return null;
    return $f;
}

/** Delete a submission's audio, refusing to follow a path out of the tree. */
function drop_audio(string $extracted, string $relative): void {
    $real = realpath($extracted . '/' . $relative);
    $base = realpath($extracted . '/Submissions');
    if ($real !== false && $base !== false && str_starts_with($real, $base . '/')) {
        @unlink($real);
    }
}

/**
 * How a submitter is shown. An Access identity is an email address, and
 * the whole address is more than a caption needs - the part before the @
 * is who it was.
 */
function display_submitter(?string $stored): ?string {
    $stored = trim((string)$stored);
    if ($stored === '') return null;
    $at = strpos($stored, '@');
    return $at === false ? $stored : substr($stored, 0, $at);
}

$action = (string)($_GET['action'] ?? '');
$pdo = db($DB_PATH);
$EXTRACTED = conf_value($CONF, 'EXTRACTED',
    (getenv('HOME') ?: '/home/pi') . '/BirdSongs/Extracted');

// ---------------------------------------------------------------- submit
if ($action === 'submit') {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') fail('submit requires POST', 405);

    $file = $_FILES['audio'] ?? null;
    if (!is_array($file) || ($file['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK) {
        fail('no audio uploaded');
    }
    if (($file['size'] ?? 0) <= 0) fail('empty recording');
    if ($file['size'] > MAX_UPLOAD_BYTES) fail('recording too large', 413);
    if (!is_uploaded_file($file['tmp_name'])) fail('bad upload');

    $pending = (int)$pdo->query("SELECT COUNT(*) FROM submissions WHERE Status IN ('pending','analysing')")
        ->fetchColumn();
    if ($pending >= MAX_PENDING) fail('too many recordings still being analysed', 429);

    // Extension comes from the browser's MIME type, never the filename -
    // MediaRecorder gives webm/opus on Chrome and mp4/aac on Safari.
    $mime = strtolower((string)($file['type'] ?? ''));
    $ext = match (true) {
        str_contains($mime, 'webm')            => 'webm',
        str_contains($mime, 'ogg')             => 'ogg',
        str_contains($mime, 'mp4'),
        str_contains($mime, 'm4a'),
        str_contains($mime, 'aac')             => 'm4a',
        str_contains($mime, 'wav'),
        str_contains($mime, 'wave')            => 'wav',
        str_contains($mime, 'mpeg')            => 'mp3',
        default                                => null,
    };
    if ($ext === null) fail('unsupported audio type: ' . ($mime ?: 'unknown'));

    $day = gmdate('Y-m-d');
    $dir = "$EXTRACTED/Submissions/$day";
    if (!is_dir($dir) && !@mkdir($dir, 0755, true) && !is_dir($dir)) {
        fail('could not create the submissions directory', 500);
    }

    try {
        $name = gmdate('His') . '-' . bin2hex(random_bytes(6)) . '.' . $ext;
    } catch (Throwable $e) {
        fail('could not name the recording', 500);
    }
    $dest = "$dir/$name";
    if (!move_uploaded_file($file['tmp_name'], $dest)) fail('could not store the recording', 500);
    @chmod($dest, 0644);

    $lat = num_or_null($_POST['lat'] ?? null, -90, 90);
    $lon = num_or_null($_POST['lon'] ?? null, -180, 180);
    // The one moment a coordinate becomes a name. After this row is
    // written the position is only ever read by the worker, which needs
    // it for the occurrence filter and nothing else.
    $named = avian_place_for(
        $lat, $lon,
        num_or_null(conf_value($CONF, 'LATITUDE', ''), -90, 90),
        num_or_null(conf_value($CONF, 'LONGITUDE', ''), -180, 180)
    );

    // A verified Access identity is the honest answer to "who recorded
    // this", and it cannot be typed in by the client.
    $who = $identity['email'] ?? '';
    if ($who === '') $who = mb_substr(trim((string)($_POST['submitter'] ?? '')), 0, 60);

    $stmt = $pdo->prepare('INSERT INTO submissions
        (Created, Status, Lat, Lon, Accuracy, Audio, Submitter, Place, Area)
        VALUES (:created, :status, :lat, :lon, :acc, :audio, :who, :place, :area)');
    $stmt->execute([
        ':created' => gmdate('c'),
        ':status'  => 'pending',
        ':lat'     => $lat,
        ':lon'     => $lon,
        ':acc'     => num_or_null($_POST['accuracy'] ?? null, 0, 100000),
        ':audio'   => "Submissions/$day/$name",
        ':who'     => $who !== '' ? $who : null,
        ':place'   => $named['place'],
        ':area'    => $named['area'],
    ]);

    echo json_encode(['ok' => true, 'id' => (int)$pdo->lastInsertId(), 'status' => 'pending',
                      'place' => $named['place']]);
    exit;
}

// ---------------------------------------------------------------- result
if ($action === 'result') {
    $id = (int)($_GET['id'] ?? 0);
    if ($id <= 0) fail('bad id');
    $stmt = $pdo->prepare('SELECT Id, Status, Audio, Error, Sci_Name, Com_Name,
                                  Confidence, Place
                           FROM submissions WHERE Id = :id');
    $stmt->execute([':id' => $id]);
    $row = $stmt->fetch();
    if (!$row) fail('no such submission', 404);

    echo json_encode([
        'ok'     => true,
        'id'     => (int)$row['Id'],
        'status' => $row['Status'],
        'sci'    => $row['Sci_Name'],
        'com'    => $row['Com_Name'],
        'conf'   => $row['Confidence'] === null ? null : round((float)$row['Confidence'], 3),
        'place'  => $row['Place'],
        'error'  => $row['Error'],
    ]);
    exit;
}

// ----------------------------------------------------------------- audio
// Submissions live under Extracted/Submissions, which no static route
// serves - and should not, because a field recording deserves the same
// authentication as everything else here. So the audio comes back through
// the API, by id, with the path read from the database rather than taken
// from the request.
if ($action === 'audio') {
    $id = (int)($_GET['id'] ?? 0);
    if ($id <= 0) fail('bad id');
    $stmt = $pdo->prepare("SELECT Audio FROM submissions
                           WHERE Id = :id AND Status = 'confirmed'");
    $stmt->execute([':id' => $id]);
    $row = $stmt->fetch();
    if (!$row) fail('no such recording', 404);

    $real = realpath($EXTRACTED . '/' . (string)$row['Audio']);
    $base = realpath($EXTRACTED . '/Submissions');
    if ($real === false || $base === false || !str_starts_with($real, $base . '/')) {
        fail('the recording is missing', 404);
    }

    $types = ['webm' => 'audio/webm', 'ogg' => 'audio/ogg', 'm4a' => 'audio/mp4',
              'wav' => 'audio/wav', 'mp3' => 'audio/mpeg'];
    $ext = strtolower(pathinfo($real, PATHINFO_EXTENSION));
    $size = (int)filesize($real);

    header('Content-Type: ' . ($types[$ext] ?? 'application/octet-stream'));
    header('Accept-Ranges: bytes');
    header('Cache-Control: private, max-age=3600');

    // Safari will not play a <audio> source that cannot serve a range, so
    // honour a single range rather than making it download the clip whole
    // and then refuse it.
    $start = 0;
    $end = $size - 1;
    $range = (string)($_SERVER['HTTP_RANGE'] ?? '');
    if ($range !== '' && preg_match('/^bytes=(\d*)-(\d*)$/', $range, $m)) {
        if ($m[1] === '' && $m[2] === '') {
            http_response_code(416);
            header("Content-Range: bytes */$size");
            exit;
        }
        if ($m[1] === '') {
            $start = max(0, $size - (int)$m[2]);
        } else {
            $start = (int)$m[1];
            if ($m[2] !== '') $end = min($end, (int)$m[2]);
        }
        if ($start > $end || $start >= $size) {
            http_response_code(416);
            header("Content-Range: bytes */$size");
            exit;
        }
        http_response_code(206);
        header("Content-Range: bytes $start-$end/$size");
    }

    header('Content-Length: ' . ($end - $start + 1));
    $handle = fopen($real, 'rb');
    if ($handle === false) fail('could not read the recording', 500);
    fseek($handle, $start);
    $left = $end - $start + 1;
    while ($left > 0 && !feof($handle)) {
        $chunk = fread($handle, (int)min(65536, $left));
        if ($chunk === false || $chunk === '') break;
        echo $chunk;
        $left -= strlen($chunk);
    }
    fclose($handle);
    exit;
}

// ---------------------------------------------------------------- reject
if ($action === 'reject') {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') fail('reject requires POST', 405);
    $body = json_body();
    $id = (int)($body['id'] ?? 0);
    if ($id <= 0) fail('reject needs id');

    $stmt = $pdo->prepare('SELECT Audio FROM submissions WHERE Id = :id');
    $stmt->execute([':id' => $id]);
    $row = $stmt->fetch();
    if (!$row) fail('no such submission', 404);

    // Discard the audio too: a rejected recording is one nobody wants,
    // and keeping it only grows the disk.
    drop_audio($EXTRACTED, (string)$row['Audio']);
    $pdo->prepare('UPDATE submissions SET Status = :s WHERE Id = :id')
        ->execute([':s' => 'rejected', ':id' => $id]);
    echo json_encode(['ok' => true, 'id' => $id, 'status' => 'rejected']);
    exit;
}

// ------------------------------------------------------------------ list
if ($action === 'list') {
    $limit = (int)($_GET['limit'] ?? 100);
    if ($limit < 1 || $limit > 500) $limit = 100;
    $stmt = $pdo->prepare("SELECT Id, Created, Sci_Name, Com_Name, Confidence,
                                  Place, Area, Audio, Submitter
                           FROM submissions
                           WHERE Status = 'confirmed'
                           ORDER BY Created DESC, Id DESC
                           LIMIT :lim");
    $stmt->bindValue(':lim', $limit, PDO::PARAM_INT);
    $stmt->execute();

    $out = [];
    foreach ($stmt->fetchAll() as $r) {
        $out[] = [
            'id'    => (int)$r['Id'],
            'at'    => $r['Created'],
            'sci'   => $r['Sci_Name'],
            'com'   => $r['Com_Name'],
            'conf'  => $r['Confidence'] === null ? null : round((float)$r['Confidence'], 3),
            'place' => $r['Place'],
            'area'  => $r['Area'],
            // A flag, not a path. The client asks for ?action=audio&id=N
            // and the server looks the path up again - the filesystem
            // layout is not the browser's business.
            'audio' => $r['Audio'] !== null && $r['Audio'] !== '',
            'who'   => display_submitter($r['Submitter']),
        ];
    }

    // Per-area totals for the map, over every confirmed submission rather
    // than the page of rows above - the shading should not change because
    // somebody asked for a shorter list.
    $areas = [];
    $totals = $pdo->query("SELECT Area, COUNT(*) AS n, COUNT(DISTINCT Sci_Name) AS species
                           FROM submissions
                           WHERE Status = 'confirmed' AND Area IS NOT NULL
                           GROUP BY Area");
    foreach ($totals as $r) {
        $areas[] = ['area' => (string)$r['Area'], 'count' => (int)$r['n'],
                    'species' => (int)$r['species']];
    }

    // The station's own listening post. Its area, never its coordinates:
    // the map wants somewhere to put the mark, not the address.
    $home = avian_place_for(
        num_or_null(conf_value($CONF, 'LATITUDE', ''), -90, 90),
        num_or_null(conf_value($CONF, 'LONGITUDE', ''), -180, 180)
    );
    $station = null;
    try {
        $station = [
            'area'    => $home['area'],
            'species' => (int)$pdo->query('SELECT COUNT(DISTINCT Sci_Name) FROM detections')
                ->fetchColumn(),
        ];
    } catch (Throwable $e) {
        $station = ['area' => $home['area'], 'species' => null];
    }

    echo json_encode(['ok' => true, 'submissions' => $out, 'areas' => $areas,
                      'station' => $station]);
    exit;
}

fail('unknown action; try submit, result, audio, reject or list', 404);
