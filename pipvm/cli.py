"""pipvm: install Python programs with pipx inside a virtual machine, and run them there."""
import argparse
import os
import subprocess
import sys
import tempfile

from . import apps, image
from .util import Error, log
from .vm import GUEST_USER, Machine


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


def run(args):
    app = apps.get(args.name)
    command = args.args[1:] if args.args[:1] == ["--"] else args.args
    argv = [args.exe or app.name, *command] if not args.shell else []
    key = image.ensure_key()
    # Everything the program writes to its disk is thrown away when it stops, and unless --net is
    # given it has no network. A new machine is started for every run.
    with tempfile.TemporaryDirectory(prefix="pipvm-run-") as logs:
        with Machine(app.disk, logs, key=key, net=args.net, snapshot=True, memory=args.memory,
                     cpus=args.cpus) as machine:
            machine.wait_ready()
            return machine.run(argv, user=GUEST_USER, tty=args.shell)


def list_apps(args):
    for app in apps.installed():
        print(f"{app.name}\t{app.spec()}\t{' '.join(app.exes())}")
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
    sizes(runner)
    runner.add_argument("name")
    runner.add_argument("args", nargs=argparse.REMAINDER, help="-- ARGS for the program")

    sub.add_parser("list", help="List installed apps")
    remover = sub.add_parser("uninstall", help="Delete an app and its disk")
    remover.add_argument("name")
    return p


def main():
    args = parser().parse_args()
    try:
        return {"install": install, "run": run, "list": list_apps, "uninstall": uninstall}[args.action](args)
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
