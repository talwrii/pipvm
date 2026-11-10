import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipvm import cli, projects
from pipvm.util import Error


def write_project(directory, text):
    (Path(directory) / "pyproject.toml").write_text(text)


class ReadingPyproject(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_reads_dependencies_extras_and_scripts_without_running_anything(self):
        write_project(self.directory, '''
[build-system]
requires = ["evil-backend"]
build-backend = "evil.backend"
[project]
name = "demo"
dependencies = ["numpy>=1.2", "requests[socks]==2.*; python_version > '3.8'"]
[project.optional-dependencies]
plot = ["matplotlib"]
[project.scripts]
demo = "demo:main"
''')
        project = projects.read(self.directory)
        self.assertEqual(project.name, "demo")
        self.assertEqual(project.requirements(), ["numpy>=1.2", "requests[socks]==2.*; python_version > '3.8'"])
        self.assertEqual(project.requirements(["plot"])[-1], "matplotlib")
        self.assertEqual(project.scripts, {"demo": "demo:main"})
        self.assertEqual(project.python_path(), [str(self.directory)])

    def test_a_src_layout_puts_src_on_the_path(self):
        write_project(self.directory, '[project]\nname = "d"\n')
        (self.directory / "src").mkdir()
        self.assertEqual(projects.read(self.directory).python_path(), [str(self.directory / "src")])

    def test_what_cannot_be_read_without_a_build_is_refused(self):
        for text in ('[tool.poetry]\nname = "x"\n',                                  # no [project] table
                     '[project]\nname = "x"\ndynamic = ["dependencies"]\n',           # computed when built
                     "this is not toml ["):
            write_project(self.directory, text)
            with self.assertRaises(Error):
                projects.read(self.directory)

    def test_an_unknown_extra_is_an_error(self):
        write_project(self.directory, '[project]\nname = "x"\n')
        with self.assertRaises(Error):
            projects.read(self.directory).requirements(["nope"])

    def test_only_plain_index_requirements_are_accepted(self):
        good = ["numpy", "numpy>=1.2,<3", "requests[socks]==2.*; python_version > '3.8'", "Foo_Bar"]
        self.assertEqual(projects.check_requirements(good), good)
        for bad in ("-e .", "--index-url https://evil", "pkg @ https://evil/pkg.whl", "https://evil/x.whl",
                    "./local", "/abs/path", "", "a\nb"):
            with self.assertRaises(Error, msg=bad):
                projects.check_requirements([bad])


class Scripts(unittest.TestCase):
    def test_wheels_only_unless_building_is_allowed(self):
        self.assertIn("--only-binary :all:", cli.install_script(False))
        self.assertNotIn("--only-binary", cli.install_script(True))

    def test_requirements_are_arguments_never_part_of_the_script(self):
        self.assertIn('"$@"', cli.install_script(False))
        self.assertIn("--without-pip", cli.install_script(False))

    def test_the_run_script_sets_the_path_and_the_source(self):
        script = cli.exec_script(["/p/src"], "/work dir", True)
        self.assertIn('export PATH="$HOME/venv/bin:$PATH"', script)
        self.assertIn("export PYTHONPATH=/p/src", script)
        self.assertIn("cd '/work dir'", script)
        self.assertTrue(script.rstrip().endswith('exec "$@"'))
        self.assertNotIn("venv", cli.exec_script([], "/w", False))


class Exec(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.project = self.directory / "proj"
        self.project.mkdir()
        write_project(self.project, '[project]\nname = "proj"\ndependencies = ["numpy"]\n')
        self.data = self.directory / "data"
        self.data.mkdir()

    def run_exec(self, *argv, install_status=0, run_status=0):
        machines = []

        def fake_machine(*args, **kwargs):
            machine = patch("pipvm.cli.Machine").start().return_value if False else _Fake(kwargs)
            machines.append(machine)
            return machine

        class _Fake:
            def __init__(self, kwargs):
                self.kwargs, self.commands = kwargs, []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def wait_ready(self):
                pass

            def mount_shares(self):
                self.mounted = True

            def run(self, command, tty=False):
                self.commands.append(command)
                return install_status if self.kwargs.get("net") and not self.kwargs.get("snapshot") else run_status

        with patch("pipvm.cli.image.ensure_prepared", return_value=Path("base.qcow2")), \
             patch("pipvm.cli.image.ensure_key", return_value=Path("key")), \
             patch("pipvm.cli.subprocess.run"), patch("pipvm.cli.log"), \
             patch("pipvm.cli.Machine", side_effect=fake_machine):
            args = cli.parser().parse_args(["exec", *argv])
            try:
                status = cli.exec_run(args)
            except Error as error:
                status = error
        return status, machines

    def test_two_phases_the_install_has_network_and_none_of_your_files(self):
        status, machines = self.run_exec("--project", str(self.project), "--with", "scipy",
                                         "--share", f"{self.data}:rw", "--", "python3", "x.py")
        self.assertEqual(status, 0)
        install, run = machines
        self.assertTrue(install.kwargs["net"])
        self.assertNotIn("shares", install.kwargs)                  # nothing mounted while the network is up
        self.assertFalse(install.kwargs.get("snapshot"))            # so the install is kept for the run
        self.assertEqual(install.commands[0][4:], ["numpy", "scipy"])
        self.assertIn("--only-binary", install.commands[0][2])

    def test_the_run_has_no_network_a_snapshot_and_the_shares(self):
        _, (install, run) = self.run_exec("--project", str(self.project), "--share", f"{self.data}:rw", "--", "echo", "hi")
        self.assertFalse(run.kwargs["net"])
        self.assertTrue(run.kwargs["snapshot"])
        shares = {share.mountpoint: share.readonly for share in run.kwargs["shares"]}
        self.assertEqual(shares, {str(self.project): True, str(self.data): False})   # source read-only, data as asked
        self.assertEqual(run.commands[0][4:], ["echo", "hi"])
        self.assertIn(f"cd {self.data}", run.commands[0][2])                       # starts in the first --share
        self.assertIn(f"PYTHONPATH={self.project}", run.commands[0][2])

    def test_nothing_to_install_means_a_single_offline_vm(self):
        _, machines = self.run_exec("--no-project", "--", "true")
        self.assertEqual(len(machines), 1)
        self.assertFalse(machines[0].kwargs["net"])

    def test_net_and_allow_build_are_overrides(self):
        _, (install, run) = self.run_exec("--no-project", "--with", "x", "--net", "--allow-build", "--", "true")
        self.assertTrue(run.kwargs["net"])
        self.assertNotIn("--only-binary", install.commands[0][2])

    def test_a_failed_install_stops_before_the_command_and_says_how_to_override(self):
        status, machines = self.run_exec("--no-project", "--with", "x", "--", "true", install_status=1)
        self.assertIsInstance(status, Error)
        self.assertIn("--allow-build", str(status))
        self.assertEqual(len(machines), 1)                                          # the command never ran

    def test_the_commands_exit_status_is_returned(self):
        self.assertEqual(self.run_exec("--no-project", "--", "false", run_status=3)[0], 3)

    def test_bad_requirements_and_missing_command_are_refused(self):
        for argv in (["--no-project", "--with=-e", "--", "true"], ["--no-project", "--with", "a @ http://x", "--", "true"],
                     ["--no-project"], ["--project", str(self.directory), "--", "true"]):
            self.assertIsInstance(self.run_exec(*argv)[0], Error, argv)


if __name__ == "__main__":
    unittest.main()
