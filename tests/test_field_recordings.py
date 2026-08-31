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
