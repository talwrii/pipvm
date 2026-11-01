# Developing pipvm

## Layout
```
pipvm/                  the pipvm package
pipvm/xephyr_run        symlink to ../xephyr-run/xephyr_run: the xephyr-run code, imported as pipvm.xephyr_run
xephyr-run/             git submodule: the xephyr-run repository (git@github.com:talwrii/xephyr-run.git)
tests/                  pipvm's tests; test_xephyr_run.py also runs xephyr-run's own tests
release-subrepo         records and publishes new changes from a subrepo
```
`xephyr-run` stays a separate project with its own history. pipvm ships its code, and its `xephyr-run`
command, so one `pipx install pipvm` gives you both. The code in `xephyr_run` must use relative imports
(`from . import registry`) so that it works under both names. If you also install `xephyr-run` on its own with
pipx, the two fight over the `xephyr-run` command; use one of them.

After cloning pipvm: `git submodule update --init`.

## Tests
```bash
python3 -m unittest discover -s tests
```
This runs pipvm's tests and xephyr-run's, so a change in the subrepo cannot quietly break pipvm. xephyr-run's
tests import the package as `xephyr_run` (the real directory); `test_xephyr_run.py` also checks the
`pipvm.xephyr_run` path. Some of xephyr-run's tests need `Xephyr` and an X display (`DISPLAY`) and skip
otherwise. Use the pipx environment's Python (the one `head -1 $(which pipvm)` names) so `ziglang` is there to
compile the connect filter.

## Changing xephyr-run
Work in `xephyr-run/` as in any git repository: edit, test, **commit inside it**. Then, from the pipvm root:
```bash
./release-subrepo           # check, test, and record the new xephyr-run commit in pipvm (nothing leaves your machine)
./release-subrepo --push    # the same, then push xephyr-run, and only then pipvm
```
The script:
1. refuses if `xephyr-run/` has uncommitted changes, or if anything else is already staged in pipvm;
2. lists the xephyr-run commits that are new since pipvm last recorded it;
3. runs the tests (`python3 -m unittest discover -s tests`, or the command in `$RELEASE_TEST`), and stops if they fail;
4. commits the new submodule pointer in pipvm, with the list of changes as the message;
5. with `--push`, pushes xephyr-run first, then pipvm with `--recurse-submodules=check`, so pipvm can never
   point at a commit that exists only on your machine. For `git@github.com:` remotes it shares one ssh
   connection between the two pushes, so a key passphrase is asked for once.

It does not tag or upload to PyPI. Releasing pipvm itself, or xephyr-run on its own, is separate.
`tests/test_release_subrepo.py` tries the script on throwaway repositories with local remotes.
