"""Isolated tests for native remote-space onboarding and shared installation."""

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
from aish_herdr import Connector, canonical_url, identity, machine_name, shell_config, via_target
import install_herdr_skill

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
        self.probe = {"folder": "/workspace/verl", "config": "notes = false\n",
                      "node_source": None, "warnings": [], "tmux_main": True,
                      "tmux": "/usr/bin/tmux", "claude": "/home/work/.local/bin/claude"}

    def remote(self, machine, action, payload):
        if action == "probe":
            return self.probe
        return {"warnings": [], "config_backup": "/config.backup"}

    def run(self, tool, *args, **kwargs):
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
            first = install_herdr_skill.install(home)
            second = install_herdr_skill.install(home)
            self.assertEqual(first["runtime"], second["runtime"])
            self.assertEqual(Path(first["codex_skill"]).resolve(), Path(first["claude_skill"]).resolve())
            self.assertTrue(os.access(first["command"], os.X_OK))
            self.assertIn(" herdr ", Path(first["command"]).read_text())

    def test_installer_refuses_unrelated_skill(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            skill = home / ".claude/skills/herdr-pod"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("keep")
            with self.assertRaises(RuntimeError):
                install_herdr_skill.install(home)
            self.assertEqual((skill / "SKILL.md").read_text(), "keep")
            self.assertFalse((home / ".local/share/aish/current").exists())


if __name__ == "__main__":
    unittest.main()
