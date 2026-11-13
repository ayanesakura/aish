"""Isolated tests for `aish doctor` and the pipe-mode login notification."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aish
from aish_doctor import Doctor

URL = "https://ais.example.test/projects/modeling/experiment/web-shell?projectId=1&experimentId=2&runId=3&podName=run-3-master-0&containerName=main"
RUNS = [{"id": 3, "name": "new", "status": "Running"}, {"id": 1, "name": "old", "status": "Stopped"}]
ALL_PRESENT = "python3=ok\nbash=ok\ntmux=ok\nclaude=ok\nnode=ok\n"


def result(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.script = Path(self.temp.name) / "aish.py"
        self.script.write_text("")
        self.lines = []
        self.machines = []
        self.probe = result(ALL_PRESENT)
        self.hosts = {}
        self.runs = RUNS
        for patcher in (patch.object(aish, "load_hosts", side_effect=lambda: self.hosts),
                        patch.object(aish, "list_runs", side_effect=self.list_runs)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)

    def list_runs(self, target, jar, n=20):
        if isinstance(self.runs, Exception):
            raise self.runs
        return self.runs

    def run_tool(self, argv, **kwargs):
        if argv[:3] == ["hc", "machine", "ls"]:
            return result(json.dumps({"machines": self.machines}))
        if argv[:2] == ["hc", "exec"]:
            if isinstance(self.probe, Exception):
                raise self.probe
            return self.probe
        raise AssertionError(argv)

    def doctor(self):
        return Doctor(aish, which=lambda name: "/bin/" + name, run=self.run_tool, out=self.lines.append)

    def machine(self, run=3, state="connected", script=None):
        url = URL.replace("runId=3", f"runId={run}").replace("run-3-", f"run-{run}-")
        return {"name": f"pod-{run}", "via": True, "state": state,
                "target": shlex.join(["uv", "run", "--script", str(script or self.script), url, "--pipe"])}

    def text(self):
        return "\n".join(self.lines)

    def test_healthy_following_host_and_machine(self):
        self.hosts = {"gpu": {"url": URL, "follow": True}}
        self.machines = [self.machine(), {"name": "vps", "via": False, "target": "vps", "state": "connected"}]
        check = self.doctor()
        check.hosts()
        check.machines()
        self.assertEqual((check.failed, check.warned), (0, 0), self.text())
        self.assertIn("cookie 有效", self.text())
        self.assertIn("最新的运行中 run 是 3", self.text())
        self.assertIn("已连接 pod run-3-master-0", self.text())
        self.assertNotIn("vps", self.text())

    def test_expired_cookie_reported_once_with_fix(self):
        self.hosts = {"a": {"url": URL, "follow": True}, "b": {"url": URL, "follow": True}}
        self.machines = [self.machine()]
        self.runs = aish.AishError("denied", auth=True)
        check = self.doctor()
        check.hosts()
        check.machines()
        self.assertEqual(check.failed, 1, self.text())
        self.assertIn("aish login", self.text())

    def test_pinned_host_on_ended_run_fails(self):
        self.hosts = {"old": {"url": URL.replace("runId=3", "runId=1"), "follow": False}}
        check = self.doctor()
        check.hosts()
        self.assertEqual(check.failed, 1)
        self.assertIn("Stopped", self.text())

    def test_no_running_run_is_a_warning(self):
        self.hosts = {"gpu": {"url": URL, "follow": True}}
        self.runs = [{"id": 1, "status": "Stopped"}]
        check = self.doctor()
        check.hosts()
        self.assertEqual((check.failed, check.warned), (0, 1))

    def test_unknown_host_name(self):
        with self.assertRaises(aish.AishError):
            self.doctor().hosts("missing")

    def test_machine_with_missing_transport_script(self):
        self.machines = [self.machine(script="/nonexistent/dir/aish.py")]
        check = self.doctor()
        check.machines()
        self.assertEqual(check.failed, 1)
        self.assertIn("已不存在", self.text())

    def test_machine_on_ended_run_points_to_latest(self):
        self.machines = [self.machine(run=1)]
        check = self.doctor()
        check.machines()
        self.assertEqual(check.failed, 1)
        self.assertIn("--latest", self.text())

    def test_disconnected_and_unresponsive_machines(self):
        self.machines = [self.machine(state="connecting")]
        check = self.doctor()
        check.machines()
        self.assertEqual(check.failed, 1)
        self.probe = subprocess.TimeoutExpired("hc", 30)
        self.machines = [self.machine()]
        check = self.doctor()
        check.machines()
        self.assertEqual(check.failed, 1)
        self.assertIn("30 秒", self.text())

    def test_missing_pod_commands(self):
        self.machines = [self.machine()]
        self.probe = result("python3=ok\nbash=ok\ntmux=ok\nclaude=missing\nnode=missing\n")
        check = self.doctor()
        check.machines()
        self.assertEqual((check.failed, check.warned), (0, 1), self.text())
        self.probe = result("python3=missing\nbash=ok\ntmux=ok\nclaude=ok\nnode=ok\n")
        check = self.doctor()
        check.machines()
        self.assertEqual(check.failed, 1)

    def test_missing_tools_and_cookie_file(self):
        check = Doctor(aish, which=lambda name: None, run=self.run_tool, out=self.lines.append)
        check.tools()
        self.assertEqual((check.failed, check.warned), (1, 1))
        with patch.dict(os.environ, {"AIS_TOKEN_FILE": str(Path(self.temp.name) / "absent")}):
            self.assertFalse(check.cookie())
        self.assertEqual(check.failed, 2)

    def test_runtime_states(self):
        home = Path(self.temp.name)
        check = self.doctor()
        check.runtime(home)
        self.assertEqual(check.warned, 1)
        release = home / ".local/share/aish/releases/abc"
        release.mkdir(parents=True)
        (home / ".local/share/aish/current").symlink_to(release)
        check.runtime(home)
        self.assertEqual(check.warned, 1)
        self.assertIn("开发目录", self.text())


class NotifyTests(unittest.TestCase):
    def test_rate_limited_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {"AIS_TOKEN_FILE": str(Path(directory) / "token")}
            with patch.dict(os.environ, environment), patch.object(aish.sys, "platform", "darwin"), \
                 patch.object(aish.subprocess, "run") as run:
                os.environ.pop("AISH_NO_NOTIFY", None)
                aish.notify_auth_failure()
                aish.notify_auth_failure()
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[0][0], "osascript")
                old = aish.time.time() - aish.AUTH_NOTIFY_INTERVAL - 1
                os.utime(Path(directory) / "auth-notified", (old, old))
                aish.notify_auth_failure()
                self.assertEqual(run.call_count, 2)
                os.utime(Path(directory) / "auth-notified", (old, old))
                with patch.dict(os.environ, {"AISH_NO_NOTIFY": "1"}):
                    aish.notify_auth_failure()
                self.assertEqual(run.call_count, 2)

    def test_silent_off_macos(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"AIS_TOKEN_FILE": str(Path(directory) / "token")}), \
                 patch.object(aish.sys, "platform", "linux"), patch.object(aish.subprocess, "run") as run:
                os.environ.pop("AISH_NO_NOTIFY", None)
                aish.notify_auth_failure()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
