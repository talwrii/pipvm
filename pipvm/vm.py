"""Starting a QEMU virtual machine, and running commands in it over ssh."""
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from .util import Error, log, tail

GUEST_USER = "pipvm"   # the unprivileged user the installed programs run as
ADMIN_USER = "root"    # only used to prepare the image and to power off cleanly; the guest is the jail
GUEST_X_DIRECTORY = "/tmp/.X11-unix"
GUEST_X_SOCKET = f"{GUEST_X_DIRECTORY}/X0"   # display :0 in the guest
GUEST_HOME = f"/home/{GUEST_USER}"


class Share(NamedTuple):
    """A directory on the host, mounted in the guest over 9p. Files the guest makes belong to you."""
    host: Path
    tag: str
    mountpoint: str
    readonly: bool = False


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Machine:
    """One QEMU virtual machine, started without root privileges on the host.

    `net` says whether the guest may reach the internet. With it off the guest can reach
    nothing at all: not the internet, your LAN, or your host.
    `snapshot` throws away everything the guest writes to its disk when it stops.
    """

    def __init__(self, disk, logs, *, key, net=False, snapshot=False, seed=None, memory="1G", cpus=2, shares=()):
        self.shares = list(shares)
        self.disk = Path(disk)
        self.logs = Path(logs)  # a directory for the console and qemu logs
        self.key = Path(key)
        self.net = net
        self.snapshot = snapshot
        self.seed = seed
        self.memory = memory
        self.cpus = cpus
        self.port = _free_port()
        self.proc = None

    @property
    def console_log(self):
        return self.logs / "console.log"

    @property
    def qemu_log(self):
        return self.logs / "qemu.log"

    def command(self):
        # The guest's network goes to QEMU's own user-mode networking, which needs no root.
        # restrict=on cuts it off from everything except the ssh forward below.
        netdev = f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.port}-:22"
        if not self.net:
            netdev += ",restrict=on"
        cmd = [
            "qemu-system-x86_64",
            "-machine", "q35,accel=kvm", "-cpu", "host",
            "-m", self.memory, "-smp", str(self.cpus),
            "-nodefaults", "-no-user-config", "-display", "none", "-monitor", "none",
            "-serial", f"file:{self.console_log}",
            "-device", "virtio-rng-pci",
            "-drive", f"file={self.disk},if=none,id=hd0,format=qcow2",
            "-device", "virtio-blk-pci,drive=hd0,bootindex=0",
            "-netdev", netdev, "-device", "virtio-net-pci,netdev=n0",
        ]
        if self.snapshot:
            cmd.append("-snapshot")
        if self.seed:
            cmd += ["-drive", f"file={self.seed},if=none,id=seed,format=raw,readonly=on",
                    "-device", "virtio-blk-pci,drive=seed"]
        for share in self.shares:
            # security_model=none: files the guest makes belong to you, and the guest user has your uid.
            cmd += ["-virtfs", f"local,path={share.host},mount_tag={share.tag},security_model=none,id={share.tag}"
                               + (",readonly=on" if share.readonly else "")]
        return cmd

    def mount_shares(self):
        """Mount the shared host directories in the guest. Done as root, once ssh is up."""
        if not self.shares:
            return
        steps = ["modprobe 9p 9pnet_virtio 2>/dev/null || true"]
        for share in self.shares:
            options = "trans=virtio,version=9p2000.L,msize=262144" + (",ro" if share.readonly else "")
            where = shlex.quote(share.mountpoint)
            # Made as root, so the user's own directory above the mount point is handed back to the user.
            parent = shlex.quote(os.path.dirname(share.mountpoint))
            steps.append(f"mkdir -p {where} && chown {GUEST_USER}:{GUEST_USER} {parent} "
                         f"&& mount -t 9p -o {options} {share.tag} {where}")
        result = subprocess.run(self._ssh() + [f"{ADMIN_USER}@127.0.0.1", "; ".join(steps)],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise Error("could not mount the shared directories in the VM: " + result.stderr.strip())

    def start(self):
        if not os.access("/dev/kvm", os.W_OK):
            raise Error("cannot open /dev/kvm: without it the VM would be very slow. "
                        "Check that your user may use KVM (the kvm group, or an ACL).")
        self.logs.mkdir(parents=True, exist_ok=True)
        self.console_log.write_text("")
        with open(self.qemu_log, "wb") as err:
            self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=err)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _ssh(self):
        return ["ssh", "-i", str(self.key), "-p", str(self.port),
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=3"]

    def wait_ready(self, user=GUEST_USER, timeout=180):
        """Block until ssh answers. Raises, with the guest's console, if it never does."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if not self.alive():
                raise Error(f"the VM exited during boot (status {self.proc.returncode}).\n"
                            f"--- qemu:\n{tail(self.qemu_log)}\n--- console:\n{tail(self.console_log)}")
            # stdin closed: ssh would otherwise read (and swallow) the input meant for the program.
            # The probe also makes the directory the forwarded X display socket will be put in.
            probe = subprocess.run(self._ssh() + [f"{user}@127.0.0.1", f"mkdir -p {GUEST_X_DIRECTORY}"],
                                   capture_output=True, stdin=subprocess.DEVNULL)
            if probe.returncode == 0:
                return time.monotonic() - start
            time.sleep(0.2)
        raise Error(f"the VM did not accept ssh within {timeout}s.\n--- console:\n{tail(self.console_log)}")

    def run_command(self, argv, user=GUEST_USER, tty=False, x_socket=None, env=None):
        """The ssh command line that runs argv in the guest. No argv: a login shell.

        With x_socket, the host's unix socket of that path is forwarded into the guest as display :0,
        so an X program in the guest reaches the X server on the other end of it. env adds
        environment variables for the program."""
        variables = {}
        forward = []
        if x_socket:
            variables["DISPLAY"] = ":0"
            forward = ["-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
                       "-R", f"{GUEST_X_SOCKET}:{x_socket}"]
        variables.update(env or {})
        assignments = "".join(f"{name}={shlex.quote(value)} " for name, value in variables.items())
        if argv:
            remote = assignments + 'PATH="$HOME/.local/bin:$PATH" exec ' + shlex.join(argv)
        else:
            remote = assignments + "exec sh -l"
        return self._ssh() + forward + (["-tt"] if tty else ["-T"]) + [f"{user}@127.0.0.1", remote]

    def popen(self, argv, user=GUEST_USER, tty=False, x_socket=None, env=None):
        """Start the command in the guest without waiting for it."""
        return subprocess.Popen(self.run_command(argv, user, tty, x_socket, env))

    def run(self, argv, user=GUEST_USER, tty=False, env=None):
        """Run a command in the guest and return its exit status. Standard input, output and error
        are the caller's own, so output arrives as the program writes it, stdout and stderr apart."""
        return subprocess.call(self.run_command(argv, user, tty, env=env))

    def output(self, argv, user=GUEST_USER):
        """Run a command in the guest and return what it printed."""
        result = subprocess.run(self.run_command(argv, user), capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise Error(f"{argv[0]} failed in the VM: {result.stderr.strip()}")
        return result.stdout

    def wait_exit(self, timeout):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def stop(self, clean=True):
        """Power the guest off cleanly (so its disk is consistent), or just kill it when its
        disk is going to be thrown away anyway. Kills it if a clean power off stalls."""
        if not self.alive():
            return
        if clean:
            subprocess.run(self._ssh() + [f"{ADMIN_USER}@127.0.0.1", "sync; poweroff"], capture_output=True,
                           stdin=subprocess.DEVNULL)
            if self.wait_exit(30) is None:
                log("the VM did not power off, stopping it")
        if self.alive():
            self.proc.terminate()
            if self.wait_exit(10) is None:
                self.proc.kill()
                self.proc.wait()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop(clean=not self.snapshot)
