<?php
// The submissions table, in one place.
//
// This exists because it was in two places and they drifted. The API knew
// how to migrate an older table; the preview seeder had its own CREATE
// TABLE IF NOT EXISTS, which is a no-op against a database that already
// has the table - so seeding a copy of a real birds.db died on a column
// that had never been added:
//
//     table submissions has no column named Place
//
// A schema written down twice is a schema that will disagree with itself.
// Both callers now come here.

declare(strict_types=1);

require_once __DIR__ . '/places.php';

/**
 * Create the table if it is missing, add any column a older station does
 * not have yet, and backfill place names for rows written before the
 * columns existed.
 *
 * Safe to call on every request: the CREATE and the index builds are
 * no-ops once done, and the migration only looks at PRAGMA output.
 *
 * @param float|null $homeLat station position, so a backfilled row made at
 * @param float|null $homeLon the station is labelled Home rather than by
 *                            its district.
 */
function avian_submissions_schema(PDO $pdo, ?float $homeLat = null, ?float $homeLon = null): void {
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
        Error       TEXT,
        Place       TEXT,
        Area        TEXT
    )');

    // Place and Area arrived after the first stations were already
    // running, so add them to an existing table rather than requiring a
    // wipe. CREATE TABLE IF NOT EXISTS does nothing for a table that is
    // already there, which is exactly how this got missed once.
    $have = [];
    foreach ($pdo->query('PRAGMA table_info(submissions)') as $column) {
        $have[(string)$column['name']] = true;
    }
    $added = false;
    foreach (['Place' => 'TEXT', 'Area' => 'TEXT'] as $column => $type) {
        if (!isset($have[$column])) {
            $pdo->exec("ALTER TABLE submissions ADD COLUMN $column $type");
            $added = true;
        }
    }

    $pdo->exec('CREATE INDEX IF NOT EXISTS submissions_status ON submissions (Status)');
    $pdo->exec('CREATE INDEX IF NOT EXISTS submissions_created ON submissions (Created DESC)');

    if ($added) avian_submissions_backfill_places($pdo, $homeLat, $homeLon);
}

/**
 * Name the rows that were stored before submit learned to do it.
 *
 * Only rows that still have a coordinate can be named, and only rows with
 * no place yet are touched, so this cannot overwrite a name somebody has
 * already seen. It runs once, immediately after the columns are added.
 */
function avian_submissions_backfill_places(PDO $pdo, ?float $homeLat, ?float $homeLon): int {
    $rows = $pdo->query('SELECT Id, Lat, Lon FROM submissions
                         WHERE Place IS NULL AND Lat IS NOT NULL AND Lon IS NOT NULL');
    $update = $pdo->prepare('UPDATE submissions SET Place = :place, Area = :area WHERE Id = :id');
    $named = 0;
    foreach ($rows as $row) {
        $place = avian_place_for((float)$row['Lat'], (float)$row['Lon'], $homeLat, $homeLon);
        if ($place['place'] === null && $place['area'] === null) continue;
        $update->execute([
            ':place' => $place['place'],
            ':area'  => $place['area'],
            ':id'    => (int)$row['Id'],
        ]);
        $named++;
    }
    return $named;
}
