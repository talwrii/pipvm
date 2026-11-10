# pipvm
Like [pipx](https://pipx.pypa.io/), but each installed program runs in its own virtual machine (or other type of jail).

AI-generated and unreviewed.

## Motivation
`pip install` runs arbitrary code, and the programs it installs run with everything your user can
touch. pipx keeps their *dependencies* apart, but not the programs themselves. pipvm installs a
package with pipx **inside a small QEMU virtual machine**, then starts a fresh machine every time you
run it. The program sees a minimal Linux with no access to your files, your X server, your ssh agent
or your network (unless you ask for the network).

Only the virtual machine "backend" exists so far; the code is arranged so that others (podman, for
example) can be added later.


## Alternatives and prior work
This is mostly a convenience wrapper around various tools (qemu, pipx, Xephyr) you could use these directly.

Sharing data can be a pain. For tigher interaction I made a tool called [ptrace-approve] which allows you to 
approve individual system calls - this might be more convenient in these use cases with the price of more
configuration. If your use cases are simplare you can use this.

## Caveats
The most annoying thing about these tools is sharing data and resources which often make VMs unusable.
I am partly making this tool as a game to practice using VMs where I can "secure" things which have easy data requirements.

There is quiter a lot of overhead of running VMs.

## Usage
```bash
pip-vm install cowsay                    # builds a base image the first time, then installs with pipx in a VM
cowsay -t "hello"                       # a command like pipx's: each run starts a fresh VM; stdin, stdout, stderr and exit status are forwarded
pip-vm run cowsay -- -t "hello"          # the same without the command on your PATH
echo hi | pip-vm run --exe cat cowsay    # --exe: the program to run, when it is not named like the package
pip-vm run --net httpie -- example.com   # let this run use the internet (it has none by default)
pip-vm run --shell cowsay                # a shell in the VM, to look around
pip-vm list
pip-vm uninstall cowsay
```
Options for `run` go before the app's name; everything after `--` is passed to the program.

`install` writes a small script for each command the package provides into `~/.local/bin` (or
`$PIPVM_BIN_DIR`), like pipx does. The script just runs `pip-vm run --exe COMMAND NAME -- "$@"`, so the
command always runs in a VM. A file that is already there and is not pipvm's own script is never overwritten
(pipvm says so, and you can still use `pip-vm run --exe`). `uninstall` removes only the scripts pipvm wrote.
Commands started this way have no network; to give one network access run it with `pip-vm run --net` instead.

* The program runs in the guest as an unprivileged user. QEMU itself runs as you, not root.
* A **new machine is started for every run**, and everything the program writes to its disk is thrown
  away when it stops. Programs that need to keep state between runs cannot yet.
* The guest has **no network** unless you pass `--net`: it cannot reach the internet, your LAN or your host.
* Your files are not visible to it. Pass data through standard input and output.
* `install` does have network access, to fetch the package.

## How it works
* The first install downloads Alpine's cloud image (checked against Alpine's published sha512) and runs a
  one-off VM that installs Python, pipx and ssh into it. The result is cached in `~/.cache/pipvm`, and
  every app is a small copy-on-write overlay on top of it, so adding an app does not copy the base.
* `install` boots the overlay with network access, runs `pipx install` as the unprivileged user, then
  powers the VM off cleanly so the package is on the disk.
* `run` boots the overlay with QEMU's `-snapshot` (writes are discarded), waits for ssh on a port
  forwarded on localhost, and runs the program over ssh. The ssh streams are yours, so output arrives as
  the program writes it, with standard output and error kept apart.
* Only a key pipvm generates (in `~/.cache/pipvm`) can log into the guest.

In one test run on this machine, preparing the base image took 26 seconds (plus the download), and
starting a VM and running `cowsay` took about 6 seconds.

## Requirements
Linux with KVM available to your user, `qemu-system-x86_64`, `qemu-img`, `xorriso`, and `ssh`. x86_64 only.
The first install needs the internet. `debugfs` (e2fsprogs), if present, is used to show the provisioning
log if preparing the image fails.

## Limits
* A VM protects your machine from the program. It does not protect the internet from the program when
  `--net` is on.
* Programs cannot yet read or write your files, or open windows: file sharing and display are not implemented.
* QEMU and the Linux guest are themselves code that could have bugs; a VM is a stronger boundary than a
  container, not an absolute one.

## Development
```bash
pipx install --editable .
python3 -m unittest discover -s tests
```
