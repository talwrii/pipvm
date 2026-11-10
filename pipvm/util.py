"""Small helpers shared by the other modules."""
import os
import sys
from pathlib import Path


class Error(Exception):
    """A problem to report to the user without a traceback."""


def cache_dir():
    """The downloaded and prepared images and the ssh key: shared by every app."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "pipvm"


def data_dir():
    """The installed apps' disks."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "pipvm"


def log(message):
    print(f"pip-vm: {message}", file=sys.stderr, flush=True)


def tail(path, lines=25):
    """The last few lines of a log file, for error messages."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return "(no log)"
    return "\n".join(text.splitlines()[-lines:])
