#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14"]
# ///
"""本地 mock：模拟 AI Studio 的 webshellUrl / opsws / opsresize，后端是本机 bash pty。
二进制帧故意在 UTF-8 多字节中间拆包。仅用于本地调试 aish。"""
import asyncio, fcntl, json, os, pty, signal, struct, sys, termios, urllib.parse
from http import HTTPStatus
from websockets.asyncio.server import serve

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18765
COOKIE = "mockcookie123456"
TOKEN = "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JleHAiOjQxMDI0NDQ4MDB9.mocksig"
SESSIONS = {}  # session -> master fd
LOG = open(sys.argv[2] if len(sys.argv) > 2 else "/dev/stderr", "a", buffering=1)
counter = [1000000]

def cookie_ok(headers):
    return f"ais_token={COOKIE}" in (headers.get("Cookie") or "")

def setsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

async def process_request(conn, req):
    path, _, query = req.path.partition("?")
    q = dict(urllib.parse.parse_qsl(query))
    if path.endswith("/runs") or path.endswith("/instances"):
        if not cookie_ok(req.headers):
            return conn.respond(HTTPStatus.UNAUTHORIZED, '{"code":401,"msg":"unauthorized"}')
        # 数据来自 MOCK_RUNS_FILE（测试中途可修改，模拟换 run）
        try:
            runs = json.load(open(os.environ["MOCK_RUNS_FILE"]))
        except (KeyError, OSError, ValueError):
            return conn.respond(HTTPStatus.NOT_FOUND, "nf")
        if path.endswith("/runs"):
            data = {"data": [{k: v for k, v in r.items() if k != "pods"} for r in runs],
                    "info": {"current": 1, "pageSize": 20, "total": len(runs)}}
        else:
            rid = path.split("/runs/")[1].split("/")[0]
            data = next((r.get("pods", []) for r in runs if str(r["id"]) == rid), [])
        return conn.respond(HTTPStatus.OK, json.dumps({"code": 200, "msg": "OK", "data": data}))
    if path.endswith("/webshellUrl"):
        LOG.write(f"webshellUrl run={path.split('/runs/')[1].split('/')[0]} instance={q.get('instance')}\n")
        if not cookie_ok(req.headers):
            return conn.respond(HTTPStatus.UNAUTHORIZED, '{"code":401,"msg":"unauthorized"}')
        counter[0] += 1
        url = (f"http://127.0.0.1:{PORT}/api/webshell/v1/webshell/terminal?cluster=mockc&namespace=ns"
               f"&instance={q.get('instance')}&container={q.get('container')}&token={TOKEN}&session={counter[0]}")
        return conn.respond(HTTPStatus.OK, json.dumps({"code": 200, "msg": "OK", "data": {"url": url}}))
    if path.endswith("/opsresize"):
        fd = SESSIONS.get(q.get("session"))
        LOG.write(f"resize {q}\n")
        if fd is None:
            return conn.respond(HTTPStatus.OK, '{"code":404}')
        setsize(fd, int(q["cols"]), int(q["rows"]))
        return conn.respond(HTTPStatus.OK, '{"code":200,"msg":"OK"}')
    if path.endswith("/opsws"):
        if not cookie_ok(req.headers):
            return conn.respond(HTTPStatus.UNAUTHORIZED, "401")
        return None
    return conn.respond(HTTPStatus.NOT_FOUND, "nf")

async def handler(ws):
    path, _, query = ws.request.path.partition("?")
    q = dict(urllib.parse.parse_qsl(query))
    LOG.write(f"ws open session={q.get('session')} cols={q.get('cols')} rows={q.get('rows')}\n")
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["PS1"] = "mock$ "
        os.environ["TERM"] = "xterm"
        os.environ["SHELL"] = "/bin/bash"  # 和 pod 一致（非交互 bash 会去掉 PS1，用来复现/验证）
        os.environ["LANG"] = os.environ["LC_ALL"] = "en_US.UTF-8"
        os.environ.pop("TMUX", None)
        if os.environ.get("MOCK_HOME"):  # 测试「用户有自己的 ~/.tmux.conf」分支时用临时 HOME
            os.environ["HOME"] = os.environ["MOCK_HOME"]
        # 独立的 tmux socket 目录，和本机平时用的 tmux 隔离
        os.environ.setdefault("TMUX_TMPDIR", "/tmp/aish-mock-tmux")
        os.environ["BASH_SILENCE_DEPRECATION_WARNING"] = "1"
        os.execv("/bin/bash", ["bash", "--norc", "--noprofile", "-i"])
    setsize(fd, int(q.get("cols", 80)), int(q.get("rows", 24)))
    os.set_blocking(fd, False)
    SESSIONS[q.get("session")] = fd
    loop = asyncio.get_running_loop()
    outq = asyncio.Queue()
    def on_out():
        try:
            d = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            d = b""
        outq.put_nowait(d)
        if not d:
            loop.remove_reader(fd)
    loop.add_reader(fd, on_out)
    async def pump_out():
        n = 0
        while True:
            d = await outq.get()
            if not d:
                break
            n += 1
            # 交替发二进制/文本帧；二进制时在中间拆包（可能拆到 UTF-8 中间）
            if n % 2 and len(d) > 1:
                mid = len(d) // 2
                await ws.send(d[:mid]); await ws.send(d[mid:])
            else:
                await ws.send(d.decode("utf-8", "replace"))
        if os.environ.get("MOCK_KEEP_OPEN"):  # 模拟真实网关：shell 退出后不关 WS
            await asyncio.Future()
        await ws.close(1000, "")
    async def pump_in():
        hb = 0
        async for m in ws:
            if m == "":
                hb += 1; LOG.write(f"heartbeat {hb}\n"); continue
            if os.environ.get("MOCK_LOG_INPUT"):
                LOG.write(f"input {m!r}\n")
            data = m.encode() if isinstance(m, str) else m
            while data:
                try:
                    written = os.write(fd, data)
                    data = data[written:]
                except BlockingIOError:
                    writable = loop.create_future()
                    def ready():
                        if not writable.done(): writable.set_result(None)
                    loop.add_writer(fd, ready)
                    try:
                        await writable
                    finally:
                        loop.remove_writer(fd)
    t1 = asyncio.create_task(pump_out()); t2 = asyncio.create_task(pump_in())
    await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in (t1, t2): t.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)
    try: loop.remove_reader(fd)
    except Exception: pass
    try: os.kill(pid, signal.SIGHUP)  # 本机 mock bash
    except ProcessLookupError: pass
    try: os.close(fd)
    except OSError: pass
    reaped = asyncio.create_task(asyncio.to_thread(os.waitpid, pid, 0))
    try:
        await asyncio.wait_for(asyncio.shield(reaped), 2)
    except asyncio.TimeoutError:
        try: os.kill(pid, signal.SIGKILL)
        except ProcessLookupError: pass
        await reaped
    except ChildProcessError:
        pass
    SESSIONS.pop(q.get("session"), None)
    LOG.write(f"ws closed session={q.get('session')}\n")

async def main():
    async with serve(handler, "127.0.0.1", PORT, process_request=process_request):
        LOG.write(f"mock listening {PORT}\n")
        await asyncio.Future()

asyncio.run(main())
