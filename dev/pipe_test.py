"""Run binary transport checks against an isolated local WebShell mock."""

import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TARGET = "https://mock.invalid/y?containerName=main&experimentId=1&podName=p&projectId=2&runId=3"


class PipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="aish-test-", dir="/tmp")
        cls.directory = Path(cls.temp.name)
        (cls.directory / "tmux").mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.port = listener.getsockname()[1]
        cookie = cls.directory / "cookie"
        cookie.write_text("mockcookie123456")
        cls.env = dict(os.environ, AIS_BASE_URL=f"http://127.0.0.1:{cls.port}",
                       AIS_TOKEN_FILE=str(cookie), AISH_HOSTS_FILE=str(cls.directory / "hosts.json"),
                       AISH_NO_BROWSER_COOKIE="1", MOCK_KEEP_OPEN="1", TMUX_TMPDIR=str(cls.directory / "tmux"))
        cls.remote_env = dict(cls.env, HC_SESSION="r", HC_SOCKET=str(cls.directory / "r"),
                              XDG_CONFIG_HOME=str(cls.directory / "remote-config"),
                              XDG_DATA_HOME=str(cls.directory / "remote-data"),
                              XDG_STATE_HOME=str(cls.directory / "remote-state"))
        cls.local_env = dict(cls.env, HC_SESSION="l", HC_SOCKET=str(cls.directory / "l"),
                             XDG_CONFIG_HOME=str(cls.directory / "local-config"),
                             XDG_DATA_HOME=str(cls.directory / "local-data"),
                             XDG_STATE_HOME=str(cls.directory / "local-state"))
        cls.log = cls.directory / "mock.log"
        cls.server = subprocess.Popen(["uv", "run", "--script", str(ROOT / "dev/mock_server.py"),
                                       str(cls.port), str(cls.log)], env=cls.remote_env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.log.exists() and "mock listening" in cls.log.read_text():
                return
            if cls.server.poll() is not None:
                raise RuntimeError(cls.server.stderr.read().decode())
            time.sleep(0.05)
        raise RuntimeError("mock startup timeout")

    @classmethod
    def tearDownClass(cls):
        if shutil.which("hc"):
            for environment in (cls.local_env, cls.remote_env):
                subprocess.run(["hc", "server", "stop"], env=environment, capture_output=True, timeout=10)
        cls.server.terminate()
        cls.server.communicate(timeout=10)
        cls.temp.cleanup()

    def run_pipe(self, command, data=b"", timeout=20):
        return subprocess.run(["uv", "run", "--script", str(ROOT / "aish.py"), TARGET,
                               "--timeout", "5", "--pipe", command], env=self.env, input=data,
                              capture_output=True, timeout=timeout)

    def test_binary_roundtrip(self):
        data = bytes(range(256)) * 4096
        result = self.run_pipe("cat", data, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, data)
        self.assertEqual(result.stderr, b"")

    def test_stderr_and_exit(self):
        result = self.run_pipe("printf 'out'; printf 'err' >&2; exit 17")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (17, b"out", b"err"))

    def test_empty_stdin_and_quoted_command(self):
        program = "import sys; print(repr(sys.stdin.buffer.read())); print(\"中文 ' \\\" $HOME\")"
        result = self.run_pipe("python3 -c " + shlex.quote(program))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("中文 ' \" $HOME".encode(), result.stdout)
        self.assertTrue(result.stdout.startswith(b"b''\n"))

    def test_remote_closes_input_early(self):
        result = self.run_pipe("exec 0<&-; printf 'done'", b"x" * 100000)
        self.assertEqual((result.returncode, result.stdout), (0, b"done"))

    def test_large_stderr(self):
        result = self.run_pipe("python3 -c 'import sys; sys.stderr.buffer.write(bytes(range(256))*1024)'")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, bytes(range(256)) * 1024)

    def test_follow_rejected_before_connecting(self):
        import json
        Path(self.env["AISH_HOSTS_FILE"]).write_text(json.dumps({"hosts": {"gpu": {"url": TARGET, "follow": True}}}))
        result = subprocess.run(["uv", "run", "--script", str(ROOT / "aish.py"), "gpu",
                                 "--pipe", "true"], env=self.env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertIn("必须固定".encode(), result.stderr)

    @unittest.skipUnless(shutil.which("hc"), "hc not installed")
    def test_native_herdr_machine(self):
        via = shlex.join(["uv", "run", "--script", str(ROOT / "aish.py"), TARGET, "--pipe"])
        added = subprocess.run(["hc", "machine", "add", "--name", "pipe-mock", "--via", via],
                               env=self.local_env, stdin=subprocess.DEVNULL, capture_output=True, timeout=45)
        self.assertEqual(added.returncode, 0, added.stderr)
        result = subprocess.run(["hc", "exec", "--on", "pipe-mock", "-i", "cat"],
                                env=self.local_env, input=bytes(range(256)) * 256,
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, bytes(range(256)) * 256)

    @unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
    def test_shared_attach_preserves_session(self):
        socket_name = "aish-" + self.directory.name[-8:]
        environment = dict(self.env, AISH_TMUX_SOCKET=socket_name)
        Path(environment["TMUX_TMPDIR"]).mkdir(exist_ok=True)
        tmux = ["tmux", "-L", socket_name]
        subprocess.run(tmux + ["new-session", "-d", "-s", "shared", "bash --norc --noprofile"],
                       env=environment, check=True)
        subprocess.run(tmux + ["set-option", "-g", "mouse", "off"], env=environment, check=True)
        clients = []
        screens = []
        stop_reading = threading.Event()
        readers = []
        try:
            for _ in range(2):
                master, slave = os.openpty()
                client = subprocess.Popen(["uv", "run", "--script", str(ROOT / "aish.py"), TARGET,
                                           "--attach", "shared"], env=environment,
                                          stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                os.close(slave)
                clients.append((client, master))
                screen = bytearray()
                screens.append(screen)
                def drain(descriptor=master, buffer=screen):
                    while not stop_reading.is_set():
                        readable, _, _ = select.select([descriptor], [], [], 0.1)
                        if readable:
                            try:
                                data = os.read(descriptor, 65536)
                            except OSError:
                                return
                            if not data:
                                return
                            buffer.extend(data)
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                readers.append(reader)
            deadline = time.monotonic() + 15
            count = 0
            while time.monotonic() < deadline:
                result = subprocess.run(tmux + ["list-clients", "-F", "#{client_session}"],
                                        env=environment, capture_output=True, text=True, check=True)
                count = result.stdout.splitlines().count("shared")
                if count == 2:
                    break
                time.sleep(0.05)
            self.assertEqual(count, 2)
            os.write(clients[0][1], b"printf 'CONTROL_OK\\n'\r")
            deadline = time.monotonic() + 5
            while b"CONTROL_OK" not in screens[1] and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn(b"CONTROL_OK", screens[1])
            option = subprocess.run(tmux + ["show-option", "-gv", "mouse"], env=environment,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(option.stdout.strip(), "off")
        finally:
            for client, master in clients:
                if client.poll() is None:
                    os.killpg(client.pid, signal.SIGTERM)
                    try:
                        client.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(client.pid, signal.SIGKILL)
                        client.wait(timeout=5)
            stop_reading.set()
            for reader in readers:
                reader.join(timeout=1)
            for _, master in clients:
                os.close(master)
            subprocess.run(tmux + ["kill-server"], env=environment, capture_output=True)

    def test_disconnect_no_replay(self):
        program = "import sys; sys.stdout.buffer.write(b'READY'); sys.stdout.flush(); import time; time.sleep(30)"
        client = subprocess.Popen(["uv", "run", "--script", str(ROOT / "aish.py"), TARGET,
                                   "--pipe", "python3 -c " + shlex.quote(program)], env=self.env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            import select
            readable, _, _ = select.select([client.stdout], [], [], 15)
            self.assertTrue(readable)
            self.assertEqual(os.read(client.stdout.fileno(), 5), b"READY")
            client.terminate()
            _, errors = client.communicate(timeout=8)
            self.assertNotEqual(client.returncode, 0, errors)
        finally:
            if client.poll() is None:
                client.kill()
                client.communicate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
