"""Illustrating a newly detected bird, end to end.

Two failures here are invisible from the outside, and the station had
both. A new species arrived, no illustration appeared, and nothing said
why: the log lived in /tmp, said "GEMINI_API_KEY not set", and exited 0.

The first is that the key has one home and this script looked in
another. The settings panel writes GEMINI_API_KEY into birdnet.conf;
generate.php reads it from there. auto_illustrate.py read only
os.environ, and it is spawned by birdnet_analysis, whose service
environment has never carried it - so the manual button worked and the
automatic path could not generate at all.

The second is the cache tokens. Generating the image is not the job;
getting it in front of a browser is. The bumper here targeted
SKETCH_VERSION and IMG_VERSION, which stopped being literals when they
were changed to read ASSET_VERSION off apt.js's own script tag, and the
substitutions had matched nothing ever since. That is the specific way
this rots - a name is refactored in apt.js and the regex in a Python
script quietly stops matching - so the names are asserted here.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
AUTO = ROOT / "avian" / "scripts" / "auto_illustrate.py"


def load_module():
    spec = importlib.util.spec_from_file_location("auto_illustrate_under_test", AUTO)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class KeyLookupTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.box = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.box, True)
        self.conf = self.box / "birdnet.conf"
        self.module.CONF_PATHS = [self.conf]
        self.previous = os.environ.pop("GEMINI_API_KEY", None)
        if self.previous is not None:
            self.addCleanup(os.environ.__setitem__, "GEMINI_API_KEY", self.previous)

    def test_the_key_is_read_from_birdnet_conf(self):
        # Quoted is how the settings panel writes it.
        self.conf.write_text('SITE_NAME=GHLYMS\nGEMINI_API_KEY="AIza-from-settings"\n')
        self.assertEqual(self.module.gemini_key(), "AIza-from-settings")

    def test_an_unquoted_key_reads_the_same(self):
        self.conf.write_text("GEMINI_API_KEY=AIza-unquoted\n")
        self.assertEqual(self.module.gemini_key(), "AIza-unquoted")

    def test_the_environment_still_wins(self):
        # A cron entry or a unit file that sets it explicitly must not be
        # overridden by whatever happens to be in the conf.
        self.conf.write_text('GEMINI_API_KEY="from-the-conf"\n')
        os.environ["GEMINI_API_KEY"] = "from-the-environment"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        self.assertEqual(self.module.gemini_key(), "from-the-environment")

    def test_a_key_written_twice_takes_the_later_value(self):
        # birdnet.conf is sourced as a shell file, so the last assignment
        # is the one that counts. config.php's read_conf and
        # admin_control.sh's conf_value both already work this way; a
        # reader that took the first would generate against a key the
        # settings panel does not show, with nothing reporting the split.
        self.conf.write_text(
            'GEMINI_API_KEY="the-old-one"\n'
            "SITE_NAME=GHLYMS\n"
            'GEMINI_API_KEY="the-one-settings-just-saved"\n')
        self.assertEqual(self.module.gemini_key(), "the-one-settings-just-saved")

    def test_no_key_anywhere_is_an_empty_string_not_a_crash(self):
        self.conf.write_text("SITE_NAME=GHLYMS\n")
        self.assertEqual(self.module.gemini_key(), "")

    def test_a_missing_conf_is_not_an_error(self):
        self.module.CONF_PATHS = [self.box / "nowhere" / "birdnet.conf"]
        self.assertEqual(self.module.gemini_key(), "")


class CacheTokenTests(unittest.TestCase):
    """The tokens the bumper edits have to exist in apt.js.

    This is the assertion that would have caught the dead bumper. It is
    deliberately coupled to apt.js's source: that coupling is the bug,
    and the only useful thing to do with it is make it loud.
    """

    def setUp(self):
        self.module = load_module()
        self.apt = (ROOT / "avian" / "frontend" / "apt.js").read_text(encoding="utf-8")

    def test_the_token_the_bumper_edits_is_still_a_literal(self):
        self.assertRegex(
            self.apt, r"TABLE_VERSION\s*=\s*'r\d+'",
            "auto_illustrate.bump_versions increments a TABLE_VERSION literal in "
            "apt.js. It is no longer one, so the bump silently does nothing and a "
            "newly illustrated bird keeps its placeholder in every browser.")

    def test_that_token_is_what_gates_the_tables(self):
        # dims.json is what the atlas consults to decide a species has art
        # ("needsArt = !DIMS[slug]"), so the token on that fetch is the one
        # a new species needs moved.
        self.assertRegex(
            self.apt, r"'\?v='\s*\+\s*TABLE_VERSION",
            "dims.json/masks.json are no longer fetched against TABLE_VERSION, so "
            "bumping it no longer makes a new bird's silhouette reachable.")
        self.assertIn("fetch('./dims.json' + q)", self.apt)

    def test_the_versions_it_used_to_bump_are_derived_now(self):
        # Left as a note to whoever reads bump_versions and wonders why it
        # no longer touches these: the site token carries them.
        for name in ("SKETCH_VERSION", "IMG_VERSION"):
            self.assertRegex(
                self.apt, name + r"\s*=\s*ASSET_VERSION",
                f"{name} is a literal again; bump_versions no longer moves it.")

    def test_both_tokens_actually_move(self):
        box = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, box, True)
        for part in ("avian/scripts", "avian/frontend"):
            (box / part).mkdir(parents=True)
        shutil.copy(AUTO, box / "avian" / "scripts")
        shutil.copy(ROOT / "avian" / "scripts" / "bump_version.sh", box / "avian" / "scripts")
        for name in ("apt.js", "index.html"):
            shutil.copy(ROOT / "avian" / "frontend" / name, box / "avian" / "frontend")

        apt = box / "avian" / "frontend" / "apt.js"
        index = box / "avian" / "frontend" / "index.html"
        before_table = re.search(r"TABLE_VERSION\s*=\s*'r(\d+)'", apt.read_text()).group(1)
        before_site = max(int(n) for n in re.findall(r"\?v=r(\d+)", index.read_text()))

        spec = importlib.util.spec_from_file_location(
            "auto_illustrate_in_a_box", box / "avian" / "scripts" / "auto_illustrate.py")
        sandboxed = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = sandboxed
        spec.loader.exec_module(sandboxed)
        sandboxed.bump_versions()

        after_table = re.search(r"TABLE_VERSION\s*=\s*'r(\d+)'", apt.read_text()).group(1)
        after_site = max(int(n) for n in re.findall(r"\?v=r(\d+)", index.read_text()))

        self.assertEqual(int(after_table), int(before_table) + 1,
                         "the tables token did not move; dims.json stays cached")
        self.assertGreater(after_site, before_site,
                           "the site token did not move; browsers keep the old apt.js "
                           "and never see the new TABLE_VERSION")


class UncutRepairTests(unittest.TestCase):
    """An image drawn but never cut was invisible to every later run.

    get_missing asks only whether the file exists, so a run that stopped
    between generating and cutting left the bird sitting on the cream
    square it was drawn on, and nothing ever came back for it. The
    species had its file, so it was not missing.
    """

    def setUp(self):
        self.module = load_module()
        self.box = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.box, True)
        self.module.ILLUST_DIR = self.box
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - Pillow is a station dependency
            self.skipTest("Pillow not available")
        self.Image = Image

    def write_uncut(self, slug):
        # What pregen leaves behind: the bird on its generation ground.
        self.Image.new("RGB", (64, 64), (245, 238, 220)).save(self.box / f"{slug}.png")

    def write_cut_palette(self, slug):
        # How the library stores a finished one, after optimisation.
        blank = self.Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        blank.convert("P", palette=self.Image.ADAPTIVE).save(
            self.box / f"{slug}.png", transparency=0)

    def write_cut_rgba(self, slug):
        self.Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(self.box / f"{slug}.png")

    def test_it_finds_the_one_still_on_its_background(self):
        self.write_uncut("oriolus-xanthornus")
        self.write_cut_palette("picus-canus")
        self.write_cut_rgba("mixornis-gularis")
        species = [("Oriolus xanthornus", "Black-hooded Oriole"),
                   ("Picus canus", "Gray-headed Woodpecker"),
                   ("Mixornis gularis", "Pin-striped Tit-Babbler")]
        self.assertEqual(self.module.uncut_illustrations(species),
                         ["oriolus-xanthornus"])

    def test_a_species_with_no_file_is_not_reported_as_uncut(self):
        # That is get_missing's job, and reporting it here would hand
        # cutout.py a slug with nothing behind it, which is an error.
        self.assertEqual(
            self.module.uncut_illustrations([("Corvus splendens", "House Crow")]), [])

    def test_an_unreadable_file_is_skipped_not_fatal(self):
        (self.box / "broken-bird.png").write_bytes(b"not a png")
        self.assertEqual(
            self.module.uncut_illustrations([("Broken bird", "Broken")]), [])

    def test_the_repair_survives_the_early_return(self):
        # The bug in miniature: with every file present, main used to
        # announce "all detected species have illustrations" and stop -
        # before anything looked at whether they had been cut.
        source = (ROOT / "avian" / "scripts" / "auto_illustrate.py").read_text()
        early = source.index("All detected species have illustrations")
        checked = source.index("stranded = uncut_illustrations(species)")
        self.assertLess(
            checked, early,
            "uncut_illustrations must be consulted before the early return, "
            "or an image left on its background is never found again")


class HelperInterpreterTests(unittest.TestCase):
    """The cutout has to run under an interpreter that has rembg.

    The detection hook spawns this script with sys.executable, which is
    birdnet_analysis's own venv. That has the model; it does not have
    rembg. So generation, which needs far less, succeeded while cutout.py
    exited with "needs Pillow + rembg" - and the bird stayed on the cream
    ground it was drawn on. From the outside the cutout step simply never
    ran.
    """

    def setUp(self):
        self.module = load_module()
        self.box = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.box, True)
        self.real = sys.executable

    def fake_python(self, name, with_rembg):
        root = self.box / name
        (root / "lib").mkdir(parents=True)
        if with_rembg:
            (root / "lib" / "rembg").mkdir()
            (root / "lib" / "rembg" / "__init__.py").write_text("")
        exe = root / "python3"
        exe.write_text(f'#!/bin/sh\nexec env PYTHONPATH="{root}/lib" '
                       f'PYTHONNOUSERSITE=1 {self.real} "$@"\n')
        exe.chmod(0o755)
        return str(exe)

    def resolve(self, started_with, home):
        self.module._HELPER_PYTHON = None
        real_exe, real_home = sys.executable, pathlib.Path.home
        sys.executable = started_with
        pathlib.Path.home = staticmethod(lambda: pathlib.Path(home))
        try:
            return self.module.helper_python()
        finally:
            sys.executable, pathlib.Path.home = real_exe, real_home

    def test_it_keeps_the_one_it_was_started_with_when_that_works(self):
        good = self.fake_python("good", with_rembg=True)
        self.assertEqual(self.resolve(good, self.box / "nowhere"), good)

    def test_it_falls_back_to_birdvenv_when_started_without_rembg(self):
        bare = self.fake_python("bare", with_rembg=False)
        good = self.fake_python("good", with_rembg=True)
        home = self.box / "home"
        (home / "birdvenv" / "bin").mkdir(parents=True)
        (home / "birdvenv" / "bin" / "python3").symlink_to(good)
        self.assertEqual(self.resolve(bare, home),
                         str(home / "birdvenv" / "bin" / "python3"))

    def test_with_rembg_nowhere_it_says_so_rather_than_pretending(self):
        bare = self.fake_python("bare", with_rembg=False)
        got = self.resolve(bare, self.box / "nowhere")
        self.assertEqual(got, bare)  # nothing better exists; the warning is the point

    def test_the_helpers_all_go_through_the_resolver(self):
        source = (ROOT / "avian" / "scripts" / "auto_illustrate.py").read_text()
        for helper in ("pregen.py", "cutout.py", "build_masks.py"):
            self.assertIn(f'[helper_python(), str(SCRIPT_DIR / "{helper}")', source,
                          f"{helper} is not launched through helper_python()")
        self.assertNotIn("[PYTHON,", source)

    def test_a_failed_cutout_stops_the_run(self):
        # Ignoring cutout's exit code is what let a station rebuild masks
        # from uncut images, move the cache token, and print "Done".
        source = (ROOT / "avian" / "scripts" / "auto_illustrate.py").read_text()
        self.assertIn("if run_cutout(slug_args) != 0:", source)
        self.assertIn("if run_build_masks() != 0:", source)
        cut = source.index("if run_cutout(slug_args) != 0:")
        bump = source.index("bump_versions()", cut)
        self.assertIn("return 1", source[cut:bump],
                      "a failed cutout must not reach bump_versions")


class LockTests(unittest.TestCase):
    def test_the_lock_is_released_however_the_run_ends(self):
        # reporting.py treats a lock under ten minutes old as "a run is in
        # progress" and stays quiet, so a run that returns without
        # clearing it mutes the next ten minutes of new species. Several
        # paths returned early; the release belongs in a finally.
        source = (ROOT / "avian" / "scripts" / "auto_illustrate.py").read_text()
        body = source[source.index("def main() -> int:"):]
        self.assertRegex(
            body, r"try:\s*\n\s*return run\(\)\s*\n\s*finally:\s*\n\s*LOCKFILE\.unlink",
            "the lock is not released in a finally, so an early return leaks it")


class DetectionHookTests(unittest.TestCase):
    def test_a_new_detection_launches_the_illustrator(self):
        # The trigger is a hook in the analysis loop, not only a cron - so
        # a new bird is illustrated within minutes rather than at the next
        # half hour. Asserted against the source because running it needs
        # the BirdNET model.
        reporting = (ROOT / "scripts" / "utils" / "reporting.py").read_text(encoding="utf-8")
        analysis = (ROOT / "scripts" / "birdnet_analysis.py").read_text(encoding="utf-8")
        self.assertIn("maybe_auto_illustrate(detection)", analysis)
        self.assertIn("auto_illustrate.py", reporting)
        self.assertIn("start_new_session=True", reporting)


if __name__ == "__main__":
    unittest.main()
