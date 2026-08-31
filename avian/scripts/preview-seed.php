<?php
// Invent field catches for a preview run.
//
// Scoring a real clip needs the BirdNET model, which only exists in the
// station's virtualenv, so a preview on a laptop has no way to produce a
// confirmed submission. This fabricates a handful at real coordinates
// around Singapore instead, so the map has something to shade and the
// list has something to play.
//
// It deliberately goes through avian_place_for() rather than writing
// place names directly: naming a fix is the part worth exercising, and
// hard-coding the answers would test nothing.
//
//     php avian/scripts/preview-seed.php <birds.db> <extracted-root>

declare(strict_types=1);

require_once __DIR__ . '/../api/places.php';

[$self, $dbPath, $extracted] = $argv + [null, null, null];
if (!$dbPath || !$extracted) {
    fwrite(STDERR, "usage: preview-seed.php <birds.db> <extracted-root>\n");
    exit(64);
}

$home = [1.3690, 103.8480];

// Real places, so the districts they land in are the real answer.
$catches = [
    ['Cinnyris jugularis',      'Olive-backed Sunbird',      0.83, 1.3810, 103.9530, 'ana'],
    ['Copsychus saularis',      'Oriental Magpie-Robin',     0.71, 1.3805, 103.9490, 'ana'],
    ['Pycnonotus goiavier',     'Yellow-vented Bulbul',      0.64, 1.3430, 103.8280, 'joelo996'],
    ['Halcyon smyrnensis',      'White-throated Kingfisher', 0.90, 1.3691, 103.8481, 'joelo996'],
    ['Acridotheres javanicus',  'Javan Myna',                0.55, 1.2494, 103.8303, 'ana'],
    ['Orthotomus sutorius',     'Common Tailorbird',         0.62, 1.4463, 103.7275, 'joelo996'],
    ['Todiramphus chloris',     'Collared Kingfisher',       0.77, 1.4100, 103.9600, 'ana'],
    ['Gracupica contra',        'Pied Myna',                 0.58, 1.3560, 103.9440, 'joelo996'],
];

/** A short WAV, so the audio endpoint has something real to stream. */
function tone(string $path, float $seconds = 2.0, int $hz = 2200): void {
    $rate = 22050;
    $frames = (int)($rate * $seconds);
    $pcm = '';
    for ($i = 0; $i < $frames; $i++) {
        // A fading chirp reads as "a bird noise" rather than a test tone,
        // and proves the player is really decoding something.
        $envelope = sin(M_PI * $i / $frames) ** 2;
        $sweep = $hz + 600 * sin(2 * M_PI * 3 * $i / $rate);
        $pcm .= pack('v', (int)(12000 * $envelope * sin(2 * M_PI * $sweep * $i / $rate)) & 0xffff);
    }
    $size = strlen($pcm);
    $header = 'RIFF' . pack('V', 36 + $size) . 'WAVEfmt ' . pack('VvvVVvv', 16, 1, 1, $rate, $rate * 2, 2, 16)
        . 'data' . pack('V', $size);
    file_put_contents($path, $header . $pcm);
}

$pdo = new PDO('sqlite:' . $dbPath, null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
$pdo->exec('CREATE TABLE IF NOT EXISTS submissions (
    Id INTEGER PRIMARY KEY AUTOINCREMENT, Created TEXT NOT NULL, Status TEXT NOT NULL,
    Sci_Name TEXT, Com_Name TEXT, Confidence REAL, Candidates TEXT, Lat REAL, Lon REAL,
    Accuracy REAL, Audio TEXT NOT NULL, Submitter TEXT, Error TEXT, Place TEXT, Area TEXT)');

$day = gmdate('Y-m-d');
$dir = "$extracted/Submissions/$day";
if (!is_dir($dir) && !mkdir($dir, 0755, true) && !is_dir($dir)) {
    fwrite(STDERR, "could not create $dir\n");
    exit(1);
}

$insert = $pdo->prepare('INSERT INTO submissions
    (Created, Status, Sci_Name, Com_Name, Confidence, Lat, Lon, Audio, Submitter, Place, Area)
    VALUES (:created, :status, :sci, :com, :conf, :lat, :lon, :audio, :who, :place, :area)');

foreach ($catches as $index => [$sci, $com, $conf, $lat, $lon, $who]) {
    $name = sprintf('preview-%02d.wav', $index);
    tone("$dir/$name", 2.0, 1800 + 220 * $index);
    $named = avian_place_for($lat, $lon, $home[0], $home[1]);
    $insert->execute([
        ':created' => gmdate('c', time() - $index * 7200),
        ':status'  => 'confirmed',
        ':sci'     => $sci,
        ':com'     => $com,
        ':conf'    => $conf,
        ':lat'     => $lat,
        ':lon'     => $lon,
        ':audio'   => "Submissions/$day/$name",
        ':who'     => $who . '@example.com',
        ':place'   => $named['place'],
        ':area'    => $named['area'],
    ]);
    printf("  %-26s %-24s %s\n", $com, $named['place'] ?? '(unnamed)', $named['area'] ?? '');
}

// A couple of extra catches in one district, so the heat scale has a top.
$busy = $pdo->prepare('INSERT INTO submissions
    (Created, Status, Sci_Name, Com_Name, Confidence, Lat, Lon, Audio, Submitter, Place, Area)
    VALUES (:created, "confirmed", "Pycnonotus goiavier", "Yellow-vented Bulbul", 0.6,
            1.3810, 103.9530, :audio, "ana@example.com", "Pasir Ris", "Pasir Ris")');
for ($i = 0; $i < 12; $i++) {
    $busy->execute([':created' => gmdate('c', time() - 86400 - $i * 600),
                    ':audio' => "Submissions/$day/preview-00.wav"]);
}

echo "seeded " . (count($catches) + 12) . " field catches\n";
