"""pip-vm: install Python programs with pipx inside a virtual machine, and run them there."""
import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from . import apps, hostterm, image, projects
from .util import Error, log
from .vm import GUEST_HOME, GUEST_USER, Machine, Share
from .xephyr_run import cli as xephyr
from .xephyr_run import lazy

# Per-app settings that `pip-vm set` knows about, and what they mean.
SETTINGS = {
    "xephyr": "show the program's windows in a Xephyr window that starts when it first uses X (default: on)",
    "config": "let the program read and write your ~/.config/<app>, if it exists (default: on)",
    "vm-emulator": "run the program in a terminal window (an xterm, shown in the Xephyr window) (default: off)",
    "host-emulator": "run the program in a window of a shared LXTerminal on your side, the VM running only the program (default: off)",
}
VALUES = {"on": True, "off": False, "default": None}


SOURCE_MOUNT = "/mnt/pipvm-src"   # where a local project directory is shared into the VM, read-only


def local_source(spec):
    """The directory of a local Python project when the spec looks like a path (./x, ~/x, /x/y), else None.
    A bare name such as `cowsay` is always a package to fetch, even if a directory of that name exists."""
    if not (os.sep in spec or spec.startswith((".", "~"))):
        return None
    path = Path(spec).expanduser()
    if not path.is_dir() or not ((path / "pyproject.toml").exists() or (path / "setup.py").exists()):
        raise Error(f"{spec} is not a Python project directory (no pyproject.toml or setup.py in it)")
    return path.resolve()


def install(args):
    if args.host_emulator and args.vm_emulator:
        raise Error("--host-emulator and --vm-emulator are two ways of showing a terminal: pick one")
    source = local_source(args.package)
    spec = str(source) if source else args.package
    name = apps.check_name(args.name or (re.sub(r"[-_.]+", "-", source.name).lower() if source
                                         else apps.default_name(args.package)))
    base = image.ensure_prepared()
    key = image.ensure_key()
    app = apps.create(name, base, spec)
    try:
        log(f"installing {spec} into a new VM disk for {name}")
        # A local project is shared into the VM read-only, copied to a scratch directory there, and built and
        # installed by pipx in the VM: its build code never runs on your machine.
        shares = [Share(source, "src", SOURCE_MOUNT, readonly=True)] if source else []
        with Machine(app.disk, app.logs, key=key, net=True, memory=args.memory, cpus=args.cpus,
                     shares=shares) as machine:
            machine.wait_ready()
            machine.mount_shares()
            command = (["sh", "-c", f"rm -rf /tmp/pipvm-src && cp -r {SOURCE_MOUNT} /tmp/pipvm-src "
                                    f"&& pipx install /tmp/pipvm-src"] if source else ["pipx", "install", args.package])
            status = machine.run(command)
            if status != 0:
                raise Error(f"pipx could not install {spec} (status {status})")
            # The programs pipx linked into the user's bin directory are the package's commands.
            exes = machine.output(["sh", "-c", "ls -1 ~/.local/bin"]).split()
        written, skipped = apps.write_shims(app, exes)
    except BaseException:
        app.remove()
        raise
    if args.host_emulator:
        app.set_setting("host-emulator", True)
        log(f"{name} will run in a window of a shared LXTerminal on your side "
            f"(change it with: pip-vm set {name} host-emulator off)")
    if args.vm_emulator:
        app.set_setting("vm-emulator", True)
        log(f"{name} will run in a terminal window (change it with: pip-vm set {name} vm-emulator off)")
    log(f"installed {name}: " + (", ".join(written) if written else "no commands"))
    for exe in skipped:
        log(f"not creating {apps.bin_dir() / exe}: something else is already there. "
            f"Run it with: pip-vm run --exe {exe} {name}")
    if written and str(apps.bin_dir()) not in os.environ.get("PATH", "").split(os.pathsep):
        log(f"{apps.bin_dir()} is not on your PATH")
    return 0


def supports(app, minimum):
    """Whether the image the app was made on is new enough (image.DISPLAY_REVISION, image.SHARE_REVISION)."""
    return image.base_revision(app.info().get("base", "")) >= minimum


def reinstall_hint(app):
    return f"pip-vm uninstall {app.name} && pip-vm install {app.spec()}"


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


def vm_emulator_setting(app):
    """The saved vm-emulator setting; an older app's `terminal-window` key means the same."""
    return app.setting("vm-emulator", app.setting("terminal-window", False))


def emulator_choice(args, app):
    """'host', 'vm' or None: where this run's terminal emulator runs. A flag of this run wins over the saved
    settings; two saved settings that disagree are an error."""
    if args.host_emulator and args.vm_emulator:
        raise Error("--host-emulator and --vm-emulator cannot be combined: the emulator runs in one place")
    if args.host_emulator:
        return "host"
    if args.vm_emulator:
        return "vm"
    host = args.host_emulator is None and app.setting("host-emulator", False)
    vm = args.vm_emulator is None and vm_emulator_setting(app)
    if host and vm:
        raise Error(f"{app.name} is set to use an emulator on the host (host-emulator) and also in the VM "
                    f"(vm-emulator): turn one off with `pip-vm set {app.name} vm-emulator off`")
    return "host" if host else ("vm" if vm else None)


def vm_emulator_wanted(app, flag=None):
    """Whether this app runs in an xterm in the VM: this run's flag, else the saved setting. Off by default. An
    app made on an image without xterm runs without one, unless one was asked for outright."""
    wanted = flag if flag is not None else vm_emulator_setting(app)
    if wanted and not supports(app, image.VM_EMULATOR_REVISION):
        if flag:
            raise Error(f"{app.name} was installed on an older image that has no terminal emulator; "
                        f"reinstall it: {reinstall_hint(app)}")
        log(f"{app.name} was installed on an older image that has no terminal emulator, so it runs without one. "
            f"To update it: {reinstall_hint(app)}")
        return False
    return bool(wanted)


def vm_emulator_command(argv, title):
    """The command that shows argv in an xterm in the VM; no argv: the xterm runs a login shell.
    The xterm gives the program a real terminal, and the display's mouse and keyboard reach it."""
    xterm = ["xterm", "-title", title, "-tn", "xterm-256color", "-fa", "DejaVu Sans Mono", "-fs", "11"]
    return xterm + (["-e", *argv] if argv else ["-ls"])


def config_share(flag, app):
    """Your ~/.config/<app> from the host, read-write, as ~/.config/<app> in the VM, so the program can keep
    its settings; None if the feature is off, the app's image is too old for it, or you have no such directory."""
    if not feature_wanted("config", flag, app, image.SHARE_REVISION, "be given your configuration"):
        return None
    host = apps.host_config_directory() / app.name
    if not host.is_dir():
        return None
    return Share(host, "config", f"{GUEST_HOME}/.config/{app.name}")


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
    choice = emulator_choice(args, app)
    if choice == "host" and not args.in_terminal:
        if not sys.stdin.isatty():
            log("this app runs in a terminal window, so standard input is not passed to it")
        hostterm.open_window(hostterm.inner_command(sys.argv[1:]), f"pip-vm: {app.name}")
        return 0
    key = image.ensure_key()
    if image.base_revision(app.info().get("base", "")) < image.REVISION:
        log(f"{app.name} was installed on an older image, so it may lack windows or your ~/.config/{app.name}. "
            f"To update it: {reinstall_hint(app)}")
    # Everything the program writes to its disk is thrown away when it stops, and unless --net is
    # given it has no network. Your ~/.config/<app> is shared with it, read-write. A new machine is
    # started for every run.
    config = config_share(args.config, app)
    vm_emulator = choice == "vm" and vm_emulator_wanted(app, True if args.vm_emulator else None)
    if vm_emulator and args.xephyr is False:
        raise Error(f"{app.name} runs in a terminal window, which is shown through Xephyr, "
                    f"so it cannot be run with --no-xephyr")
    with tempfile.TemporaryDirectory(prefix="pipvm-run-") as logs:
        with Machine(app.disk, logs, key=key, net=args.net, snapshot=True, memory=args.memory,
                     cpus=args.cpus, shares=[config] if config else []) as machine:
            machine.wait_ready()
            machine.mount_shares()
            if args.in_terminal:      # already in a window on the host: the program gets its terminal
                if xephyr_wanted(args.xephyr, app):
                    return run_with_display(machine, argv, app.name, tty=True)
                return machine.run(argv, user=GUEST_USER, tty=True)
            if vm_emulator:
                if not sys.stdin.isatty():
                    log("this app runs in a terminal window, so standard input is not passed to it")
                return run_with_display(machine, vm_emulator_command(argv, app.name), app.name)
            if xephyr_wanted(args.xephyr, app):
                return run_with_display(machine, argv, app.name, tty=args.shell)
            return machine.run(argv, user=GUEST_USER, tty=args.shell)


def parse_share(text):
    """'DIR', 'DIR:ro' or 'DIR:rw' -> (absolute path, read-only?). Read-only unless :rw is given."""
    head, sep, tail = text.rpartition(":")
    path_text, mode = (head, tail) if sep and tail in ("ro", "rw") else (text, "ro")
    path = Path(path_text).expanduser()
    if not path.is_dir():
        raise Error(f"--share {text}: {path_text} is not a directory")
    path = path.resolve()
    if path == Path("/"):
        raise Error("--share: refusing to share the whole filesystem")
    return path, mode == "ro"


def install_script(allow_build):
    """Run in the VM, with the network up and nothing of yours mounted: a venv, then the requirements into it.
    Only binary wheels, which are unpacked and not run, unless building from source was asked for."""
    only = "" if allow_build else "--only-binary :all: "
    return ('set -e\nV="$HOME/venv"\npython3 -m venv --without-pip "$V"\n'
            f'pip --python "$V/bin/python" install --no-input --disable-pip-version-check {only}"$@"\n')


def exec_script(python_path, cwd, venv):
    """Run in the VM for the command itself: the venv's programs first, the project's source importable."""
    lines = []
    if venv:
        lines.append('export PATH="$HOME/venv/bin:$PATH"')
    if python_path:
        lines.append(f"export PYTHONPATH={shlex.quote(':'.join(python_path))}")
    lines += [f"cd {shlex.quote(cwd)} 2>/dev/null || cd", 'exec "$@"']
    return "\n".join(lines)


def exec_run(args):
    """One-off, anonymous: nothing is installed as an app and the disk is thrown away afterwards.

    Phase 1 (only if there is something to install): a VM WITH network and NOTHING of yours mounted installs the
    requirements from the package index as wheels. Phase 2: a fresh VM from that disk, WITHOUT network (unless
    --net), with the project's source and any --share directories mounted, runs the command."""
    command = args.args[1:] if args.args[:1] == ["--"] else args.args
    if not command:
        raise Error("give a command to run, after --   (for example: pip-vm exec --with matplotlib -- python3 plot.py)")
    project = None
    if not args.no_project:
        directory = Path(args.project).expanduser() if args.project else Path.cwd()
        if (directory / "pyproject.toml").exists():
            project = projects.read(directory)
        elif args.project:
            raise Error(f"{directory} has no pyproject.toml")
    requirements = projects.check_requirements((project.requirements(args.extra) if project else []) + list(args.with_))
    shares = [Share(project.directory, "src", str(project.directory), True)] if project else []
    chosen = []
    for number, text in enumerate(args.share):
        path, readonly = parse_share(text)
        chosen.append(Share(path, f"share{number}", str(path), readonly))
    shares += chosen
    cwd = chosen[0].mountpoint if chosen else (str(project.directory) if project else GUEST_HOME)
    base, key = image.ensure_prepared(), image.ensure_key()
    with tempfile.TemporaryDirectory(prefix="pipvm-exec-") as work:
        work = Path(work)
        disk = work / "disk.qcow2"
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(disk)],
                       check=True, capture_output=True)
        if requirements:
            how = "from source or wheels" if args.allow_build else "as wheels only"
            log(f"installing {', '.join(requirements)} ({how}; this VM has the network and none of your files)")
            with Machine(disk, work / "install-logs", key=key, net=True, memory=args.memory, cpus=args.cpus) as machine:
                machine.wait_ready()
                status = machine.run(["sh", "-c", install_script(args.allow_build), "sh", *requirements])
            if status != 0:
                raise Error("could not install the requirements" + ("" if args.allow_build else
                            "; a package with no wheel for this VM cannot be installed this way. --allow-build lets "
                            "it build from source, which runs its build code with the network up"))
        with Machine(disk, work / "run-logs", key=key, net=args.net, snapshot=True, memory=args.memory,
                     cpus=args.cpus, shares=shares) as machine:
            machine.wait_ready()
            machine.mount_shares()
            script = exec_script(project.python_path() if project else [], cwd, bool(requirements))
            return machine.run(["sh", "-c", script, "sh", *command], tty=args.tty)


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
    installer.add_argument("package", help="A pip requirement, e.g. cowsay or 'httpie==3.2.2'; or the directory of "
                                           "a local project (./dir, ~/dir or /path/dir), which is built inside the VM")
    installer.add_argument("--name", help="Name for the app (default: the package name)")
    installer.add_argument("--host-emulator", action="store_true",
                           help="Run this app in a window of a shared LXTerminal on your side (the VM runs only the "
                                "program, over ssh). Remembered. Change it later with `pip-vm set NAME host-emulator on|off`")
    installer.add_argument("--vm-emulator", action="store_true",
                           help="Run this app in a terminal window: an xterm in the VM, shown in the Xephyr window, so a "
                                "text program gets a real mouse and keyboard. Remembered; standard input is not passed "
                                "on. Change it later with `pip-vm set NAME vm-emulator on|off`")
    sizes(installer)

    runner = sub.add_parser("run", help="Start a fresh VM and run the program in it; options go before NAME")
    runner.add_argument("--net", action="store_true", help="Give the program internet access (none by default)")
    runner.add_argument("--exe", help="The program to run (default: the app's name)")
    runner.add_argument("--shell", action="store_true", help="Open a shell in the VM instead")
    runner.add_argument("--host-emulator", action=argparse.BooleanOptionalAction, default=None,
                        help="Run the program in a window of a shared LXTerminal on your side, or not. Without this, "
                             "the app's saved setting is used (see `pip-vm set`), and then the default: off. Returns once "
                             "the window is open")
    runner.add_argument("--vm-emulator", action=argparse.BooleanOptionalAction, default=None,
                        help="Run the program in an xterm in the VM, shown in the Xephyr window, or not. Without this, "
                             "the app's saved setting is used (see `pip-vm set`), and then the default: off")
    runner.add_argument("--in-terminal", action="store_true", help=argparse.SUPPRESS)    # internal: already in the window
    runner.add_argument("--config", action=argparse.BooleanOptionalAction, default=None,
                        help="Let the program read and write your ~/.config/<app>, or not. Without this, the app's "
                             "saved setting is used (see `pip-vm set`), and then the default: on")
    runner.add_argument("--xephyr", action=argparse.BooleanOptionalAction, default=None,
                        help="Show the program's windows in a lazily started Xephyr window, or not. "
                             "Without this, the app's saved setting is used (see `pip-vm set`), and then the default: on")
    sizes(runner)
    runner.add_argument("name")
    runner.add_argument("args", nargs=argparse.REMAINDER, help="-- ARGS for the program")

    executor = sub.add_parser(
        "exec", help="One-off: install requirements as wheels, then run a command, keeping nothing",
        description="Anonymous mode: nothing is installed as an app and the disk is thrown away. Requirements come "
                    "from the pyproject.toml in the current directory (read as data: its build tool is never run) and "
                    "from --with. They are installed from the package index as wheels in a VM that has the network and "
                    "none of your files. The command then runs in a fresh VM with no network, with the project's source "
                    "and any --share directories mounted.",
        epilog="example: pip-vm exec --with matplotlib --share .:rw -- python3 plot.py")
    executor.add_argument("--with", dest="with_", action="append", default=[], metavar="REQ",
                          help="also install this requirement (repeatable)")
    executor.add_argument("--extra", action="append", default=[], help="also install this optional-dependencies group")
    executor.add_argument("--project", metavar="DIR", help="the project to read (default: the current directory, if it has a pyproject.toml)")
    executor.add_argument("--no-project", action="store_true", help="ignore any pyproject.toml; install only --with")
    executor.add_argument("--share", action="append", default=[], metavar="DIR[:rw]",
                          help="make a host directory visible to the command at the same path (read-only unless :rw); "
                               "the command starts in the first one. Repeatable")
    executor.add_argument("--net", action="store_true", help="give the command (not the install) internet access")
    executor.add_argument("--allow-build", action="store_true",
                          help="let packages without a wheel be built from source: runs their build code, with the network up")
    executor.add_argument("-t", "--tty", action="store_true", help="give the command a terminal")
    sizes(executor)
    executor.add_argument("args", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")

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
        return {"install": install, "run": run, "exec": exec_run, "list": list_apps, "set": set_setting,
                "uninstall": uninstall}[args.action](args)
    except Error as error:
        print(f"pip-vm: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        print(f"pip-vm: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
