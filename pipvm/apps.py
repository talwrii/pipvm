"""Installed apps: one small copy-on-write disk each, on top of the shared prepared image."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .util import Error, data_dir

NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
EXE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
SHIM_MARK = "# pipvm shim"


def apps_dir():
    return data_dir() / "apps"


def default_name(spec):
    """The package name inside a pip requirement such as 'Foo_Bar[extra]==1.2', normalised as pip does."""
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec.strip())
    if not match:
        raise Error(f"cannot find a package name in {spec!r}; give one with --name")
    return re.sub(r"[-_.]+", "-", match.group(0)).lower()


def check_name(name):
    if not NAME.fullmatch(name):
        raise Error(f"{name!r} is not a valid app name (lowercase letters, digits, . _ -)")
    return name


class App:
    def __init__(self, name, root=None):
        self.name = check_name(name)
        self.directory = (root or apps_dir()) / name

    @property
    def disk(self):
        return self.directory / "disk.qcow2"

    @property
    def meta(self):
        return self.directory / "app.json"

    @property
    def logs(self):
        return self.directory / "logs"

    def exists(self):
        return self.disk.exists()

    def info(self):
        try:
            return json.loads(self.meta.read_text())
        except (OSError, ValueError):
            return {}

    def update(self, **changes):
        self.meta.write_text(json.dumps({**self.info(), **changes}))

    def setting(self, key, default=None):
        return self.info().get("settings", {}).get(key, default)

    def set_setting(self, key, value):
        """Save a setting for this app; None removes it, so the default applies again."""
        settings = dict(self.info().get("settings", {}))
        if value is None:
            settings.pop(key, None)
        else:
            settings[key] = value
        self.update(settings=settings)

    def spec(self):
        return self.info().get("spec", self.name)

    def exes(self):
        return self.info().get("exes", [])

    def remove(self):
        remove_shims(self)
        shutil.rmtree(self.directory, ignore_errors=True)


def bin_dir():
    """Where the commands go: pipx's default, so they are normally already on your PATH."""
    return Path(os.environ.get("PIPVM_BIN_DIR") or Path.home() / ".local" / "bin")


def shim_text(app_name, exe, pipvm):
    return (f"#!/bin/sh\n{SHIM_MARK} for {app_name}: runs {exe} in a virtual machine\n"
            f'exec {shlex.quote(pipvm)} run --exe {shlex.quote(exe)} {shlex.quote(app_name)} -- "$@"\n')


def _ours(path, app_name):
    """Whether path is a shim this app wrote earlier, so it is safe to replace or delete."""
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return f"{SHIM_MARK} for {app_name}:" in path.read_text()[:300]
    except (OSError, UnicodeDecodeError):
        return False


def write_shims(app, exes, directory=None, pipvm=None):
    """A command for each program the package installed. Returns (written, skipped); a file that is
    already there and is not this app's shim is never overwritten."""
    directory = directory or bin_dir()
    pipvm = pipvm or os.path.abspath(shutil.which("pipvm") or sys.argv[0])
    directory.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for exe in exes:
        path = directory / exe
        if not EXE.fullmatch(exe) or ((path.exists() or path.is_symlink()) and not _ours(path, app.name)):
            skipped.append(exe)
            continue
        path.write_text(shim_text(app.name, exe, pipvm))
        path.chmod(0o755)
        written.append(exe)
    app.update(exes=written, bin_dir=str(directory))
    return written, skipped


def remove_shims(app):
    directory = Path(app.info().get("bin_dir") or bin_dir())
    for exe in app.exes():
        path = directory / exe
        if EXE.fullmatch(exe) and _ours(path, app.name):
            path.unlink()


def create(name, base, spec, root=None):
    """A new app whose disk starts as an empty overlay on the prepared image."""
    app = App(name, root)
    if app.directory.exists():
        raise Error(f"{name} is already installed; uninstall it first or pick another --name")
    app.directory.mkdir(parents=True)
    try:
        subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(app.disk)],
                       check=True, capture_output=True)
        app.meta.write_text(json.dumps({"spec": spec, "base": str(base), "installed": time.time()}))
    except BaseException:
        app.remove()
        raise
    return app


def get(name, root=None):
    app = App(name, root)
    if not app.exists():
        raise Error(f"{name} is not installed. Install it with: pipvm install {name}")
    return app


def installed(root=None):
    root = root or apps_dir()
    if not root.is_dir():
        return []
    return [App(path.name, root) for path in sorted(root.iterdir())
            if NAME.fullmatch(path.name) and (path / "disk.qcow2").exists()]
