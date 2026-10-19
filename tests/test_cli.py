import contextlib
import io
import os
import plistlib
import subprocess
import tempfile
import unittest
from unittest import mock

from tvcast import cli, launcher
from tvcast.cli import pick

VIDEOS = [(label, label) for label in ("Alien (1979)", "Aliens (1986)", "Dune (1984)", "Dune (2021)")]


class PickTest(unittest.TestCase):
    def test_exact_label_beats_partial_matches(self):
        self.assertEqual(pick("alien", VIDEOS, "video"), "Alien (1979)")
        self.assertEqual(pick("Dune (2021)", VIDEOS, "video"), "Dune (2021)")
        self.assertEqual(pick("aliens", VIDEOS, "video"), "Aliens (1986)")

    def test_ambiguous_or_missing_exits_with_the_choices(self):
        with self.assertRaises(SystemExit) as cm:
            pick("dune", VIDEOS, "video")
        self.assertIn("Dune (1984)\n  Dune (2021)", str(cm.exception))
        with self.assertRaises(SystemExit):
            pick("arrival", VIDEOS, "video")


class InstallTest(unittest.TestCase):
    def test_login_service_uses_the_packaged_launcher(self):
        with tempfile.TemporaryDirectory(prefix="tvcast install ") as tmp:
            plist = os.path.join(tmp, "agent.plist")
            with mock.patch.object(cli, "PLIST", plist), \
                    mock.patch.object(cli, "api", return_value={"now": None}), \
                    mock.patch.object(cli.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 1),
                        subprocess.CompletedProcess([], 0),
                    ]) as run, contextlib.redirect_stdout(io.StringIO()):
                cli.install()
            with open(plist, "rb") as f:
                args = plistlib.load(f)["ProgramArguments"]
            self.assertEqual(args, ["/usr/bin/python3", "-I", launcher.__file__, "daemon"])
            self.assertEqual(run.call_args.args[0][-1], plist)

    def test_install_refuses_to_interrupt_playback(self):
        with mock.patch.object(cli, "api", return_value={"now": {"title": "Movie", "year": None}}), \
                mock.patch.object(cli.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "playing"):
                cli.install()
            run.assert_not_called()


class LauncherTest(unittest.TestCase):
    def test_installed_entry_point_uses_isolated_apple_python(self):
        with mock.patch.object(launcher.sys, "platform", "darwin"), \
                mock.patch.object(launcher.sys, "argv", ["tv", "--version"]), \
                mock.patch.object(launcher.os, "execv") as execute:
            launcher.main()
        execute.assert_called_once_with(
            "/usr/bin/python3", ["/usr/bin/python3", "-I", launcher.__file__, "--version"])

    def test_missing_apple_python_explains_the_prerequisite(self):
        with mock.patch.object(launcher.sys, "platform", "darwin"), \
                mock.patch.object(launcher.os, "execv", side_effect=FileNotFoundError("python3")):
            with self.assertRaisesRegex(SystemExit, "xcode-select --install"):
                launcher.main()


if __name__ == "__main__":
    unittest.main()
