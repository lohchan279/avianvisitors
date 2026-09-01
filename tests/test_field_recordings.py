"""Field recordings and the Map view.

Two things are worth pinning here. The first is that the fourth view is
wired consistently: a tab, a section, a title and a clamp that reaches
it. Those live in four different files, and three of them are files
upstream rewrites, so a merge can silently leave the tab pointing at
nothing.

The second is that the API keeps its promise about coordinates. The
station records where a clip was made because the occurrence filter needs
it; the site only ever shows a place name. That is a property of the
source, not of a comment, so it is asserted against the source.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


class FieldRecordingTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    # ---- the fourth view -------------------------------------------
    def test_map_view_is_wired_end_to_end(self):
        html = self.read("avian/frontend/index.html")
        apt = self.read("avian/frontend/apt.js")

        self.assertIn('<button type="button" data-i="3">map</button>', html)
        self.assertIn('<section class="view" id="v3"', html)

        titles = re.search(r"var VIEW_TITLES = \[(.*?)\];", apt)
        self.assertIsNotNone(titles, "VIEW_TITLES is gone")
        self.assertEqual(4, len(re.findall(r"'[^']*'", titles.group(1))),
                         "the fourth view has no title")

        # The clamp has to follow the array, or the tab is unreachable.
        self.assertIn("Math.min(VIEW_TITLES.length - 1, i)", apt)
        self.assertNotIn("Math.min(2, i)", apt)

    def test_every_tab_has_a_view_to_slide_to(self):
        html = self.read("avian/frontend/index.html")
        tabs = sorted(re.findall(r'data-i="(\d+)"', html))
        views = sorted(re.findall(r'<section class="view" id="v(\d+)"', html))
        self.assertEqual(tabs, views)

    def test_new_frontend_files_are_published(self):
        # A file missing from the manifest 404s in production while
        # working perfectly from a checkout, so it looks fine locally.
        manifest = self.read("scripts/link_webroot.sh")
        for name in ("field.js", "field.css", "sg-map.js"):
            self.assertIn(f'"${{frontend_dir}}/{name}"', manifest, name)
            self.assertIn(f'"{name}"', manifest, name)

    def test_submissions_endpoint_is_served(self):
        # /avian/api/* is an allowlist; an unlisted endpoint gets a 404.
        self.assertIn("submissions.php", self.read("scripts/update_caddyfile.sh"))

    # ---- what leaves the station ------------------------------------
    def test_the_api_answers_in_place_names_not_coordinates(self):
        api = self.read("avian/api/submissions.php")
        self.assertNotRegex(api, r"'lat'\s*=>")
        self.assertNotRegex(api, r"'lon'\s*=>")
        self.assertIn("'place'", api)

    def test_the_map_data_is_never_reachable_as_a_url(self):
        # The browser copy is published; the API copy must not be, or
        # resolving positions server-side buys nothing.
        manifest = self.read("scripts/link_webroot.sh")
        self.assertNotIn("sg-areas.php", manifest)
        self.assertNotIn("sg-areas.php", self.read("scripts/update_caddyfile.sh"))

    # ---- the worker --------------------------------------------------
    def test_the_worker_applies_the_occurrence_filter(self):
        worker = self.read("scripts/submission_worker.py")
        # analyzeAudioData's second return value is the species the range
        # model admits at this place and week. Discarding it is how a
        # phone clip in Singapore gets offered an American vireo.
        self.assertIn("raw, predicted = analyzeAudioData", worker)
        self.assertIn("sci_name not in predicted", worker)
        self.assertIn("from utils.classes import birdnet_week", worker)

    def test_a_clip_it_cannot_delete_does_not_undo_the_verdict(self):
        """PHP owns the directory; the worker only visits it.

        The upload arrives through the web server, which creates the
        day's directory as its own user. Unlinking a file needs write
        permission on the directory that holds it, not on the file, so
        the worker - running as the station owner - can be refused. It
        deletes the clip behind an "unsure" verdict, and that delete used
        to run after the verdict was written and outside any guard: the
        PermissionError fell through to the catch-all, which overwrote a
        correctly analysed row with "failed" and put [Errno 13] on the
        caller's phone. The clip is housekeeping. The verdict is the
        product.
        """
        import importlib.util
        import sqlite3
        import stat
        import sys
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "submission_worker", ROOT / "scripts" / "submission_worker.py")
        worker = importlib.util.module_from_spec(spec)
        sys.modules["submission_worker"] = worker
        spec.loader.exec_module(worker)

        with tempfile.TemporaryDirectory() as box:
            extracted = pathlib.Path(box)
            day = extracted / "Submissions" / "2026-09-01"
            day.mkdir(parents=True)
            clip = day / "061605-81baaf637f7e.webm"
            clip.write_bytes(b"not really audio")

            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE submissions (
                Id INTEGER PRIMARY KEY, Created TEXT, Status TEXT, Sci_Name TEXT,
                Com_Name TEXT, Confidence REAL, Candidates TEXT, Lat REAL, Lon REAL,
                Audio TEXT, Error TEXT)""")
            db.execute("""INSERT INTO submissions
                (Id, Created, Status, Lat, Lon, Audio) VALUES
                (1, '2026-09-01T06:16:05+00:00', 'analysing', 1.3, 103.9,
                 'Submissions/2026-09-01/061605-81baaf637f7e.webm')""")
            db.commit()

            # Below the accept bar, so the worker takes the branch that
            # deletes the clip.
            worker.to_wav = lambda src, wav: wav.write_bytes(b"") or True
            worker.analyse = lambda *a, **k: [
                {"sci": "Turdus mandarinus", "com": "Chinese Blackbird", "conf": 0.11}]
            worker.accept_threshold = lambda: 0.5
            # Reaches into BirdNET's utils package, which is not importable
            # here and is not what this test is about.
            worker.submission_week = lambda created: 35

            # Read and execute, but not write: exactly what the worker
            # gets when the web server made the directory. Root ignores
            # that, and skipping under root would mean this never runs in
            # a container - so there the refusal is raised directly. The
            # guard under test is the same either way.
            real_unlink = pathlib.Path.unlink
            as_root = os.geteuid() == 0
            if as_root:
                def refuse(self, *a, **k):
                    if self == clip:
                        raise PermissionError(13, "Permission denied", str(clip))
                    return real_unlink(self, *a, **k)
                pathlib.Path.unlink = refuse
            else:
                os.chmod(day, stat.S_IRUSR | stat.S_IXUSR)
            try:
                row = db.execute("SELECT * FROM submissions WHERE Id = 1").fetchone()
                worker.process(db, row, extracted)
            finally:
                pathlib.Path.unlink = real_unlink
                os.chmod(day, stat.S_IRWXU)

            got = db.execute("SELECT Status, Error FROM submissions WHERE Id = 1").fetchone()

        self.assertEqual(
            got["Status"], "unsure",
            f"a clip that could not be deleted overwrote the verdict: {dict(got)}")
        self.assertNotIn("Permission denied", got["Error"] or "")
        self.assertNotIn("Errno 13", got["Error"] or "")

    # ---- the preview harness ----------------------------------------
    def test_preview_still_reads_both_real_lists(self):
        # preview.sh derives its webroot and its API allowlist from the
        # production scripts rather than restating them, which is the only
        # reason a preview can catch a file missing from either. If the
        # formatting of those scripts drifts, the extraction silently
        # yields nothing and the preview quietly serves less than it
        # should - so check the seams here rather than at 8am on the Pi.
        manifest = subprocess.run(
            ["bash", "-c",
             "sed -n '/^  sources=(/,/^  )/p' scripts/link_webroot.sh"
             " | sed -n 's/^ *\"\\(.*\\)\"$/\\1/p'"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, check=True,
        ).stdout.split()
        self.assertGreater(len(manifest), 20, "the webroot manifest did not parse")
        self.assertTrue(any(name.endswith("sg-map.js") for name in manifest))

        allowlist = subprocess.run(
            ["bash", "-c",
             "sed -n 's#.*not path /avian/api/\\(.*\\)#\\1#p' scripts/update_caddyfile.sh"
             " | head -1 | tr ' ' '\\n' | sed 's#^/avian/api/##' | grep '[.]php$'"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, check=True,
        ).stdout.split()
        self.assertIn("submissions.php", allowlist)
        self.assertNotIn("sg-areas.php", allowlist)
        self.assertNotIn("places.php", allowlist)
        self.assertNotIn("access-auth.php", allowlist)

    def test_preview_overrides_cannot_reach_a_real_request(self):
        # Every override the preview relies on is read behind a PHP_SAPI
        # check. FPM serves the station and is neither of the SAPIs those
        # checks admit, so none of these can move a path or open a gate on
        # a request that matters. That is the whole reason letting a
        # preview redirect the database and the admin gate is acceptable.
        def guarded(source: str, pattern: str, label: str) -> None:
            found = list(re.finditer(pattern, source))
            self.assertTrue(found, f"{label} is gone")
            for match in found:
                window = source[max(0, match.start() - 260):match.end() + 260]
                self.assertIn("PHP_SAPI", window, f"{label} is not gated on the SAPI")
                self.assertNotIn("fpm", window, f"{label} would apply under FPM")

        # submissions.php reads both of its overrides through one helper,
        # so the helper is what has to carry the guard.
        guarded(self.read("avian/api/submissions.php"),
                r"(?s)function preview_path\(.*?\n\}", "preview_path()")
        for name in ("AV_DB_FILE", "AV_BIRDNET_CONF"):
            self.assertIn(f"preview_path('{name}'", self.read("avian/api/submissions.php"))

        for relative, variable in (
            ("avian/api/birdnet-api.php", "AV_DB_FILE"),
            ("avian/api/access-auth.php", "AV_ACCESS_CONF"),
            ("avian/api/access-auth.php", "AV_ACCESS_CERTS"),
            ("avian/api/admin-auth.php", "AV_REQUIRE_AUTH"),
        ):
            guarded(self.read(relative), rf"getenv\('{variable}'\)",
                    f"{variable} in {relative}")

    def test_preview_checks_the_port_before_doing_any_work(self):
        # php -S only discovers a taken port at the very end, after the
        # webroot is linked and the database seeded. On a BirdNET-Pi 8080
        # is often already spoken for, so that was a lot of work thrown
        # away over a number nobody chose.
        preview = self.read("avian/scripts/preview.sh")
        self.assertLess(preview.index("port_free"), preview.index("mktemp -d"),
                        "the port check runs after the scratch directory is built")

        # An explicitly chosen port is refused rather than silently moved.
        import socket
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            result = subprocess.run(
                ["bash", "avian/scripts/preview.sh", "--port", str(port)],
                cwd=ROOT, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=120, check=False,
            )
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("already in use", result.stdout)

    def test_the_map_draws_even_when_the_catches_are_refused(self):
        # The districts are geography: no permission, no data. Fetching
        # them together with the list meant one 401 hid the whole map and
        # left an empty box, which reads as broken rather than locked.
        field = self.read("avian/frontend/field.js")
        self.assertNotIn("Promise.all([loadShape(), api('list')])", field)
        self.assertIn("var drawn = loadShape()", field)
        # And a refusal has to say what to do about it.
        self.assertIn("error.status === 401", field)
        self.assertIn("Unlock with the station admin password", field)

    def test_home_carries_what_the_station_has_heard(self):
        # A station is where most of the birds are. A map that showed only
        # the clips somebody carried a phone to would be a map of the
        # exception, and it read as "0 caught" on a station with hundreds.
        api = self.read("avian/api/submissions.php")
        self.assertIn("'heard'", api)
        # Walked along the (Date DESC, Time DESC) index rather than grouped
        # over the whole table, which would be a full scan every request.
        self.assertIn("ORDER BY Date DESC, Time DESC LIMIT :scan", api)
        self.assertIn("STATION_SCAN", api)

        field = self.read("avian/frontend/field.js")
        self.assertIn("function renderStation", field)
        self.assertIn("function atHome", field)
        # And an empty field list has to point at where the birds are.
        self.assertIn("what the station has heard at home", field)

    def test_a_backgrounded_preview_survives_the_shell_and_stops_cleanly(self):
        # Foreground was the wrong default for something you publish and
        # then walk around with: the shell it runs in is the same shell you
        # need for the install command, and typing into it kills it.
        import socket
        import time
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        script = str(ROOT / "avian/scripts/preview.sh")
        started = subprocess.run(
            ["bash", script, "--background", "--port", str(port)],
            cwd=ROOT, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=120, check=False,
        )
        try:
            self.assertEqual(0, started.returncode, started.stdout)
            self.assertIn("running in the background", started.stdout)

            # The parent has exited; the preview must still be answering.
            deadline = time.time() + 20
            alive = False
            while time.time() < deadline and not alive:
                with socket.socket() as check:
                    check.settimeout(1)
                    alive = check.connect_ex(("127.0.0.1", port)) == 0
                if not alive:
                    time.sleep(0.5)
            self.assertTrue(alive, "the detached preview is not listening")
        finally:
            subprocess.run(["bash", script, "--stop"], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=False)

        # --stop has to take the whole process group: setsid makes the
        # child a group leader and php -S forks workers, so killing one
        # process leaves the port held.
        deadline = time.time() + 20
        while time.time() < deadline:
            with socket.socket() as check:
                check.settimeout(1)
                if check.connect_ex(("127.0.0.1", port)) != 0:
                    break
            time.sleep(0.5)
        else:
            self.fail("the port is still held after --stop")

    # ---- publishing the preview at a sublink -------------------------
    def test_expose_refuses_the_mode_that_opens_the_admin_gate(self):
        # --as admin turns the password gate off, which is free on
        # localhost and expensive behind the tunnel: only birds.db and
        # birdnet.conf are redirected, so config.php would still write the
        # real station config. The refusal has to come before any work.
        result = subprocess.run(
            ["bash", "avian/scripts/preview.sh", "--expose", "--as", "admin"],
            cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=60, check=False,
        )
        self.assertEqual(64, result.returncode, result.stdout)
        self.assertIn("will not run with --as admin", result.stdout)

    def test_expose_defaults_to_the_password_gate_and_says_why_it_cannot(self):
        # The admin credential state is root:caddy 0640, so a preview run
        # by an ordinary shell cannot read it and every request would 401
        # whatever password was typed. Failing with the fix beats failing
        # with a login form that never works.
        result = subprocess.run(
            ["bash", "avian/scripts/preview.sh", "--expose"],
            cwd=ROOT, env=dict(os.environ, AV_ADMIN_STATE_FILE="/nonexistent"),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=120, check=False,
        )
        self.assertEqual(77, result.returncode, result.stdout)
        self.assertIn("password-reset", result.stdout)

        # The unreadable-but-present case needs a root:caddy file, which a
        # test cannot portably make, so assert the remedy it prints. It has
        # to be a command that works: sg(1) is not, because it asks for a
        # group password that does not exist unless you are already a
        # member - which is exactly when you would not need it.
        preview = self.read("avian/scripts/preview.sh")
        self.assertIn("sudo -u $me -g caddy", preview)
        self.assertIn("sudo usermod -aG caddy $me", preview)

    def test_exposed_preview_moves_the_admin_session_cookie(self):
        # The session cookie is scoped to /avian/. Served under /preview/
        # the browser would set it and never send it back, so unlocking
        # would appear to work and silently not.
        self.assertIn("'path' => '/avian/'", self.read("avian/api/admin-auth.php"))
        overlay = self.read("avian/scripts/preview-expose.sh")

        # (?i) is the whole fix. PHP writes the attribute lower case and
        # Go's regexp is case sensitive, so a capitalised pattern matched
        # nothing and failed exactly as if the line were missing - which
        # is what made it cost an evening to find. A tidy-up that drops
        # the flag would silently reintroduce it.
        self.assertIn('header_down Set-Cookie "(?i)path=/avian/" "path=/preview/avian/"',
                      overlay)

        # And the block carries a version, so an overlay written by an
        # older checkout is reported rather than quietly serving old rules.
        self.assertIn("BLOCK_VERSION=", overlay)
        self.assertIn("$BEGIN v$BLOCK_VERSION", overlay)

    def test_expose_narrows_the_api_to_endpoints_that_cannot_mutate(self):
        preview = self.read("avian/scripts/preview.sh")
        narrow = re.search(r"grep -E '\^\(([^)]*)\)\\\.php\$'", preview)
        self.assertIsNotNone(narrow, "the --expose allowlist filter is gone")
        allowed = set(narrow.group(1).split("|"))
        # Everything that writes to the station, spawns work, or reports
        # its configuration stays out.
        for endpoint in ("config", "maintenance", "generate", "export",
                         "birdweather", "birdnet-status", "archive"):
            self.assertNotIn(endpoint, allowed, f"{endpoint}.php is exposed")
        self.assertIn("submissions", allowed)
        self.assertIn("birdnet-api", allowed)

    def test_expose_overlay_round_trip_preserves_everything_else(self):
        # The overlay already carries the /stream route that keeps live
        # audio working on the public host. Adding and removing a preview
        # block must leave it byte-identical - losing that route is a
        # silent regression nobody would connect to a preview.
        import tempfile
        begin = "# >>> avian preview (temporary)"
        end = "# <<< avian preview"
        original = (
            "# Local Caddy overlay.\n"
            "@accessStream {\n\tpath /stream\n"
            "\theader Cf-Access-Jwt-Assertion *\n}\n"
            "handle @accessStream {\n\treverse_proxy localhost:8000\n}\n"
        )
        strip = ("awk -v b='%s' -v e='%s' "
                 "'index($0,b){skip=1} !skip{print} index($0,e){skip=0}'" % (begin, end))
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / "overlay.caddy"
            source.write_text(original, encoding="utf-8")
            installed = subprocess.run(
                ["bash", "-c", f"{strip} {source}"],
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout + "\n".join([
                begin,
                "handle_path /preview/* {",
                "\treverse_proxy 127.0.0.1:8080",
                "}",
                end,
                "",
            ])
            self.assertIn("reverse_proxy localhost:8000", installed)

            (pathlib.Path(tmp) / "installed.caddy").write_text(installed, encoding="utf-8")
            removed = subprocess.run(
                ["bash", "-c", f"{strip} {pathlib.Path(tmp) / 'installed.caddy'}"],
                text=True, stdout=subprocess.PIPE, check=True,
            ).stdout
            self.assertEqual(original, removed, "removing the block changed the overlay")

    # ---- the schema -------------------------------------------------
    @unittest.skipUnless(shutil.which("php"), "PHP CLI is unavailable")
    def test_seeding_migrates_a_database_from_before_the_place_columns(self):
        # CREATE TABLE IF NOT EXISTS does nothing to a table that already
        # exists, so a copy of a real birds.db kept the old schema and
        # seeding died on "no column named Place". The schema lives in one
        # place now; this is the case that caught the drift.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            database = pathlib.Path(tmp) / "old.db"
            root = pathlib.Path(tmp) / "root"
            root.mkdir()

            with sqlite3.connect(database) as db:
                db.execute("""CREATE TABLE submissions (
                    Id INTEGER PRIMARY KEY AUTOINCREMENT, Created TEXT NOT NULL,
                    Status TEXT NOT NULL, Sci_Name TEXT, Com_Name TEXT,
                    Confidence REAL, Candidates TEXT, Lat REAL, Lon REAL,
                    Accuracy REAL, Audio TEXT NOT NULL, Submitter TEXT, Error TEXT)""")
                db.execute(
                    "INSERT INTO submissions (Created, Status, Sci_Name, Com_Name,"
                    " Confidence, Lat, Lon, Audio) VALUES (?,?,?,?,?,?,?,?)",
                    ("2026-08-20T09:00:00+00:00", "confirmed", "Cinnyris jugularis",
                     "Olive-backed Sunbird", 0.8, 1.3430, 103.8280,
                     "Submissions/old/x.webm"),
                )

            seeded = subprocess.run(
                ["php", str(ROOT / "avian/scripts/preview-seed.php"),
                 str(database), str(root)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=120, check=False,
            )
            self.assertEqual(0, seeded.returncode, seeded.stdout)

            with sqlite3.connect(database) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(submissions)")}
                self.assertIn("Place", columns)
                self.assertIn("Area", columns)
                # The row written before the columns existed gets a name,
                # rather than sitting blank on the map forever.
                place, area = db.execute(
                    "SELECT Place, Area FROM submissions WHERE Audio = ?",
                    ("Submissions/old/x.webm",),
                ).fetchone()
            self.assertEqual("Central Water Catchment", place)
            self.assertEqual("Central Water Catchment", area)

    def test_the_submissions_schema_is_written_down_once(self):
        # Two copies of a schema is how the migration got missed.
        for relative in ("avian/api/submissions.php", "avian/scripts/preview-seed.php"):
            source = self.read(relative)
            self.assertIn("avian_submissions_schema(", source, relative)
            self.assertNotIn("CREATE TABLE IF NOT EXISTS submissions", source, relative)

    # ---- turning Access on -------------------------------------------
    @unittest.skipUnless(shutil.which("php"), "PHP CLI is unavailable")
    def test_access_setup_preserves_the_rest_of_birdnet_conf(self):
        # birdnet.conf carries local keys nothing upstream knows about -
        # LIVESTREAM_FILTER, EXTRACTION_FILTER. Losing one to a config
        # edit would change how the station sounds, silently.
        import tempfile
        original = (
            'SITE_NAME="ghlyms"\n'
            "LATITUDE=1.3690\n"
            'LIVESTREAM_FILTER="highpass=f=900"\n'
            'EXTRACTION_FILTER="highpass 900"\n'
        )
        aud = "9f8c2b1e7d4a6053c1e8b2f7a94d6053c1e8b2f7a94d60539f8c2b1e7d4a6053"
        with tempfile.TemporaryDirectory() as tmp:
            conf = pathlib.Path(tmp) / "birdnet.conf"
            conf.write_text(original, encoding="utf-8")
            environment = dict(os.environ, AV_ACCESS_CONF=str(conf))
            script = str(ROOT / "avian/scripts/access-setup.sh")

            first = subprocess.run(
                ["bash", script, "install", "team.cloudflareaccess.com", aud],
                env=environment, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=60, check=False,
            )
            written = conf.read_text(encoding="utf-8")
            self.assertIn("ACCESS_TEAM_DOMAIN=team.cloudflareaccess.com", written)
            self.assertIn(f"ACCESS_AUD={aud}", written)
            for line in original.splitlines():
                self.assertIn(line, written, f"{line} was lost\n{first.stdout}")

            # Setting it twice must update, not accumulate.
            subprocess.run(
                ["bash", script, "install", "team.cloudflareaccess.com", "b" * 64],
                env=environment, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=60, check=False,
            )
            again = conf.read_text(encoding="utf-8")
            self.assertEqual(1, again.count("ACCESS_TEAM_DOMAIN="))
            self.assertEqual(1, again.count("ACCESS_AUD="))
            self.assertIn("ACCESS_AUD=" + "b" * 64, again)

    @unittest.skipUnless(shutil.which("php"), "PHP CLI is unavailable")
    def test_access_setup_rejects_values_that_cannot_be_right(self):
        script = str(ROOT / "avian/scripts/access-setup.sh")
        for team, aud in (("not a domain", "a" * 64),
                          ("team.cloudflareaccess.com", "short")):
            result = subprocess.run(
                ["bash", script, "install", team, aud],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=60, check=False,
            )
            self.assertEqual(65, result.returncode, result.stdout)

    # ---- going live ---------------------------------------------------
    def test_go_live_checks_the_served_site_not_the_checkout(self):
        # Reading files off disk would agree with itself and prove
        # nothing. These are the two failures that actually happened: a
        # file never linked into the webroot, and a cache token that never
        # moved after a pull.
        import http.server
        import socketserver
        import threading

        index = (b'<!doctype html><link rel="stylesheet" href="./styles.css?v=r9">'
                 b'<button type="button" data-i="3">map</button>')

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    body, status = index, 200
                elif path in ("/field.js", "/field.css"):
                    body, status = b"ok", 200
                elif path == "/avian/api/submissions.php":
                    body, status = b'{"ok":false}', 401
                else:
                    # sg-map.js was never linked; sg-areas.php must 404 too.
                    body, status = b"404", 404
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        site = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            result = subprocess.run(
                ["bash", "avian/scripts/go-live.sh"],
                cwd=ROOT, env=dict(os.environ, AV_SITE=site),
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=120, check=False,
            )
        finally:
            server.shutdown()

        self.assertEqual(1, result.returncode, result.stdout)
        self.assertRegex(result.stdout, r"the cache token matches the checkout\s+NO")
        self.assertRegex(result.stdout, r"/sg-map\.js is served\s+NO")
        # A guarded endpoint is routed; only a 404 means the allowlist is
        # stale, so 401 has to read as a pass.
        self.assertRegex(result.stdout, r"the submissions endpoint is routed\s+ok")

    @unittest.skipUnless(shutil.which("php"), "PHP CLI is unavailable")
    def test_php_access_and_place_suite(self):
        result = subprocess.run(
            ["php", "tests/test_field_access_auth.php"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertRegex(result.stdout, r"field access tests passed \([0-9]+ checks\)")


if __name__ == "__main__":
    unittest.main()
