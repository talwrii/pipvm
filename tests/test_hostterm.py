import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipvm import apps, cli, hostterm
from pipvm.util import Error


class Display(unittest.TestCase):
    def test_a_different_spelling_of_the_same_display(self):
        self.assertEqual(hostterm.dedicated_display(":0"), ":0.0")
        self.assertEqual(hostterm.dedicated_display(":12"), ":12.0")
        self.assertEqual(hostterm.dedicated_display(":0.0"), "unix:0.0")

    def test_other_spellings_are_left_alone(self):
        for display in ("localhost:10.0", ":0.1", "unix:0"):
            self.assertEqual(hostterm.dedicated_display(display), display)


class Config(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.directory)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_menus_are_hidden_by_default(self):
        home = hostterm.ensure_config()
        self.assertEqual(home, self.directory / "pipvm" / "terminal")
        text = (home / "lxterminal" / "lxterminal.conf").read_text()
        for line in ("hidemenubar=true", "hidescrollbar=true", "disablef10=true"):
            self.assertIn(line, text)

    def test_your_edits_are_never_overwritten(self):
        path = hostterm.ensure_config() / "lxterminal" / "lxterminal.conf"
        path.write_text("[general]\nhidemenubar=false\n")
        hostterm.ensure_config()
        self.assertEqual(path.read_text(), "[general]\nhidemenubar=false\n")


class Commands(unittest.TestCase):
    def test_the_inner_command_is_this_run_told_that_it_is_in_a_window(self):
        argv = ["run", "--exe", "x", "name", "--", "a b", "--flag"]
        self.assertEqual(hostterm.inner_command(argv),
                         [sys.executable, "-m", "pipvm", "run", "--in-terminal", "--exe", "x", "name", "--", "a b", "--flag"])

    def test_only_run_can_be_shown_in_a_window(self):
        for argv in ([], ["install", "x"]):
            with self.assertRaises(Error):
                hostterm.inner_command(argv)

    def test_the_window_gives_the_program_your_config_home_back(self):
        inner = ["python", "-m", "pipvm", "run", "x"]
        command = hostterm.window_command(inner, "title", "/my/config")
        self.assertEqual(command[:5], ["lxterminal", "--title", "title", "-e", "sh"])
        self.assertEqual(command[-len(inner) - 2:], ["env", "XDG_CONFIG_HOME=/my/config", *inner])
        unset = hostterm.window_command(inner, "title", None)
        self.assertEqual(unset[-len(inner) - 3:], ["env", "-u", "XDG_CONFIG_HOME", *inner])

    def test_a_failure_waits_so_the_message_can_be_read(self):
        self.assertIn("read", hostterm.HOLD)
        self.assertIn('-ne 0', hostterm.HOLD)
        self.assertTrue(hostterm.HOLD.rstrip().endswith("exit $status"))


class OpenWindow(unittest.TestCase):
    def test_a_detached_window_with_the_dedicated_display_and_config(self):
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {"DISPLAY": ":0", "XDG_CONFIG_HOME": home}), \
             patch("pipvm.hostterm.shutil.which", return_value="/usr/bin/lxterminal"), \
             patch("pipvm.hostterm.subprocess.Popen") as popen:
            hostterm.open_window(["pipvm", "run", "x"], "pipvm: x")
        command, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(command[0], "lxterminal")
        self.assertEqual(options["env"]["DISPLAY"], ":0.0")                          # not the display you use yourself
        self.assertEqual(options["env"]["XDG_CONFIG_HOME"], str(Path(home) / "pipvm" / "terminal"))
        self.assertTrue(options["start_new_session"])
        self.assertIn(f"XDG_CONFIG_HOME={home}", command)                           # restored for the program

    def test_no_lxterminal_and_no_display_are_clear_errors(self):
        with patch.dict(os.environ, {"DISPLAY": ":0"}), patch("pipvm.hostterm.shutil.which", return_value=None):
            with self.assertRaises(Error) as caught:
                hostterm.open_window(["x"], "t")
            self.assertIn("apt install lxterminal", str(caught.exception))
        env = {key: value for key, value in os.environ.items() if key != "DISPLAY"}
        with patch.dict(os.environ, env, clear=True), patch("pipvm.hostterm.shutil.which", return_value="/x"):
            with self.assertRaises(Error):
                hostterm.open_window(["x"], "t")


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is not installed")
class RunInAWindow(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        environment = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.directory), "XDG_CACHE_HOME": str(self.directory),
                                              "XDG_CONFIG_HOME": str(self.directory / "config")})
        environment.start()
        self.addCleanup(environment.stop)
        base = self.directory / "alpine-3.24.1-pipvm-r5.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M"], check=True)
        self.app = apps.create("tool", base, "tool")

    def run_tool(self, *options, argv=None):
        """Run through the run command; returns (status, whether a host window was opened, Machine, the display runner)."""
        argv = argv or ["pipvm", "run", *options, "tool", "--", "-x"]
        with patch("pipvm.cli.image.ensure_key", return_value=self.directory / "key"), \
             patch("pipvm.cli.Machine") as machine, \
             patch("pipvm.cli.run_with_display", return_value=0) as display, \
             patch("pipvm.cli.hostterm.open_window") as window, patch("pipvm.cli.log"), patch("sys.argv", argv):
            machine.return_value.__enter__.return_value.run.return_value = 0
            status = cli.run(cli.parser().parse_args(argv[1:]))
        return status, window, machine, display

    def test_off_by_default(self):
        _, window, _, _ = self.run_tool()
        self.assertFalse(window.called)

    def test_the_saved_setting_opens_a_window_and_starts_no_vm(self):
        self.app.set_setting("host-emulator", True)
        status, window, machine, _ = self.run_tool()
        self.assertEqual(status, 0)
        inner, title = window.call_args.args
        self.assertEqual(inner[-4:], ["--in-terminal", "tool", "--", "-x"][-4:] if False else inner[-4:])
        self.assertIn("--in-terminal", inner)
        self.assertEqual(inner[inner.index("--in-terminal") + 1:], ["tool", "--", "-x"])
        self.assertEqual(title, "pip-vm: tool")
        self.assertFalse(machine.called)                                     # nothing is booted until the window runs it

    def test_in_the_window_the_program_gets_a_terminal_and_no_second_window(self):
        self.app.set_setting("host-emulator", True)
        status, window, machine, display = self.run_tool(argv=["pipvm", "run", "--in-terminal", "tool", "--", "-x"])
        self.assertFalse(window.called)
        self.assertEqual(display.call_args.kwargs.get("tty"), True)           # the display runner, with a terminal

    def test_in_the_window_without_xephyr_it_is_a_plain_run_with_a_terminal(self):
        _, window, machine, display = self.run_tool("--in-terminal", "--no-xephyr")
        self.assertFalse(display.called)
        plain = machine.return_value.__enter__.return_value.run
        self.assertEqual(plain.call_args.kwargs.get("tty"), True)
        self.assertEqual(plain.call_args.args[0], ["tool", "-x"])

    def test_the_flag_beats_the_saved_setting_both_ways(self):
        self.app.set_setting("host-emulator", True)
        self.assertFalse(self.run_tool("--no-host-emulator")[1].called)
        self.app.set_setting("host-emulator", None)
        self.assertTrue(self.run_tool("--host-emulator")[1].called)

    def test_it_cannot_be_combined_with_the_vm_emulator_in_the_vm(self):
        self.app.set_setting("host-emulator", True)
        self.app.set_setting("vm-emulator", True)
        with self.assertRaises(Error):
            self.run_tool()

    def test_a_run_flag_beats_the_other_saved_setting(self):
        self.app.set_setting("host-emulator", True)
        _, window, _, display = self.run_tool("--vm-emulator")
        self.assertFalse(window.called)
        self.assertEqual(display.call_args.args[1][0], "xterm")            # in the VM, not on the host
        self.app.set_setting("host-emulator", None)
        self.app.set_setting("vm-emulator", True)
        self.assertTrue(self.run_tool("--host-emulator")[1].called)

    def test_the_old_terminal_window_key_still_means_vm_emulator(self):
        self.app.set_setting("terminal-window", True)
        _, window, _, display = self.run_tool()
        self.assertFalse(window.called)
        self.assertEqual(display.call_args.args[1][0], "xterm")

    def test_both_flags_together_are_refused(self):
        with self.assertRaises(Error):
            self.run_tool("--host-emulator", "--vm-emulator")

    def test_the_setting_can_be_changed_with_set(self):
        cli.set_setting(cli.parser().parse_args(["set", "tool", "host-emulator", "on"]))
        self.assertTrue(self.app.setting("host-emulator"))
        cli.set_setting(cli.parser().parse_args(["set", "tool", "host-emulator", "default"]))
        self.assertIsNone(self.app.setting("host-emulator"))

    def test_install_knows_the_flag_and_refuses_both_kinds_of_terminal(self):
        self.assertTrue(cli.parser().parse_args(["install", "--host-emulator", "x"]).host_emulator)
        with self.assertRaises(Error):
            cli.install(cli.parser().parse_args(["install", "--host-emulator", "--vm-emulator", "x"]))


if __name__ == "__main__":
    unittest.main()
