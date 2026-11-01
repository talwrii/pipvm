# Developing pipvm

## Layout
```
pipvm/                  the pipvm package
pipvm/xephyr_run        symlink to ../xephyr-run/xephyr_run: the xephyr-run code, imported as pipvm.xephyr_run
xephyr-run/             a git subrepo: the xephyr-run repository (git@github.com:talwrii/xephyr-run.git), as ordinary files
tests/                  pipvm's tests; test_xephyr_run.py also runs xephyr-run's own tests
```
`xephyr-run` stays a separate project with its own repository. Its files live in this repository as ordinary files
(a [git subrepo](https://github.com/ingydotnet/git-subrepo)), so cloning pipvm needs no extra step. pipvm ships
its code, and its `xephyr-run` command, so one `pipx install pipvm` gives you both. The code in `xephyr_run` must use
relative imports (`from . import registry`) so that it works under both names. If you also install `xephyr-run` on
its own with pipx, the two fight over the `xephyr-run` command; use one of them.

## Tests
```bash
python3 -m unittest discover -s tests
```
This runs pipvm's tests and xephyr-run's, so a change in the subrepo cannot quietly break pipvm. Some of xephyr-run's
tests need `Xephyr` and an X display (`DISPLAY`) and skip otherwise. Use the pipx environment's Python (the one
`head -1 $(which pipvm)` names) so `ziglang` is there to compile the connect filter.

## Changing xephyr-run
Edit `xephyr-run/` here like any directory and commit in pipvm as usual. Publishing needs `git subrepo`, which is
only for whoever publishes (`git clone https://github.com/ingydotnet/git-subrepo ~/.local/share/git-subrepo`, then link
`lib/git-subrepo` into your PATH as `git-subrepo`). From the pipvm root, with a clean working tree:
```bash
git subrepo push xephyr-run     # publish the changes to xephyr-run's own repository
git subrepo pull xephyr-run     # bring in changes made there directly; do this first if push says upstream has moved
git subrepo status xephyr-run
```
Notes:
* The commits that `push` creates are rebuilt for the xephyr-run repository, so their hashes differ from the ones
  in pipvm. `push` also adds a commit here that updates `xephyr-run/.gitrepo`, which records what is in sync. Don't edit it by hand.
* If xephyr-run changed upstream since the last pull or push, pull before pushing.
* Run the tests before `push`; it does not.
* The standalone clone at `~/mine/xephyr-run` is optional. If you also work there, push from one place and `pull`
  into the other, so the two do not diverge.
