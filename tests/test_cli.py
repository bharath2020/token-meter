import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from token_meter import __main__ as cli

ENV_KEYS = ("TOKEN_METER_FIRSTMATE_HOME", "TOKEN_METER_HOSTS", "TOKEN_METER_PORT")


def make_home(root):
    for d in ("bin", "data", "state"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    return root


class CleanEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)


class ResolveHomeTest(CleanEnv):
    def test_flag_wins_over_env_and_cwd(self):
        flag = make_home(os.path.join(self.root, "flag"))
        env = make_home(os.path.join(self.root, "env"))
        cwd = make_home(os.path.join(self.root, "cwd"))
        got = cli.resolve_home(flag, env={cli.HOME_ENV: env}, cwd=cwd)
        self.assertEqual(got, flag)

    def test_env_wins_over_cwd(self):
        env = make_home(os.path.join(self.root, "env"))
        cwd = make_home(os.path.join(self.root, "cwd"))
        self.assertEqual(cli.resolve_home(None, env={cli.HOME_ENV: env}, cwd=cwd), env)

    def test_given_home_expands_user(self):
        home = make_home(os.path.join(self.root, "fm"))
        with mock.patch.dict(os.environ, {"HOME": self.root}):
            self.assertEqual(cli.resolve_home("~/fm", env={}), home)

    def test_given_home_must_exist(self):
        missing = os.path.join(self.root, "nope")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            cli.resolve_home(missing, env={})
        with self.assertRaisesRegex(ValueError, "not a directory"):
            cli.resolve_home(None, env={cli.HOME_ENV: missing})

    def test_cwd_is_home(self):
        home = make_home(os.path.join(self.root, "fm"))
        self.assertEqual(cli.resolve_home(None, env={}, cwd=home), home)

    def test_subdirectory_of_home_is_not_a_home(self):
        home = make_home(os.path.join(self.root, "fm"))
        deep = os.path.join(home, "data", "task-1")
        os.makedirs(deep)
        with self.assertRaisesRegex(ValueError, "no Firstmate home found"):
            cli.resolve_home(None, env={}, cwd=deep)

    def test_partial_layout_is_not_a_home(self):
        partial = os.path.join(self.root, "partial")
        os.makedirs(os.path.join(partial, "state"))
        os.makedirs(os.path.join(partial, "data"))
        with self.assertRaisesRegex(ValueError, "no Firstmate home found.*--home.*TOKEN_METER_FIRSTMATE_HOME"):
            cli.resolve_home(None, env={}, cwd=partial)

    def test_empty_env_value_falls_through_to_cwd(self):
        home = make_home(os.path.join(self.root, "fm"))
        self.assertEqual(cli.resolve_home(None, env={cli.HOME_ENV: ""}, cwd=home), home)


class MainDefaultsTest(CleanEnv):
    def run_main(self, argv, cwd=None):
        served = {}

        def fake_serve(monitor, hosts, port, interval):
            served.update(monitor=monitor, hosts=hosts, port=port)
            return 0

        with mock.patch.object(cli, "Monitor", side_effect=lambda projects, home: ("monitor", home)), \
                mock.patch("token_meter.server.serve", fake_serve), \
                mock.patch("os.getcwd", return_value=cwd or self.root):
            cli.main(argv)
        return served

    def test_default_host_is_loopback_only(self):
        home = make_home(os.path.join(self.root, "fm"))
        served = self.run_main(["web", "--home", home])
        self.assertEqual(served["hosts"], ["127.0.0.1"])
        self.assertEqual(served["port"], 4791)
        self.assertEqual(served["monitor"], ("monitor", home))

    def test_host_flag_adds_addresses(self):
        home = make_home(os.path.join(self.root, "fm"))
        served = self.run_main(["web", "--home", home, "--host", "127.0.0.1, 192.0.2.7,"])
        self.assertEqual(served["hosts"], ["127.0.0.1", "192.0.2.7"])

    def test_hosts_env(self):
        home = make_home(os.path.join(self.root, "fm"))
        with mock.patch.dict(os.environ, {"TOKEN_METER_HOSTS": "127.0.0.1,192.0.2.8"}):
            served = self.run_main(["web", "--home", home])
        self.assertEqual(served["hosts"], ["127.0.0.1", "192.0.2.8"])

    def test_home_from_env(self):
        home = make_home(os.path.join(self.root, "fm"))
        with mock.patch.dict(os.environ, {cli.HOME_ENV: home}):
            served = self.run_main(["web"])
        self.assertEqual(served["monitor"], ("monitor", home))

    def test_home_from_cwd(self):
        home = make_home(os.path.join(self.root, "fm"))
        served = self.run_main(["web"], cwd=home)
        self.assertEqual(served["monitor"], ("monitor", home))

    def test_no_home_is_a_clear_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            self.run_main(["web"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("no Firstmate home found", err.getvalue())
        self.assertIn("--home", err.getvalue())

    def test_help_needs_no_home(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("(default 127.0.0.1)", " ".join(out.getvalue().split()))


if __name__ == "__main__":
    unittest.main()
