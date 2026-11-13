"""tmux 模式端到端测试（本地 mock）：接入 / 断线自动重连 / 窗口大小 / detach / 接回 / 接管 / 会话结束。
用法: python3 tmux_test.py <aish 路径> <机器名> [--drop]
需要环境变量 AIS_BASE_URL / AIS_TOKEN_FILE / AISH_HOSTS_FILE 指向 mock。
--drop: 测断线重连（会重启本地 mock_server.py）。不加则跳过（真实 pod 上不能模拟断线）。
"""
import fcntl, os, re, select, signal, struct, subprocess, sys, termios, time

AISH, HOST = sys.argv[1], sys.argv[2]
DROP = "--drop" in sys.argv
SD = os.path.dirname(os.path.abspath(__file__))
T0 = time.time()


def step(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


class Pty:
    def __init__(self, args, rows=30, cols=100):
        self.m, s = os.openpty()
        self.setsize(rows, cols)
        self.f1, self.f2 = os.path.join(SD, f"stty_b{id(self)}"), os.path.join(SD, f"stty_a{id(self)}")
        wrapped = ["/bin/sh", "-c", f'stty -g > {self.f1}; "$@"; rc=$?; stty -g > {self.f2}; echo "AISH_RC=$rc"; sleep 0.3',
                   "sh", *args]
        self.p = subprocess.Popen(wrapped, stdin=s, stdout=s, stderr=s, start_new_session=True,
                                  preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        os.close(s)
        self.buf = bytearray()

    def setsize(self, rows, cols):
        fcntl.ioctl(self.m, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def pump(self, t=0.1):
        r, _, _ = select.select([self.m], [], [], t)
        if r:
            try:
                d = os.read(self.m, 65536)
            except OSError:
                return False
            self.buf.extend(d)
            return bool(d)
        return False

    def expect(self, pat, timeout=20, start=0):
        end = time.time() + timeout
        rx = re.compile(pat, re.S)
        while time.time() < end:
            m = rx.search(self.buf, start)
            if m:
                return m
            self.pump(0.1)
        raise SystemExit(f"TIMEOUT {pat!r}\n--- tail ---\n{self.show(self.buf[-1500:])}")

    def quiet(self, sec=1.0, maxwait=10):
        end, last = time.time() + maxwait, time.time()
        while time.time() < end and time.time() - last < sec:
            if self.pump(0.1):
                last = time.time()

    def send(self, s):
        os.write(self.m, s.encode() if isinstance(s, str) else s)

    def finish(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end and b"AISH_RC=" not in self.buf:
            self.pump(0.1)
        m = re.search(rb"AISH_RC=(\d+)", self.buf)
        self.quiet(0.3, 2)
        def norm(p):
            out = []
            for kv in open(p).read().strip().split(":"):
                if kv.startswith("lflag="):
                    kv = "lflag=%x" % (int(kv[6:], 16) & ~0x20000000)  # 屏蔽 macOS 的 PENDIN
                out.append(kv)
            return out
        restored = os.path.exists(self.f2) and norm(self.f1) == norm(self.f2)
        msgs = [x.decode("utf-8", "replace") for x in re.findall(rb"\[aish\] [^\r\n]*", self.buf)]
        for x in msgs:
            if "ping" in x or "opsresize" in x:
                print("      日志:", x)
        return (m.group(1).decode() if m else "?"), restored, msgs

    @staticmethod
    def show(b):
        s = bytes(b).decode("utf-8", "replace")
        return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()=>][0-9A-Za-z]?", "", s).replace("\r", "")


def start_mock():
    subprocess.Popen([os.path.join(SD, "mock_server.py"), "18765", os.path.join(SD, "mock.log")],
                     env={**os.environ, "MOCK_KEEP_OPEN": "1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    time.sleep(2.5)


tag = f"m{int(time.time()) % 100000}"
# ---------------------------------------------------------------- 1. 接入
a = Pty([AISH, HOST])
a.expect(rb"\x1b\[\?1049h", 30)
a.quiet(1.5)
hidden_ok = b"new-session" not in a.buf
step(f"1. 已接入 tmux（备用屏幕已进入）；tmux 命令行未显示在屏幕上 = {hidden_ok}")
a.send(f"echo {tag}_one\r")
a.expect(rf"{tag}_one\r\n".encode())
a.quiet(0.5)
step("   tmux 里输入 echo 正常")

# ---------------------------------------------------------------- 2. 断线重连
if DROP:
    subprocess.run(["pkill", "-f", "mock_server.py"])
    step("2. 杀掉 mock 服务器（模拟断线）")
    a.expect(rb"\[aish\] [^\r\n]*\xe9\x87\x8d\xe8\xbf\x9e", 20)  # “重连”
    time.sleep(1.0)
    start_mock()
    step("   mock 已重启")
    mark = len(a.buf)
    a.expect("已重连".encode(), 40, start=mark)
    a.expect(rf"{tag}_one".encode(), 20, start=mark)  # tmux 重绘出之前的内容
    a.quiet(1.0)
    step("   自动重连成功，tmux 重绘出断线前的内容")
    mark = len(a.buf)
    a.send(f"echo {tag}_two\r")
    a.expect(rf"{tag}_two\r\n".encode(), start=mark)
    a.quiet(0.5)
    step("   重连后输入正常")

# ---------------------------------------------------------------- 3. 窗口大小
a.setsize(40, 120)
os.killpg(os.getpgid(a.p.pid), signal.SIGWINCH)
time.sleep(1.5)
a.quiet(0.5)
mark = len(a.buf)
a.send("stty size\r")
m = a.expect(rb"(\d+) (\d+)\r\n", start=mark)
step(f"3. 本地改为 40x120 → tmux 内 stty size = {m.group(1).decode()} {m.group(2).decode()}（tmux 状态栏占 1 行）")

# ---------------------------------------------------------------- 4. detach
a.quiet(0.5)
a.send(b"\x02d")
rc, restored, msgs = a.finish()
step(f"4. Ctrl-b d → aish 退出码={rc} 终端已恢复={restored} 提示={msgs[-1] if msgs else None}")

# ---------------------------------------------------------------- 5. 接回 + 接管
b = Pty([AISH, HOST])
b.expect(rb"\x1b\[\?1049h", 30)
b.expect(rf"{tag}_one".encode(), 15)
b.quiet(1.0)
step("5. 再次运行 → 接回同一会话，看到之前的内容")
c = Pty([AISH, HOST])
c.expect(rb"\x1b\[\?1049h", 30)
c.quiet(1.0)
rc, restored, msgs = b.finish()
step(f"   第二个客户端接入 → 第一个自动退出：退出码={rc} 终端已恢复={restored} 提示={msgs[-1] if msgs else None}")

# ---------------------------------------------------------------- 6. 会话结束
c.send("exit\r")
rc, restored, msgs = c.finish()
step(f"6. tmux 里 exit → 会话结束：退出码={rc} 终端已恢复={restored} 提示={msgs[-1] if msgs else None}")
