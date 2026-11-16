"""Isolated tests for native remote-space onboarding and shared installation."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aish
from aish_herdr import (Connector, canonical_url, identity, machine_name, prepare_script, resolve_target,
                        shell_config, stale_machines, via_target)
import install as installer

spec = importlib.util.spec_from_file_location("herdr_setup", Path(__file__).resolve().parents[1] / "remote/herdr_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

URL = "https://ais.example.test/projects/modeling/experiment/web-shell?projectId=1&experimentId=2&runId=3&podName=run-3-master-0&containerName=main"


class FakeConnector(Connector):
    def __init__(self):
        super().__init__(aish, {"uv": "/opt/tools with spaces/uv"})
        self.machines = []
        self.spaces = [{"id": "local/space", "machine": None, "folder": "/workspace/verl"}]
        self.added = 0
        self.created = 0
        self.wrong_binding = False
        self.calls = []
        self.connecting = 0
        self.probe = {"folder": "/workspace/verl", "config": "notes = false\n",
                      "node_source": None, "warnings": [], "tmux_main": True,
                      "tmux": "/usr/bin/tmux", "claude": "/home/work/.local/bin/claude"}

    def remote(self, machine, action, payload):
        if action == "probe":
            return self.probe
        return {"warnings": [], "config_backup": "/config.backup"}

    def run(self, tool, *args, **kwargs):
        self.calls.append((tool, args))
        if tool == "uv":
            return ""
        if args[:2] == ("do", "space.new") and self.connecting:
            self.connecting -= 1
            return json.dumps({"error": "pod is connecting"})
        if args[:2] == ("machine", "ls"):
            return json.dumps({"machines": self.machines})
        if args[:2] == ("machine", "add"):
            self.added += 1
            self.machines.append({"name": args[3], "target": args[5], "via": True})
            return "{}"
        if args == ("ls",):
            return json.dumps({"spaces": self.spaces})
        if args[:2] == ("do", "space.new"):
            self.created += 1
            self.creation = json.loads(args[2])
            machine = self.creation["machine"]
            self.spaces.append({"id": machine + "/space", "name": "Pod", "machine": machine,
                                "folder": self.creation["folder"], "tabs": [{"terminals": [
                                    {"terminal": "t1" if self.wrong_binding else machine + "/t1"}]}]})
            return "{}"
        if args[:2] == ("do", "space.select"):
            return "{}"
        raise AssertionError((tool, args, kwargs))


class OnboardingTests(unittest.TestCase):
    def test_pinned_sanitized_url_and_name(self):
        target = aish.target_from(URL + "&token=secret-value&extra=discard")
        self.assertNotIn("secret", canonical_url(target))
        self.assertEqual(identity(aish.target_from(canonical_url(target))), identity(target))
        first = machine_name(target)
        target.container = "worker"
        self.assertNotEqual(first, machine_name(target))
        self.assertLessEqual(len(first), 32)

    def test_repeated_connection_reuses_remote_machine_space(self):
        connector = FakeConnector()
        first = connector.attach(aish.target_from(URL))
        second = connector.attach(aish.target_from(URL))
        self.assertEqual((connector.added, connector.created), (1, 1))
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["space"], second["space"])
        self.assertEqual(connector.creation["argv"], ["/usr/bin/tmux", "-u", "attach-session", "-t", "=main"])
        self.assertIsNone(connector.spaces[0]["machine"])
        words = shlex.split(connector.machines[0]["target"])
        self.assertEqual(words[0], "/opt/tools with spaces/uv")
        self.assertEqual(words[-1], "--pipe")

    def test_existing_manually_named_machine_is_reused(self):
        connector = FakeConnector()
        connector.machines = [{"name": "training-pod", "via": True,
                               "target": "uv run --script /aish.py gpu --run 3 --pod run-3-master-0 --pipe"}]
        with patch.object(aish, "load_hosts", return_value={"gpu": {"url": URL, "follow": True}}):
            result = connector.attach(aish.target_from(URL))
        self.assertEqual(result["machine"], "training-pod")
        self.assertEqual(connector.added, 0)

    def test_following_alias_does_not_match(self):
        machine = {"via": True, "target": "uv run --script /aish.py gpu --pipe"}
        with patch.object(aish, "load_hosts", return_value={"gpu": {"url": URL, "follow": True}}):
            self.assertIsNone(via_target(machine, aish))

    def test_following_alias_needs_latest(self):
        hosts = {"gpu": {"url": URL, "follow": True}}
        with patch.object(aish, "load_hosts", return_value=hosts):
            with self.assertRaises(aish.AishError) as raised:
                resolve_target("gpu", False, aish, None)
            self.assertIn("--latest", str(raised.exception))

            def newest(target, jar):
                target.run, target.pod = "9", "run-9-master-0"

            with patch.object(aish, "resolve_latest", side_effect=newest) as resolved:
                target = resolve_target("gpu", True, aish, object())
            resolved.assert_called_once()
        self.assertFalse(target.follow)
        self.assertEqual((target.run, target.pod), ("9", "run-9-master-0"))
        self.assertIn("runId=9", canonical_url(target))

    def test_pinned_url_resolves_without_network(self):
        with patch.object(aish, "resolve_latest", side_effect=AssertionError("must not query")):
            target = resolve_target(URL, False, aish, None)
        self.assertEqual(target.run, "3")

    def test_stale_machines_only_reports_ended_runs_of_same_experiment(self):
        def machine(name, run, experiment="2"):
            url = URL.replace("runId=3", f"runId={run}").replace("run-3-", f"run-{run}-")
            return {"name": name, "via": True,
                    "target": shlex.join(["uv", "run", "--script", "/x/aish.py",
                                          url.replace("experimentId=2", f"experimentId={experiment}"), "--pipe"])}

        machines = [machine("current", 3), machine("ended", 1), machine("still-running", 2),
                    machine("unlisted", 0), machine("other-experiment", 1, experiment="7"),
                    {"name": "ssh-box", "via": False, "target": "vps"}]
        runs = [{"id": 3, "status": "Running"}, {"id": 2, "status": "Running"}, {"id": 1, "status": "Stopped"}]
        stale = stale_machines(machines, aish.target_from(URL), runs, aish)
        self.assertEqual([(item["machine"], item["status"]) for item in stale],
                         [("ended", "Stopped"), ("unlisted", "不在最近的 run 列表中")])

    def test_prepare_script_runs_on_pod_before_core_install(self):
        connector = FakeConnector()
        script = "[ -f /persist/bootstrap.sh ] || exit 0\nbash /persist/bootstrap.sh\n"
        connector.attach(aish.target_from(URL), prepare=script)
        steps = [(tool, args[:2]) for tool, args in connector.calls]
        prepared = next(index for index, (tool, _) in enumerate(steps) if tool == "uv")
        self.assertLess(prepared, steps.index(("hc", ("machine", "add"))))
        args = connector.calls[prepared][1]
        self.assertEqual(args[4], canonical_url(aish.target_from(URL)))
        command = args[args.index("--exec") + 1]
        self.assertEqual(base64.b64decode(command.split()[1]).decode(), script)
        self.assertNotIn("\n", command)
        connector.attach(aish.target_from(URL), prepare=script)
        self.assertEqual(sum(tool == "uv" for tool, _ in connector.calls), 2)
        self.assertEqual(connector.added, 1)

    def test_no_prepare_script_means_no_extra_connection(self):
        connector = FakeConnector()
        connector.attach(aish.target_from(URL))
        self.assertFalse([tool for tool, _ in connector.calls if tool == "uv"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "herdr-prepare.sh"
            with patch.dict(os.environ, {"AISH_HERDR_PREPARE": str(path)}):
                self.assertIsNone(prepare_script(aish))
                path.write_text("  \n")
                self.assertIsNone(prepare_script(aish))
                path.write_text("true\n")
                self.assertEqual(prepare_script(aish), "true\n")

    def test_failed_prepare_script_stops_before_registration(self):
        connector = FakeConnector()

        def failing(tool, *args, **kwargs):
            raise aish.AishError("uv 执行失败：bootstrap 出错")

        with patch.object(connector, "run", side_effect=failing):
            with self.assertRaises(aish.AishError):
                connector.attach(aish.target_from(URL), prepare="false\n")
        self.assertEqual((connector.added, connector.created), (0, 0))

    def test_space_creation_waits_while_machine_is_connecting(self):
        connector = FakeConnector()
        connector.connecting = 2
        with patch("aish_herdr.time.sleep") as slept:
            result = connector.attach(aish.target_from(URL))
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(connector.created, 1)
        self.assertTrue(result["created"])

    def test_name_collision_refuses_without_changes(self):
        connector = FakeConnector()
        connector.machines = [{"name": "occupied", "via": False, "target": "other"}]
        with self.assertRaises(aish.AishError):
            connector.attach(aish.target_from(URL), name="occupied")
        self.assertEqual((connector.added, connector.created), (0, 0))

    def test_local_terminal_in_remote_space_is_rejected(self):
        connector = FakeConnector()
        connector.wrong_binding = True
        with self.assertRaises(aish.AishError):
            connector.attach(aish.target_from(URL))

    def test_new_claude_or_shell_when_no_tmux(self):
        connector = FakeConnector()
        connector.probe["tmux_main"] = False
        connector.attach(aish.target_from(URL))
        self.assertEqual(connector.creation["argv"], ["/home/work/.local/bin/claude"])
        connector = FakeConnector()
        connector.probe.update(tmux_main=False, claude=None)
        connector.attach(aish.target_from(URL))
        self.assertEqual(connector.creation["argv"], ["/bin/bash", "--norc"])

    def test_toml_preserves_existing_options_and_shell(self):
        text = '# custom\nnotes = false\n[templates.example]\ncommand = "hello"\n'
        rendered = shell_config(text)
        import tomlkit
        parsed = tomlkit.parse(rendered)
        self.assertEqual(parsed["shell"], ["/bin/bash", "--norc"])
        self.assertFalse(parsed["notes"])
        self.assertEqual(parsed["templates"]["example"]["command"], "hello")
        self.assertIn("# custom", rendered)
        custom = 'shell = ["zsh"]\n'
        self.assertEqual(shell_config(custom), custom)

    def test_remote_apply_backup_and_concurrent_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("notes = false\n")
            payload = {"config_before": path.read_text(), "config_after": shell_config(path.read_text())}
            with patch.object(setup, "config_path", return_value=path):
                result = setup.apply(payload)
                self.assertEqual(Path(result["config_backup"]).read_text(), payload["config_before"])
                self.assertEqual(path.read_text(), payload["config_after"])
                with self.assertRaises(RuntimeError):
                    setup.apply(payload)
                self.assertEqual(path.read_text(), payload["config_after"])

    def test_node_link_never_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            destination = home / ".local/bin/node"
            destination.parent.mkdir(parents=True)
            destination.write_text("keep")
            source = home / "nvm/node"
            source.parent.mkdir()
            source.write_text("node")
            source.chmod(0o755)
            payload = {"config_before": "", "config_after": "", "node_source": str(source)}
            with patch.object(setup, "config_path", return_value=home / "config.toml"), \
                 patch.object(Path, "home", return_value=home), \
                 patch.dict(os.environ, {"PATH": str(destination.parent)}):
                setup.apply(payload)
                self.assertEqual(destination.read_text(), "keep")
                destination.unlink()
                setup.apply(payload)
                self.assertEqual(destination.resolve(), source.resolve())

    def test_installer_shared_skill_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            first = installer.install(home)
            second = installer.install(home)
            self.assertEqual(first["runtime"], second["runtime"])
            self.assertEqual(Path(first["codex_skill"]).resolve(), Path(first["claude_skill"]).resolve())
            self.assertTrue(os.access(first["command"], os.X_OK))
            self.assertIn(" herdr ", Path(first["command"]).read_text())
            self.assertTrue(os.access(first["aish"], os.X_OK))
            self.assertNotIn(" herdr ", Path(first["aish"]).read_text())
            self.assertTrue((Path(first["runtime"]) / "aish_doctor.py").is_file())

    def test_installer_refuses_unrelated_aish_command(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            command = home / ".local/bin/aish"
            command.parent.mkdir(parents=True)
            command.write_text("keep")
            with self.assertRaises(RuntimeError):
                installer.install(home)
            self.assertEqual(command.read_text(), "keep")

    def test_installer_refuses_unrelated_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            skill = home / ".claude/skills/herdr-pod"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("keep")
            with self.assertRaises(RuntimeError):
                installer.install(home)
            self.assertEqual((skill / "SKILL.md").read_text(), "keep")
            self.assertFalse((home / ".local/share/aish/current").exists())


if __name__ == "__main__":
    unittest.main()
