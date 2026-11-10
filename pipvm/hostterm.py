"""A terminal window on the host for a program that runs in a VM.

The terminal is LXTerminal, run as its own dedicated process that every pipvm window shares (so a window costs
a fraction of a megabyte, not a whole terminal). The program itself runs in the VM, over ssh, in that window.

LXTerminal shares one process between all windows that ask for the same display, and the first config wins.
To keep pipvm's windows (with their own config, menus hidden) apart from any LXTerminal you use yourself, they
use a different spelling of your display (:0 becomes :0.0). That is the same X server, but LXTerminal treats it
as another display and starts a separate process.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .util import Error

CONFIG = """[general]
hidemenubar=true
hidescrollbar=true
hideclosebutton=true
disablef10=true
disablealt=true
"""

# What runs in the window: the command, then, if it failed, wait so that the message can be read.
HOLD = ('"$@"; status=$?\n'
        'if [ "$status" -ne 0 ]; then echo; echo "pip-vm: exited with status $status. Press Enter to close."; read _x; fi\n'
        'exit $status')


def config_home(environ=None):
    """The XDG_CONFIG_HOME of pipvm's LXTerminal: its settings live in ~/.config/pipvm/terminal/lxterminal/."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "pipvm" / "terminal"


def ensure_config():
    """Write the default settings (menus hidden) if there are none. Anything you change there is kept."""
    path = config_home() / "lxterminal" / "lxterminal.conf"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG)
    return config_home()


def dedicated_display(display):
    """The same X display, spelled so that LXTerminal starts a separate process for it. ':0' -> ':0.0',
    ':0.0' -> 'unix:0.0'. Any other spelling is returned as it is, and its windows may then join an LXTerminal
    that is already running on it."""
    plain = re.fullmatch(r":(\d+)", display)
    if plain:
        return f":{plain[1]}.0"
    with_screen = re.fullmatch(r":(\d+)\.0", display)
    if with_screen:
        return f"unix:{with_screen[1]}.0"
    return display


def inner_command(argv):
    """The command to run in the window: this same `pip-vm run ...`, told that it is already in a window."""
    if not argv or argv[0] != "run":
        raise Error("the host terminal is only for `pip-vm run`")
    return [sys.executable, "-m", "pipvm", "run", "--in-terminal", *argv[1:]]


def window_command(inner, title, real_config_home=None):
    """The lxterminal command line for one window. The shared server has pipvm's own XDG_CONFIG_HOME, so the
    program in the window is given back yours (or has it unset): it must still find ~/.config/<app>."""
    restore = ["env", f"XDG_CONFIG_HOME={real_config_home}"] if real_config_home else ["env", "-u", "XDG_CONFIG_HOME"]
    return ["lxterminal", "--title", title, "-e", "sh", "-c", HOLD, "sh", *restore, *inner]


def open_window(inner, title):
    """Open a window running `inner`, and return at once: the program's exit status is not available here."""
    if not shutil.which("lxterminal"):
        raise Error("lxterminal is not installed (on Debian or Ubuntu: sudo apt install lxterminal)")
    display = os.environ.get("DISPLAY")
    if not display:
        raise Error("there is no DISPLAY, so a terminal window cannot be opened")
    environment = dict(os.environ, DISPLAY=dedicated_display(display), XDG_CONFIG_HOME=str(ensure_config()),
                       NO_AT_BRIDGE="1")
    command = window_command(inner, title, os.environ.get("XDG_CONFIG_HOME"))
    subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
