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
