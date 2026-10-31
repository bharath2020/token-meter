"""Runs install.sh against a local tarball into a temporary HOME."""

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "install.sh")


def build_tarball(dest, marker=None):
    """A GitHub-style archive: everything under one top-level token-meter-main/ directory."""
    def skip_cache(info):
        return None if "__pycache__" in info.name or info.name.endswith(".pyc") else info

    with tarfile.open(dest, "w:gz") as tar:
        tar.add(os.path.join(REPO, "token-meter"), "token-meter-main/token-meter")
        tar.add(os.path.join(REPO, "token_meter"), "token-meter-main/token_meter", filter=skip_cache)
        tar.add(os.path.join(REPO, "README.md"), "token-meter-main/README.md")
        if marker:
            info = tarfile.TarInfo("token-meter-main/VERSION-MARKER")
            data = marker.encode()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return dest


@unittest.skipUnless(shutil.which("sh") and shutil.which("curl"), "needs sh and curl")
class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.tarball = build_tarball(os.path.join(self.root, "main.tar.gz"))

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TOKEN_METER_")}
        env.update(HOME=self.home, TMPDIR=self.root, TOKEN_METER_TARBALL_URL="file://" + self.tarball)
        env.update(extra)
        return env

    def install(self, **extra):
        # Same shape as the documented `curl ... | sh`: the script arrives on stdin.
        with open(SCRIPT, "rb") as f:
            return subprocess.run(["sh"], stdin=f, env=self.env(**extra), capture_output=True,
                                  text=True, timeout=60)

    def test_installs_launcher_that_runs_help(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        install_dir = os.path.join(self.home, ".local", "share", "token-meter")
        launcher = os.path.join(self.home, ".local", "bin", "token-meter")
        self.assertTrue(os.path.isdir(os.path.join(install_dir, "token_meter")))
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertIn(install_dir, result.stdout)
        self.assertIn("is not on your PATH", result.stdout)

        run = subprocess.run([launcher, "--help"], env=self.env(), capture_output=True, text=True,
                             timeout=60, cwd=self.root)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("usage: token-meter", run.stdout)

    def test_launcher_ignores_token_meter_package_in_cwd(self):
        self.assertEqual(self.install().returncode, 0)
        launcher = os.path.join(self.home, ".local", "bin", "token-meter")
        cwd = os.path.join(self.root, "untrusted")
        os.makedirs(os.path.join(cwd, "token_meter"))
        with open(os.path.join(cwd, "token_meter", "__init__.py"), "w"):
            pass
        with open(os.path.join(cwd, "token_meter", "__main__.py"), "w") as f:
            f.write("print('SHADOWED-PACKAGE-RAN')\n")

        run = subprocess.run([launcher, "--help"], env=self.env(), capture_output=True, text=True,
                             timeout=60, cwd=cwd)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("SHADOWED-PACKAGE-RAN", run.stdout + run.stderr)
        self.assertIn("usage: token-meter", run.stdout)

    def test_no_path_warning_when_bin_dir_on_path(self):
        bin_dir = os.path.join(self.home, ".local", "bin")
        result = self.install(PATH=bin_dir + os.pathsep + os.environ.get("PATH", ""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("is not on your PATH", result.stdout)

    def test_rerun_replaces_previous_install(self):
        self.assertEqual(self.install().returncode, 0)
        install_dir = os.path.join(self.home, ".local", "share", "token-meter")
        stale = os.path.join(install_dir, "token_meter", "stale.py")
        with open(stale, "w") as f:
            f.write("# left over from an older version\n")
        build_tarball(self.tarball, marker="v2")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(install_dir, "VERSION-MARKER")))

    def test_refuses_to_replace_unrelated_directory(self):
        install_dir = os.path.join(self.home, ".local", "share", "token-meter")
        os.makedirs(install_dir)
        keep = os.path.join(install_dir, "keep.txt")
        with open(keep, "w") as f:
            f.write("mine\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a token-meter install", result.stderr)
        self.assertTrue(os.path.exists(keep))

    def test_download_failure_is_reported(self):
        result = self.install(TOKEN_METER_TARBALL_URL="file://" + os.path.join(self.root, "missing.tar.gz"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("download failed", result.stderr)

    def test_requires_curl(self):
        tools = self.tools_without("curl")
        result = self.install(PATH=tools)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("curl is required", result.stderr)

    def tools_without(self, missing):
        tools = os.path.join(self.root, "tools")
        os.makedirs(tools)
        for name in ("sh", "tar", "curl", "python3", "mktemp", "rm", "mkdir", "cp", "mv", "sed",
                     "chmod", "dirname", "ls", "cat"):
            found = shutil.which(name)
            if found and name != missing:
                os.symlink(found, os.path.join(tools, name))
        return tools

    def test_requires_python3(self):
        result = self.install(PATH=self.tools_without("python3"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python3 is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
