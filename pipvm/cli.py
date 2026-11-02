"""pipvm: install Python programs with pipx inside a virtual machine, and run them there."""
import argparse
import os
import signal
import subprocess
import sys
import tempfile

from . import apps, image
from .util import Error, log
from .vm import GUEST_HOME, GUEST_USER, Machine, Share
from .xephyr_run import cli as xephyr
from .xephyr_run import lazy

# Per-app settings that `pipvm set` knows about, and what they mean.
SETTINGS = {
    "xephyr": "show the program's windows in a Xephyr window that starts when it first uses X (default: on)",
    "config": "let the program see your ~/.config/<app>, read-only, if it exists (default: on)",
}
VALUES = {"on": True, "off": False, "default": None}


def install(args):
    name = apps.check_name(args.name or apps.default_name(args.package))
    base = image.ensure_prepared()
    key = image.ensure_key()
    app = apps.create(name, base, args.package)
    try:
        log(f"installing {args.package} into a new VM disk for {name}")
        with Machine(app.disk, app.logs, key=key, net=True, memory=args.memory, cpus=args.cpus) as machine:
            machine.wait_ready()
            status = machine.run(["pipx", "install", args.package])
            if status != 0:
                raise Error(f"pipx could not install {args.package} (status {status})")
            # The programs pipx linked into the user's bin directory are the package's commands.
            exes = machine.output(["sh", "-c", "ls -1 ~/.local/bin"]).split()
        written, skipped = apps.write_shims(app, exes)
    except BaseException:
        app.remove()
        raise
    log(f"installed {name}: " + (", ".join(written) if written else "no commands"))
    for exe in skipped:
        log(f"not creating {apps.bin_dir() / exe}: something else is already there. "
            f"Run it with: pipvm run --exe {exe} {name}")
    if written and str(apps.bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep):
        log(f"{apps.bin_dir()} is not on your PATH")
    return 0


def supports(app, minimum):
    """Whether the image the app was made on is new enough (image.DISPLAY_REVISION, image.SHARE_REVISION)."""
    return image.base_revision(app.info().get("base", "")) >= minimum


def reinstall_hint(app):
    return f"pipvm uninstall {app.name} && pipvm install {app.spec()}"


def feature_wanted(setting, flag, app, minimum, what):
    """Whether to use an optional feature: this run's flag wins, then the app's saved setting, then the
    default, which is on. An app made on an image too old for the feature runs without it, unless it was
    asked for outright."""
    wanted = flag if flag is not None else app.setting(setting, True)
    if wanted and not supports(app, minimum):
        if flag:
            raise Error(f"{app.name} was installed on an older image that cannot {what}; "
                        f"reinstall it: {reinstall_hint(app)}")
        return False
    return wanted


def xephyr_wanted(flag, app):
    return feature_wanted("xephyr", flag, app, image.DISPLAY_REVISION, "show windows")


def config_share(flag, app):
    """Your ~/.config/<app> from the host, read-only, as ~/.config/<app> in the VM; None if the feature is
    off, the app's image is too old for it, or you have no such directory."""
    if not feature_wanted("config", flag, app, image.SHARE_REVISION, "be given your configuration"):
        return None
    host = apps.host_config_directory() / app.name
    if not host.is_dir():
        return None
    return Share(host, "config", f"{GUEST_HOME}/.config/{app.name}", readonly=True)


def run_with_display(machine, argv, title, tty=False):
    """Run the command with an X display forwarded into the VM from a Xephyr that is started only when
    the program first connects to X. A program that never uses X never causes a window."""
    display = lazy.LazyXephyr(lazy.free_display(), "640x400", f"pipvm-{title}")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))   # so a kill still cleans up
    display.open()
    process = None
    try:
        process = machine.popen(argv, tty=tty, x_socket=str(display.path))
        return xephyr.follow(process, display)
    finally:
        if process and process.poll() is None:
            process.terminate()
        display.close()


def run(args):
    app = apps.get(args.name)
    command = args.args[1:] if args.args[:1] == ["--"] else args.args
    argv = [args.exe or app.name, *command] if not args.shell else []
    key = image.ensure_key()
    if image.base_revision(app.info().get("base", "")) < image.REVISION:
        log(f"{app.name} was installed on an older image, so it may lack windows or your ~/.config/{app.name}. "
            f"To update it: {reinstall_hint(app)}")
    # Everything the program writes to its disk is thrown away when it stops, and unless --net is
    # given it has no network. Your ~/.config/<app> is visible to it, read-only. A new machine is
    # started for every run.
    config = config_share(args.config, app)
    with tempfile.TemporaryDirectory(prefix="pipvm-run-") as logs:
        with Machine(app.disk, logs, key=key, net=args.net, snapshot=True, memory=args.memory,
                     cpus=args.cpus, shares=[config] if config else []) as machine:
            machine.wait_ready()
            machine.mount_shares()
            if xephyr_wanted(args.xephyr, app):
                return run_with_display(machine, argv, app.name, tty=args.shell)
            return machine.run(argv, user=GUEST_USER, tty=args.shell)


def set_setting(args):
    app = apps.get(args.name)
    if args.setting not in SETTINGS:
        raise Error(f"unknown setting {args.setting!r}; the settings are: " + ", ".join(SETTINGS))
    if args.value not in VALUES:
        raise Error(f"{args.setting} can be: " + ", ".join(VALUES))
    app.set_setting(args.setting, VALUES[args.value])
    log(f"{app.name}: {args.setting} is now {args.value}")
    return 0


def list_apps(args):
    for app in apps.installed():
        saved = app.setting("xephyr")
        xephyr_state = "default" if saved is None else ("on" if saved else "off")
        print(f"{app.name}\t{app.spec()}\t{' '.join(app.exes())}\txephyr {xephyr_state}")
    return 0


def uninstall(args):
    apps.get(args.name).remove()
    return 0


def parser():
    p = argparse.ArgumentParser(description="Install Python programs in a virtual machine, and run them there.")
    sub = p.add_subparsers(dest="action", required=True)

    def sizes(command):
        command.add_argument("--memory", default="1G", help="Guest memory (default: %(default)s)")
        command.add_argument("--cpus", type=int, default=2, help="Guest CPUs (default: %(default)s)")

    installer = sub.add_parser("install", help="Install a package with pipx in a new VM disk")
    installer.add_argument("package", help="A pip requirement, e.g. cowsay or 'httpie==3.2.2'")
    installer.add_argument("--name", help="Name for the app (default: the package name)")
    sizes(installer)

    runner = sub.add_parser("run", help="Start a fresh VM and run the program in it; options go before NAME")
    runner.add_argument("--net", action="store_true", help="Give the program internet access (none by default)")
    runner.add_argument("--exe", help="The program to run (default: the app's name)")
    runner.add_argument("--shell", action="store_true", help="Open a shell in the VM instead")
    runner.add_argument("--config", action=argparse.BooleanOptionalAction, default=None,
                        help="Show the program your ~/.config/<app> (read-only), or not. Without this, the app's "
                             "saved setting is used (see `pipvm set`), and then the default: on")
    runner.add_argument("--xephyr", action=argparse.BooleanOptionalAction, default=None,
                        help="Show the program's windows in a lazily started Xephyr window, or not. "
                             "Without this, the app's saved setting is used (see `pipvm set`), and then the default: on")
    sizes(runner)
    runner.add_argument("name")
    runner.add_argument("args", nargs=argparse.REMAINDER, help="-- ARGS for the program")

    sub.add_parser("list", help="List installed apps")
    setter = sub.add_parser("set", help="Change an app's saved setting, which runs without a flag use",
                            description="Settings: " + "; ".join(f"{key}: {text}" for key, text in SETTINGS.items()))
    setter.add_argument("name")
    setter.add_argument("setting", choices=sorted(SETTINGS))
    setter.add_argument("value", choices=sorted(VALUES), help="on, off, or default (forget the saved value)")
    remover = sub.add_parser("uninstall", help="Delete an app and its disk")
    remover.add_argument("name")
    return p


def main():
    args = parser().parse_args()
    try:
        return {"install": install, "run": run, "list": list_apps, "set": set_setting,
                "uninstall": uninstall}[args.action](args)
    except Error as error:
        print(f"pipvm: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"pipvm: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
