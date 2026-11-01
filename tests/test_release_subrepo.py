"""release-subrepo, tried on throwaway repositories with local remotes (no network, nothing of yours)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "release-subrepo"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
           GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="protocol.file.allow", GIT_CONFIG_VALUE_0="always")


def git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=check).stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ReleaseSubrepo(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.sub_remote = self.root / "sub.git"
        self.parent_remote = self.root / "parent.git"
        for remote in (self.sub_remote, self.parent_remote):
            git("init", "-q", "--bare", "-b", "main", str(remote), cwd=self.root)
        seed = self.root / "seed"
        git("clone", "-q", str(self.sub_remote), str(seed), cwd=self.root)
        (seed / "lib.py").write_text("one\n")
        git("add", ".", cwd=seed)
        git("commit", "-q", "-m", "first", cwd=seed)
        git("push", "-q", "origin", "HEAD:main", cwd=seed)

        self.parent = self.root / "parent"
        git("clone", "-q", str(self.parent_remote), str(self.parent), cwd=self.root)
        git("submodule", "add", "-q", str(self.sub_remote), "lib", cwd=self.parent)
        shutil.copy(SCRIPT, self.parent / "release-subrepo")
        git("add", ".", cwd=self.parent)
        git("commit", "-q", "-m", "start", cwd=self.parent)
        git("push", "-q", "origin", "HEAD:main", cwd=self.parent)
        git("-C", "lib", "checkout", "-q", "-B", "main", "origin/main", cwd=self.parent)

    def commit_in_sub(self, text="two\n", message="add two"):
        (self.parent / "lib" / "lib.py").write_text(text)
        git("commit", "-q", "-am", message, cwd=self.parent / "lib")
        return git("rev-parse", "HEAD", cwd=self.parent / "lib")

    def release(self, *args, tests="true"):
        env = dict(ENV, RELEASE_TEST=tests)
        return subprocess.run(["bash", "./release-subrepo", *args], cwd=self.parent, env=env,
                              capture_output=True, text=True)

    def commits(self):
        return int(git("rev-list", "--count", "HEAD", cwd=self.parent))

    def has(self, remote, sha):
        return subprocess.run(["git", "--git-dir", str(remote), "cat-file", "-e", sha],
                              env=ENV, capture_output=True).returncode == 0

    def test_nothing_new(self):
        before = self.commits()
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing new", result.stdout)
        self.assertEqual(self.commits(), before)

    def test_records_the_new_commit_but_pushes_nothing(self):
        before = self.commits()
        sha = self.commit_in_sub()
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.commits(), before + 1)
        self.assertEqual(git("ls-tree", "HEAD", "lib", cwd=self.parent).split()[2], sha)
        message = git("log", "-1", "--format=%B", cwd=self.parent)
        self.assertIn("add two", message)
        self.assertFalse(self.has(self.sub_remote, sha))
        self.assertIn("Nothing was pushed", result.stdout)

    def test_push_publishes_the_subrepo_and_then_this_repository(self):
        sha = self.commit_in_sub()
        result = self.release("--push")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.has(self.sub_remote, sha))
        pointer = git("rev-parse", "HEAD", cwd=self.parent)
        self.assertTrue(self.has(self.parent_remote, pointer))

    def test_uncommitted_changes_in_the_subrepo_are_refused(self):
        before = self.commits()
        (self.parent / "lib" / "lib.py").write_text("unsaved work\n")
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted changes in lib", result.stderr)
        self.assertEqual(self.commits(), before)

    def test_failing_tests_stop_the_release_before_anything_is_recorded(self):
        before = self.commits()
        sha = self.commit_in_sub()
        result = self.release("--push", tests="exit 1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commits(), before)
        self.assertFalse(self.has(self.sub_remote, sha))   # even with --push, nothing was published

    def test_other_staged_changes_are_refused(self):
        self.commit_in_sub()
        (self.parent / "stray.txt").write_text("x\n")
        git("add", "stray.txt", cwd=self.parent)
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stray.txt", result.stderr)

    def test_unknown_subrepo(self):
        result = self.release("nonsense")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a submodule", result.stderr)


if __name__ == "__main__":
    unittest.main()
