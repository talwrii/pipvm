import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipvm import apps, cli
from pipvm.util import Error
from pipvm.vm import GUEST_USER, Machine


class Names(unittest.TestCase):
    def test_default_name_is_the_normalised_package_name(self):
        self.assertEqual(apps.default_name("cowsay"), "cowsay")
        self.assertEqual(apps.default_name("Foo_Bar.baz[extra]==1.2"), "foo-bar-baz")
        self.assertEqual(apps.default_name("httpie>=3"), "httpie")

    def test_cannot_name_something_unsafe(self):
        for bad in ("", "../x", "a b", "-x", "A"):
            with self.assertRaises(Error):
                apps.check_name(bad)

    def test_no_package_name_in_a_url_or_path(self):
        for bad in ("", "/tmp/thing", "./thing"):
            with self.assertRaises(Error):
                apps.default_name(bad)


class Image(unittest.TestCase):
    def test_the_guest_allows_the_host_to_forward_its_display_socket_in(self):
        # Alpine's sshd refuses remote forwards by default, and uses the first value it finds in its
        # config, so the default line must be removed and not just overridden.
        from pipvm import image
        self.assertIn("AllowTcpForwarding remote", image.PROVISION)
        self.assertIn("-e '/^#\\?AllowTcpForwarding/d'", image.PROVISION)
        self.assertIn("StreamLocalBindUnlink yes", image.PROVISION)

    def test_a_guest_can_draw_windows(self):
        from pipvm import image
        self.assertIn("python3-tkinter", image.PROVISION)


class MachineCommand(unittest.TestCase):
    def machine(self, **kwargs):
        return Machine("/d/disk.qcow2", "/d/logs", key="/d/key", **kwargs)

    def netdev(self, machine):
        command = machine.command()
        return command[command.index("-netdev") + 1]

    def test_no_network_by_default_but_ssh_is_forwarded(self):
        netdev = self.netdev(self.machine())
        self.assertIn("restrict=on", netdev)
        self.assertIn("hostfwd=tcp:127.0.0.1:", netdev)

    def test_net_removes_the_restriction(self):
        self.assertNotIn("restrict=on", self.netdev(self.machine(net=True)))

    def test_snapshot_discards_writes(self):
        self.assertIn("-snapshot", self.machine(snapshot=True).command())
        self.assertNotIn("-snapshot", self.machine().command())

    def test_runs_in_the_guest_as_the_unprivileged_user(self):
        machine = self.machine()
        command = machine.run_command(["cowsay", "hello world"])
        self.assertIn(f"{GUEST_USER}@127.0.0.1", command)
        self.assertNotIn("root@127.0.0.1", command)
        self.assertIn("-T", command)   # no terminal: stdout and stderr stay separate
        self.assertEqual(command[-1], 'PATH="$HOME/.local/bin:$PATH" exec cowsay \'hello world\'')

    def test_no_display_is_forwarded_unless_asked(self):
        command = self.machine().run_command(["xeyes"])
        self.assertNotIn("-R", command)
        self.assertNotIn("DISPLAY", command[-1])

    def test_a_display_is_forwarded_into_the_guest_as_display_zero(self):
        command = self.machine().run_command(["xeyes"], x_socket="/tmp/.X11-unix/X93")
        self.assertEqual(command[command.index("-R") + 1], "/tmp/.X11-unix/X0:/tmp/.X11-unix/X93")
        self.assertIn("ExitOnForwardFailure=yes", command)   # no silent run without a display
        self.assertTrue(command[-1].startswith("DISPLAY=:0 "))

    def test_the_readiness_probe_makes_the_directory_for_the_display_socket(self):
        machine = self.machine()
        machine.proc = type("Running", (), {"poll": lambda self: None})()
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            machine.wait_ready()
        self.assertEqual(run.call_args.args[0][-1], "mkdir -p /tmp/.X11-unix")

    def test_shell_gets_a_terminal(self):
        command = self.machine().run_command([], tty=True)
        self.assertIn("-tt", command)
        self.assertEqual(command[-1], "exec sh -l")

    def test_readiness_probe_does_not_consume_the_programs_stdin(self):
        machine = self.machine()
        machine.proc = type("Running", (), {"poll": lambda self: None})()
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            machine.wait_ready()
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_run_returns_the_programs_exit_status(self):
        with patch("subprocess.call", return_value=3) as call:
            self.assertEqual(self.machine().run(["false"]), 3)
        call.assert_called_once()


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is not installed")
class AppDisks(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.base = self.directory / "base.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(self.base), "1M"], check=True)
        self.root = self.directory / "apps"

    def test_create_list_and_remove(self):
        app = apps.create("cowsay", self.base, "cowsay==6", self.root)
        self.assertTrue(app.exists())
        self.assertEqual(app.spec(), "cowsay==6")
        info = subprocess.run(["qemu-img", "info", str(app.disk)], capture_output=True, text=True).stdout
        self.assertIn(str(self.base), info)   # an overlay: the shared image is not copied
        self.assertEqual([a.name for a in apps.installed(self.root)], ["cowsay"])
        app.remove()
        self.assertEqual(apps.installed(self.root), [])

    def test_cannot_install_the_same_name_twice(self):
        apps.create("cowsay", self.base, "cowsay", self.root)
        with self.assertRaises(Error):
            apps.create("cowsay", self.base, "cowsay", self.root)

    def test_unknown_app(self):
        with self.assertRaises(Error):
            apps.get("nothing", self.root)


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is not installed")
class Shims(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        base = self.directory / "base.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M"], check=True)
        self.bin = self.directory / "bin"
        self.app = apps.create("cowsay", base, "cowsay", self.directory / "apps")

    def test_shim_runs_the_program_in_the_vm(self):
        written, skipped = apps.write_shims(self.app, ["cowsay"], self.bin, pipvm="/opt/my bin/pipvm")
        self.assertEqual((written, skipped), (["cowsay"], []))
        shim = self.bin / "cowsay"
        self.assertTrue(os.access(shim, os.X_OK))
        self.assertEqual(shim.read_text().splitlines()[-1],
                         "exec '/opt/my bin/pipvm' run --exe cowsay cowsay -- \"$@\"")
        self.assertEqual(self.app.exes(), ["cowsay"])

    def test_never_overwrites_something_that_is_not_its_own(self):
        self.bin.mkdir()
        (self.bin / "cowsay").write_text("#!/bin/sh\necho mine\n")
        written, skipped = apps.write_shims(self.app, ["cowsay", "moo"], self.bin, pipvm="pipvm")
        self.assertEqual((written, skipped), (["moo"], ["cowsay"]))
        self.assertIn("echo mine", (self.bin / "cowsay").read_text())

    def test_another_apps_shim_is_not_taken_over(self):
        other = apps.create("other", self.directory / "base.qcow2", "other", self.directory / "apps")
        apps.write_shims(other, ["tool"], self.bin, pipvm="pipvm")
        self.assertEqual(apps.write_shims(self.app, ["tool"], self.bin, pipvm="pipvm"), ([], ["tool"]))

    def test_unsafe_command_names_are_skipped(self):
        self.assertEqual(apps.write_shims(self.app, ["../evil", "ok"], self.bin, pipvm="pipvm"), (["ok"], ["../evil"]))

    def test_removing_the_app_removes_only_its_own_shims(self):
        apps.write_shims(self.app, ["cowsay"], self.bin, pipvm="pipvm")
        (self.bin / "unrelated").write_text("keep me")
        self.app.remove()
        self.assertFalse((self.bin / "cowsay").exists())
        self.assertTrue((self.bin / "unrelated").exists())

    def test_a_replaced_shim_is_left_alone_on_removal(self):
        apps.write_shims(self.app, ["cowsay"], self.bin, pipvm="pipvm")
        (self.bin / "cowsay").write_text("#!/bin/sh\necho later replaced by me\n")
        self.app.remove()
        self.assertTrue((self.bin / "cowsay").exists())


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is not installed")
class XephyrSetting(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        base = self.directory / "base.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M"], check=True)
        self.environment = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.directory),
                                                   "XDG_CACHE_HOME": str(self.directory)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = apps.create("tool", base, "tool")

    def set(self, value):
        cli.set_setting(cli.parser().parse_args(["set", "tool", "xephyr", value]))

    def run_tool(self, *options):
        """Run the app through pipvm's run command, returning which way it ran (display or plain)."""
        with patch("pipvm.cli.image.ensure_key", return_value=self.directory / "key"), \
             patch("pipvm.cli.Machine") as machine, \
             patch("pipvm.cli.run_with_display", return_value=0) as display:
            plain = machine.return_value.__enter__.return_value.run
            plain.return_value = 0
            cli.run(cli.parser().parse_args(["run", *options, "tool"]))
        return "display" if display.called else ("plain" if plain.called else "neither")

    def test_xephyr_is_on_by_default(self):
        self.assertTrue(cli.xephyr_wanted(None, self.app))
        self.assertEqual(self.run_tool(), "display")

    def test_the_saved_setting_changes_the_default(self):
        self.set("off")
        self.assertFalse(self.app.setting("xephyr"))
        self.assertEqual(self.run_tool(), "plain")

    def test_the_flag_beats_the_saved_setting(self):
        self.set("off")
        self.assertEqual(self.run_tool("--xephyr"), "display")
        self.set("on")
        self.assertEqual(self.run_tool("--no-xephyr"), "plain")

    def test_default_forgets_the_saved_value(self):
        self.set("off")
        self.set("default")
        self.assertIsNone(self.app.setting("xephyr"))
        self.assertEqual(self.run_tool(), "display")

    def test_unknown_settings_and_values_are_refused(self):
        for arguments in (["set", "tool", "nonsense", "on"], ["set", "tool", "xephyr", "maybe"]):
            with self.assertRaises(SystemExit), patch("sys.stderr"):
                cli.parser().parse_args(arguments)

    def test_list_shows_the_setting(self):
        self.set("off")
        with patch("builtins.print") as shown:
            cli.list_apps(None)
        self.assertIn("xephyr off", shown.call_args.args[0])


class CommandLine(unittest.TestCase):
    def test_arguments_after_the_name_are_the_programs(self):
        args = cli.parser().parse_args(["run", "--net", "cowsay", "--", "-t", "hi"])
        self.assertTrue(args.net)
        self.assertEqual(args.name, "cowsay")
        self.assertEqual(args.args[-2:], ["-t", "hi"])   # some Python versions keep a leading "--"

    def test_run_is_offline_and_not_persistent_by_default(self):
        args = cli.parser().parse_args(["run", "cowsay"])
        self.assertFalse(args.net)

    def test_run_uses_a_throwaway_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)/"base.qcow2"
            if shutil.which("qemu-img"):
                subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(base), "1M"], check=True)
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory, "XDG_CACHE_HOME": directory}), \
                 patch("pipvm.cli.image.ensure_key", return_value=Path(directory)/"key"), \
                 patch("pipvm.cli.Machine") as machine:
                apps.create("cowsay", base, "cowsay")
                machine.return_value.__enter__.return_value.run.return_value = 0
                self.assertEqual(cli.run(cli.parser().parse_args(["run", "--no-xephyr", "cowsay", "--", "-t", "hi"])), 0)
            options = machine.call_args.kwargs
            self.assertTrue(options["snapshot"])
            self.assertFalse(options["net"])
            run = machine.return_value.__enter__.return_value.run
            self.assertEqual(run.call_args.args[0], ["cowsay", "-t", "hi"])


if __name__ == "__main__":
    unittest.main()
