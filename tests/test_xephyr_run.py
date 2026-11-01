"""Runs xephyr-run's own tests as part of pipvm's, so a change to the subrepo cannot quietly break pipvm."""
import sys
import unittest
from pathlib import Path

SUBREPO = Path(__file__).resolve().parent.parent / "xephyr-run"
TESTS = SUBREPO / "tests"

# xephyr-run's tests import its package by its own name, `xephyr_run`, which is the subrepo's real
# directory. pipvm uses the same files as `pipvm.xephyr_run` through the symlink.
sys.path.insert(0, str(SUBREPO))


class SubrepoPresent(unittest.TestCase):
    def test_the_xephyr_run_submodule_is_checked_out(self):
        self.assertTrue(TESTS.is_dir(), "xephyr-run/ is empty: run `git submodule update --init`")


class ThroughThePipvmPackage(unittest.TestCase):
    """The code as pipvm uses it, `pipvm.xephyr_run`, which is a symlink into the subrepo."""

    def test_relative_imports_and_file_lookups_work_through_the_symlink(self):
        from pipvm.xephyr_run import cli, connect_filter, lazy, native, registry  # noqa: F401
        self.assertTrue(native.SOURCE.is_file())
        self.assertEqual(native.SOURCE.read_bytes(), (SUBREPO / "xephyr_run" / "connect_filter.c").read_bytes())

    def test_a_confined_command_runs(self):
        # Starts the helper by the path of connect_filter.py (or builds and runs the compiled supervisor).
        from pipvm.xephyr_run import connect_filter
        process = connect_filter.popen_confined_rules(["sh", "-c", "exit 5"], [("deny", "^@")])
        self.assertEqual(process.wait(timeout=60), 5)


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(SubrepoPresent))
    suite.addTests(loader.loadTestsFromTestCase(ThroughThePipvmPackage))
    if TESTS.is_dir():
        suite.addTests(loader.discover(str(TESTS), top_level_dir=str(TESTS)))
    return suite


if __name__ == "__main__":
    unittest.main()
