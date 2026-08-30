<?php
// AvianVisitors - field recordings submitted from a phone.
//
// A visitor records a few seconds wherever they are, the station's own
// BirdNET scores it, and they confirm which candidate was actually the
// bird before anything is kept. Confirmation is the point: a trusted
// person looking at three candidates beats any confidence threshold, and
// it keeps guesses out of the collage.
//
// Endpoints:
//   POST ?action=submit   multipart: audio, optional lat/lon/accuracy
//                         -> {id}. Row lands as 'pending'; the worker
//                         (scripts/submission_worker.py) picks it up.
//   GET  ?action=result&id=N -> {status, candidates:[{sci,com,conf}]}
//   POST ?action=confirm  JSON {id, sci} -> keeps that identification.
//                         sci must be one the analyser actually offered,
//                         so a client cannot invent a species.
//   POST ?action=reject   JSON {id} -> discards it, audio and all.
//   GET  ?action=list&limit=N -> confirmed submissions, newest first.
//
// Submissions live in their own table, deliberately not in `detections`:
// the collage, stats and the BirdWeather export all read that table, and
// nothing that reads it should have to learn about a second provenance.
//
// Auth is the station's: direct private-address requests are open unless
// the owner enables the LAN admin gate; forwarded and public-host
// requests always need the password.

declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

require_once __DIR__ . '/admin-auth.php';
avian_require_admin();

$BIRDNETPI_DIR = dirname(__DIR__, 2);
$DB_PATH = "$BIRDNETPI_DIR/scripts/birds.db";
$CONF    = '/etc/birdnet/birdnet.conf';

const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;   // ~8 MB; 15s of phone audio is ~250 KB
const MAX_PENDING      = 20;                // backlog guard, not a rate limit
const KEEP_CANDIDATES  = 5;

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
    $pdo->exec('CREATE TABLE IF NOT EXISTS submissions (
        Id          INTEGER PRIMARY KEY AUTOINCREMENT,
        Created     TEXT NOT NULL,
        Status      TEXT NOT NULL,
        Sci_Name    TEXT,
        Com_Name    TEXT,
        Confidence  REAL,
        Candidates  TEXT,
        Lat         REAL,
        Lon         REAL,
        Accuracy    REAL,
        Audio       TEXT NOT NULL,
        Submitter   TEXT,
        Error       TEXT
    )');
    $pdo->exec('CREATE INDEX IF NOT EXISTS submissions_status ON submissions (Status)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS submissions_created ON submissions (Created DESC)');
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

$action = (string)($_GET['action'] ?? '');
$pdo = db($DB_PATH);

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

    $pending = (int)$pdo->query("SELECT COUNT(*) FROM submissions WHERE Status = 'pending'")
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

    $extracted = conf_value($CONF, 'EXTRACTED', (getenv('HOME') ?: '/home/pi') . '/BirdSongs/Extracted');
    $day = gmdate('Y-m-d');
    $dir = "$extracted/Submissions/$day";
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

    $stmt = $pdo->prepare('INSERT INTO submissions
        (Created, Status, Lat, Lon, Accuracy, Audio, Submitter)
        VALUES (:created, :status, :lat, :lon, :acc, :audio, :who)');
    $stmt->execute([
        ':created' => gmdate('c'),
        ':status'  => 'pending',
        ':lat'     => num_or_null($_POST['lat'] ?? null, -90, 90),
        ':lon'     => num_or_null($_POST['lon'] ?? null, -180, 180),
        ':acc'     => num_or_null($_POST['accuracy'] ?? null, 0, 100000),
        ':audio'   => "Submissions/$day/$name",
        ':who'     => mb_substr(trim((string)($_POST['submitter'] ?? '')), 0, 40) ?: null,
    ]);

    echo json_encode(['ok' => true, 'id' => (int)$pdo->lastInsertId(), 'status' => 'pending']);
    exit;
}

// ---------------------------------------------------------------- result
if ($action === 'result') {
    $id = (int)($_GET['id'] ?? 0);
    if ($id <= 0) fail('bad id');
    $stmt = $pdo->prepare('SELECT Id, Status, Candidates, Audio, Error, Sci_Name, Com_Name
                           FROM submissions WHERE Id = :id');
    $stmt->execute([':id' => $id]);
    $row = $stmt->fetch();
    if (!$row) fail('no such submission', 404);

    $candidates = [];
    if (!empty($row['Candidates'])) {
        $decoded = json_decode((string)$row['Candidates'], true);
        if (is_array($decoded)) $candidates = array_slice($decoded, 0, KEEP_CANDIDATES);
    }
    echo json_encode([
        'ok'         => true,
        'id'         => (int)$row['Id'],
        'status'     => $row['Status'],
        'candidates' => $candidates,
        'audio'      => $row['Audio'],
        'sci'        => $row['Sci_Name'],
        'com'        => $row['Com_Name'],
        'error'      => $row['Error'],
    ]);
    exit;
}

// --------------------------------------------------------------- confirm
if ($action === 'confirm') {
    if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') fail('confirm requires POST', 405);
    $body = json_body();
    $id  = (int)($body['id'] ?? 0);
    $sci = trim((string)($body['sci'] ?? ''));
    if ($id <= 0 || $sci === '') fail('confirm needs id and sci');

    $stmt = $pdo->prepare('SELECT Candidates, Status FROM submissions WHERE Id = :id');
    $stmt->execute([':id' => $id]);
    $row = $stmt->fetch();
    if (!$row) fail('no such submission', 404);
    if ($row['Status'] === 'confirmed') fail('already confirmed', 409);

    // Only a species the analyser actually offered. Without this the
    // endpoint would be a way to write arbitrary names into the site.
    $candidates = json_decode((string)($row['Candidates'] ?? '[]'), true) ?: [];
    $match = null;
    foreach ($candidates as $c) {
        if (is_array($c) && ($c['sci'] ?? null) === $sci) { $match = $c; break; }
    }
    if ($match === null) fail('that species was not one of the candidates');

    $upd = $pdo->prepare('UPDATE submissions
        SET Status = :s, Sci_Name = :sci, Com_Name = :com, Confidence = :conf
        WHERE Id = :id');
    $upd->execute([
        ':s'    => 'confirmed',
        ':sci'  => $sci,
        ':com'  => (string)($match['com'] ?? $sci),
        ':conf' => (float)($match['conf'] ?? 0),
        ':id'   => $id,
    ]);
    echo json_encode(['ok' => true, 'id' => $id, 'status' => 'confirmed',
                      'sci' => $sci, 'com' => $match['com'] ?? $sci]);
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
    $extracted = conf_value($CONF, 'EXTRACTED', (getenv('HOME') ?: '/home/pi') . '/BirdSongs/Extracted');
    $path = $extracted . '/' . (string)$row['Audio'];
    $real = realpath($path);
    $base = realpath($extracted . '/Submissions');
    if ($real !== false && $base !== false && str_starts_with($real, $base . '/')) {
        @unlink($real);
    }
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
                                  Lat, Lon, Audio, Submitter
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
            'lat'   => $r['Lat'] === null ? null : (float)$r['Lat'],
            'lon'   => $r['Lon'] === null ? null : (float)$r['Lon'],
            'audio' => $r['Audio'],
            'who'   => $r['Submitter'],
        ];
    }
    echo json_encode(['ok' => true, 'submissions' => $out]);
    exit;
}

fail('unknown action; try submit, result, confirm, reject or list', 404);
