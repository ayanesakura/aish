"""用 pty 驱动 aish 交互模式做验收。
用法: python3 pty_test.py full|dashdash <expect-for-dashdash> -- <aish 及参数...>
full:      等提示符 -> echo ok -> stty size -> 改窗口 -> stty size -> TMOUT 保险 -> ~. -> 核对终端属性
dashdash:  等预期输出 -> 输入 exit -> 等 aish 退出
"""
import fcntl, os, re, select, struct, subprocess, sys, termios, time

mode = sys.argv[1]
expect_dd = sys.argv[2]
cmd = sys.argv[sys.argv.index("--") + 1:]

master, slave = os.openpty()
def setsize(rows, cols):
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
setsize(30, 100)
SD = os.path.dirname(os.path.abspath(__file__))
F1, F2 = os.path.join(SD, "stty_before"), os.path.join(SD, "stty_after")
for f in (F1, F2):
    if os.path.exists(f): os.remove(f)
# 用 sh 包一层（sh 是会话首进程，保持 tty 存活），在 aish 前后各记录一次 stty -g
wrapped = ["/bin/sh", "-c", f'stty -g > {F1}; "$@"; rc=$?; stty -g > {F2}; echo "AISH_RC=$rc"; sleep 0.3', "sh", *cmd]
proc = subprocess.Popen(wrapped, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
buf = bytearray()
t0 = time.time()

def pump(timeout):
    r, _, _ = select.select([master], [], [], timeout)
    if r:
        try:
            d = os.read(master, 65536)
        except OSError:
            return False
        buf.extend(d)
        return True
    return False

def wait_for(pat, timeout=15.0, start=0):
    end = time.time() + timeout
    rx = re.compile(pat)
    while time.time() < end:
        m = rx.search(buf, start)
        if m:
            return m
        pump(0.2)
    send("\x15exit\r")  # 失败路径也要让远端 shell 退出，不留游离 shell
    end2 = time.time() + 8
    while proc.poll() is None and time.time() < end2:
        pump(0.1)
    raise SystemExit(f"TIMEOUT waiting for {pat!r}（已发送 exit）\n--- transcript ---\n{show(buf)}")

def quiet(sec=1.5, maxwait=15):
    end = time.time() + maxwait
    last = time.time()
    while time.time() < end and time.time() - last < sec:
        if pump(0.1):
            last = time.time()

def send(s):
    os.write(master, s.encode())

def show(b):
    s = bytes(b).decode("utf-8", "replace")
    s = re.sub(r"\x1b\[\?2004[hl]", "", s)  # 去掉 bracketed-paste 开关，便于阅读
    return s.replace("\x1b", "\\e").replace("\r\n", "\n").replace("\r", "")

def step(name):
    print(f"[{time.time()-t0:5.1f}s] {name}", flush=True)

if mode == "full":
    wait_for(rb"\$ ", timeout=20); quiet(2.0)
    step("提示符出现: " + repr(show(buf).strip().splitlines()[-1] if buf.strip() else ""))
    mark = len(buf); send("echo ok\r")
    wait_for(rb"echo ok\r\n(?:\x1b\[\?2004l)?\r?ok\r\n", start=mark); quiet(0.5)
    step("echo ok -> 回显+结果 OK")
    mark = len(buf); send("stty size\r")
    m = wait_for(rb"\r\n(?:\x1b\[\?2004l)?\r?(\d+ \d+)\r\n", start=mark); quiet(0.5)
    step(f"本地窗口 30x100(rows x cols) -> 远端 stty size = {m.group(1).decode()}")
    setsize(40, 120)
    time.sleep(2.0); quiet(0.5)
    mark = len(buf); send("stty size\r")
    m = wait_for(rb"\r\n(?:\x1b\[\?2004l)?\r?(\d+ \d+)\r\n", start=mark); quiet(0.5)
    step(f"本地窗口改为 40x120 -> 远端 stty size = {m.group(1).decode()}")
    if expect_dd == "exit":   # 真实 pod：以 exit 结束（网关不回收断开的 shell）
        send("exit\r"); step("发送 exit")
    else:                     # mock：测 ~.
        mark = len(buf); send("~."); step("发送 ~.")
elif mode == "hold":
    quiet(2.0); step(f"保持连接 {expect_dd}s"); time.sleep(float(expect_dd)); quiet(0.3)
    send("~."); step("发送 ~.")
elif mode == "sigterm":
    quiet(2.0)
    pid = subprocess.run(["pgrep", "-f", "python.*/aish"], capture_output=True, text=True).stdout.split()[0]
    os.kill(int(pid), 15); step(f"向本地 aish(pid {pid}) 发送 SIGTERM")
elif mode == "drop":
    quiet(2.0)
    subprocess.run(["pkill", "-f", "mock_server.py"]); step("杀掉本地 mock 服务器，模拟意外断线")
elif mode == "dashdash":
    m = wait_for(expect_dd.encode(), timeout=20)
    quiet(1.0)
    step(f"看到 -- 命令的输出: {m.group(0).decode()!r}")
    send("exit\r"); step("发送 exit")
end = time.time() + 10
seen = rc_seen = None
while proc.poll() is None and time.time() < end:
    pump(0.05)  # 边读边等：会话首进程退出时内核要排空 tty 输出
    if seen is None and b"[aish]" in buf: seen = time.time(); step("出现 [aish] 提示")
    if rc_seen is None and b"AISH_RC=" in buf: rc_seen = time.time(); step("aish 进程已退出（sh 打印 AISH_RC）")
if proc.poll() is None:
    proc.terminate()
    while proc.poll() is None:
        pump(0.1)
quiet(0.3, 2)
m = re.search(rb"AISH_RC=(\d+)", buf)
rc = m.group(1).decode() if m else "?"
def norm(path):  # 屏蔽 macOS 内核在切回规范模式时自动置位的 PENDIN(0x20000000)
    out = []
    for kv in open(path).read().strip().split(":"):
        if kv.startswith("lflag="):
            kv = "lflag=%x" % (int(kv[6:], 16) & ~0x20000000)
        out.append(kv)
    return out
b, a = norm(F1), norm(F2)
raw_diff = [(x, y) for x, y in zip(open(F1).read().strip().split(":"), open(F2).read().strip().split(":")) if x != y]
step(f"aish 退出码 = {rc}；本地终端属性已恢复(stty -g 前后一致，屏蔽 PENDIN) = {a == b}；原始差异 = {raw_diff}")
print("--- transcript ---")
print(show(buf))
