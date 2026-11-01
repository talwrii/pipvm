"""The VM image: download Alpine's cloud image once, verify it, and add Python, pipx and ssh to it."""
import hashlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from .util import Error, cache_dir, log
from .vm import Machine

ALPINE = "3.24.1"
IMAGE = f"generic_alpine-{ALPINE}-x86_64-bios-tiny-r0.qcow2"
BASE_URL = f"https://dl-cdn.alpinelinux.org/alpine/v{ALPINE.rpartition('.')[0]}/releases/cloud"
DISK_SIZE = "8G"  # the cloud image is only 128 MiB: far too small to install programs in. Its
                  # first-boot agent (tiny-cloud) grows the filesystem to fill the disk.
# Raise this whenever PROVISION changes, so images prepared the old way are rebuilt.
REVISION = 1

# Run once, as root, inside the VM by tiny-cloud, from the config disk. tiny-cloud keeps
# its output in /var/log/user-data.log, which pipvm reads back if this fails.
PROVISION = r"""#!/bin/sh
set -eu
say() { echo "pipvm: $*"; echo "pipvm: $*" > /dev/ttyS0 2>/dev/null || true; }

ok=0
finish() {
	if [ "$ok" = 1 ]; then say PIPVM-PROVISIONED; else say PIPVM-FAILED; fi
	sync
	poweroff
}
trap finish EXIT

say "provisioning started"

apk add --no-cache python3 py3-pip pipx openssh ca-certificates

# Installed programs run as an ordinary user. Only the key pipvm made may log in, as that user
# (to run programs) or as root (to power off cleanly): nothing else has a way in.
adduser -D -s /bin/sh pipvm
echo "pipvm:$(head -c 24 /dev/urandom | base64)" | chpasswd
echo "root:$(head -c 24 /dev/urandom | base64)" | chpasswd
for home in /root /home/pipvm; do
	mkdir -p "$home/.ssh"
	printf '%s\n' '@PUBKEY@' > "$home/.ssh/authorized_keys"
	chmod 700 "$home/.ssh"
	chmod 600 "$home/.ssh/authorized_keys"
done
chown -R pipvm:pipvm /home/pipvm/.ssh
sed -i -e '/^#\?PermitRootLogin/d' -e '/^#\?PasswordAuthentication/d' /etc/ssh/sshd_config
printf 'PermitRootLogin prohibit-password\nPasswordAuthentication no\n' >> /etc/ssh/sshd_config
rc-update add sshd default || true

# The first-boot agent has done its job: later boots need not wait for it to look for a config disk.
for service in tiny-cloud-early tiny-cloud-main tiny-cloud-final; do
	rc-update del "$service" default || true
done
rc-update del tiny-cloud-boot boot || true

# Boot quickly. The bootloader waits 10 seconds for a menu nobody sees, and DHCP waits another
# five. QEMU's user-mode network always gives the guest the same address, so set it directly.
sed -i 's/^TIMEOUT .*/TIMEOUT 1/' /boot/extlinux.conf
sed -i 's/^timeout=.*/timeout=1/' /etc/update-extlinux.conf || true
cat > /etc/network/interfaces <<'EOF'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
	address 10.0.2.15/24
	gateway 10.0.2.2
EOF
echo "nameserver 10.0.2.3" > /etc/resolv.conf

ok=1
"""


def _fetch(url):
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def ensure_base():
    """The Alpine cloud image, downloaded and checked against the sha512 Alpine publishes."""
    path = cache_dir() / IMAGE
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading Alpine {ALPINE} cloud image (about 118 MB). This happens once.")
    listed = _fetch(f"{BASE_URL}/{IMAGE}.sha512").decode()
    match = re.search(r"\b[0-9a-f]{128}\b", listed)
    if not match:
        raise Error(f"could not read the published checksum for {IMAGE}")
    part = path.with_name(path.name + ".part")
    digest = hashlib.sha512()
    with urllib.request.urlopen(f"{BASE_URL}/{IMAGE}", timeout=60) as response, open(part, "wb") as out:
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != match.group(0):
        part.unlink()
        raise Error(f"the downloaded {IMAGE} does not match its published sha512: not using it")
    part.replace(path)
    return path


def ensure_key():
    """The ssh key pipvm logs in with. Only this one is allowed into the VMs."""
    key = cache_dir() / "id_ed25519"
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pipvm", "-f", str(key)],
                       check=True)
    return key


def _make_seed(directory, pubkey):
    """A small disk labelled `cidata`, from which the image's first-boot agent reads PROVISION."""
    directory = Path(directory)
    (directory / "meta-data").write_text("instance-id: pipvm-prepare\nlocal-hostname: pipvm\n")
    (directory / "user-data").write_text(PROVISION.replace("@PUBKEY@", pubkey))
    seed = directory / "seed.iso"
    result = subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-o", str(seed), "-V", "cidata",
                             "-J", "-r", str(directory / "meta-data"), str(directory / "user-data")],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise Error(f"could not make the first-boot disk: {result.stderr.strip()}")
    return seed


def _guest_log(image, scratch):
    """The provisioning script's own output, read out of a (powered off) image without booting it.

    The image is one ext4 filesystem with no partition table, so debugfs can read it as a file.
    Needs debugfs (from e2fsprogs); returns "" if it is not installed or the log cannot be read.
    """
    if not shutil.which("debugfs"):
        return ""
    raw = Path(scratch) / "failed.raw"
    try:
        subprocess.run(["qemu-img", "convert", "-O", "raw", str(image), str(raw)],
                       check=True, capture_output=True)
        read = subprocess.run(["debugfs", "-R", "cat /var/log/user-data.log", str(raw)],
                              capture_output=True, text=True)
        return read.stdout
    except (OSError, subprocess.CalledProcessError):
        return ""
    finally:
        raw.unlink(missing_ok=True)


def prepared_path():
    return cache_dir() / f"alpine-{ALPINE}-pipvm-r{REVISION}.qcow2"


def ensure_prepared():
    """The Alpine image with Python, pipx and ssh installed. Built on first use, and then cached."""
    prepared = prepared_path()
    if prepared.exists():
        return prepared

    base = ensure_base()
    key = ensure_key()
    log("preparing the VM image: installing Python and pipx. This needs the internet and happens once.")
    work = prepared.with_name(prepared.name + ".part")
    work.unlink(missing_ok=True)
    subprocess.run(["qemu-img", "convert", "-O", "qcow2", str(base), str(work)], check=True)
    subprocess.run(["qemu-img", "resize", "-q", str(work), DISK_SIZE], check=True)

    logs = cache_dir() / "prepare"
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pipvm-") as tmp:
        seed = _make_seed(tmp, key.with_suffix(".pub").read_text().strip())
        machine = Machine(work, logs, key=key, net=True, seed=seed)
        machine.start()
        try:
            status = machine.wait_exit(1200)
        finally:
            if machine.alive():
                machine.proc.kill()
                machine.proc.wait()
        console = machine.console_log.read_text(errors="replace")
        if status is None or "PIPVM-PROVISIONED" not in console:
            reason = "took too long" if status is None else f"failed (the VM exited with status {status})"
            guest = _guest_log(work, tmp)
            detail = guest.strip() or console.strip() or "(nothing was logged)"
            raise Error(f"preparing the image {reason}.\n--- what the VM logged:\n"
                        + "\n".join(detail.splitlines()[-40:]) + f"\n(console: {machine.console_log})")
    work.replace(prepared)
    log(f"image ready ({time.monotonic() - started:.0f}s): {prepared}")
    return prepared
