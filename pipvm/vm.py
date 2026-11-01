"""Starting a QEMU virtual machine, and running commands in it over ssh."""
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path

from .util import Error, log, tail

GUEST_USER = "pipvm"   # the unprivileged user the installed programs run as
ADMIN_USER = "root"    # only used to prepare the image and to power off cleanly; the guest is the jail


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

    def __init__(self, disk, logs, *, key, net=False, snapshot=False, seed=None, memory="1G", cpus=2):
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
        return cmd

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
            # stdin closed: ssh would otherwise read (and swallow) the input meant for the program
            probe = subprocess.run(self._ssh() + [f"{user}@127.0.0.1", "true"], capture_output=True,
                                   stdin=subprocess.DEVNULL)
            if probe.returncode == 0:
                return time.monotonic() - start
            time.sleep(0.2)
        raise Error(f"the VM did not accept ssh within {timeout}s.\n--- console:\n{tail(self.console_log)}")

    def run_command(self, argv, user=GUEST_USER, tty=False):
        """The ssh command line that runs argv in the guest. No argv: a login shell."""
        if argv:
            remote = 'PATH="$HOME/.local/bin:$PATH" exec ' + shlex.join(argv)
        else:
            remote = "exec sh -l"
        return self._ssh() + (["-tt"] if tty else ["-T"]) + [f"{user}@127.0.0.1", remote]

    def run(self, argv, user=GUEST_USER, tty=False):
        """Run a command in the guest and return its exit status. Standard input, output and error
        are the caller's own, so output arrives as the program writes it, stdout and stderr apart."""
        return subprocess.call(self.run_command(argv, user, tty))

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
