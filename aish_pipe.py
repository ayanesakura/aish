"""Binary-safe stdio transport over the platform's text-only terminal input."""

import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import sys

CHUNK = 12288
MAX_FRAME = 131072
IDLE_SECONDS = 45


async def read_stdin():
    loop = asyncio.get_running_loop()
    available = loop.create_future()

    def readable():
        if not available.done():
            available.set_result(None)

    loop.add_reader(0, readable)
    try:
        await available
        return os.read(0, CHUNK)
    finally:
        loop.remove_reader(0)


async def write_fd(descriptor, data):
    loop = asyncio.get_running_loop()
    while data:
        try:
            count = os.write(descriptor, data)
            data = data[count:]
        except BlockingIOError:
            writable = loop.create_future()

            def ready():
                if not writable.done():
                    writable.set_result(None)

            loop.add_writer(descriptor, ready)
            try:
                await writable
            finally:
                loop.remove_writer(descriptor)


async def run_pipe(ws, command, startup_timeout):
    from websockets.exceptions import ConnectionClosed

    nonce = secrets.token_hex(16).encode()
    marker = b"\x1eR" + nonce + b"\n"
    helper = Path(__file__).with_name("aish_remote.py").read_text()
    bootstrap_marker = b"\x1eB" + nonce + b"\n"
    loader = (
        "import json,signal,sys; signal.alarm(45); "
        f"print({bootstrap_marker[:-1].decode()!r}, flush=True); "
        "payload=json.loads(sys.stdin.buffer.readline()); signal.alarm(0); "
        f"sys.argv=['aish-remote',payload['command'],{nonce.decode()!r},{str(IDLE_SECONDS)!r}]; "
        "exec(compile(payload['source'],'aish_remote.py','exec'))"
    )
    bootstrap = " stty raw -echo; exec python3 -u -c " + shlex.quote(loader) + "\n"
    buffer = bytearray()
    ready = asyncio.Event()
    bootstrapped = asyncio.Event()
    first_output = asyncio.Event()
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    tasks = []
    blocking = {}

    async def receive():
        async for message in ws:
            first_output.set()
            buffer.extend(message.encode() if isinstance(message, str) else message)
            if not bootstrapped.is_set():
                position = buffer.find(bootstrap_marker)
                if position < 0:
                    del buffer[:-len(bootstrap_marker)]
                    continue
                del buffer[:position + len(bootstrap_marker)]
                bootstrapped.set()
            if not ready.is_set():
                position = buffer.find(marker)
                if position < 0:
                    del buffer[:-len(marker)]
                    continue
                del buffer[:position + len(marker)]
                ready.set()
            while b"\n" in buffer:
                frame, _, remainder = buffer.partition(b"\n")
                buffer[:] = remainder
                if len(frame) > MAX_FRAME or frame[:1] != b"\x1e":
                    raise ValueError("invalid pipe frame")
                tag, payload = frame[1:2], frame[2:]
                if tag in (b"O", b"E"):
                    await write_fd(1 if tag == b"O" else 2, base64.b64decode(payload, validate=True))
                elif tag == b"X":
                    code = int(payload)
                    if not 0 <= code <= 255:
                        raise ValueError("invalid pipe exit status")
                    return code
                else:
                    raise ValueError("unknown pipe frame")
            if len(buffer) > MAX_FRAME:
                raise ValueError("oversized pipe frame")
        raise ConnectionError("WebShell closed before command exit")

    async def transmit():
        await ready.wait()
        while True:
            data = await read_stdin()
            if not data:
                await ws.send("C\n")
                return
            await ws.send("D" + base64.b64encode(data).decode() + "\n")

    async def keepalive():
        await ready.wait()
        while True:
            await asyncio.sleep(10)
            await ws.send("H\n")

    async def start():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(first_output.wait(), 2)
        await ws.send(bootstrap)
        await asyncio.wait_for(bootstrapped.wait(), startup_timeout)
        payload = json.dumps({"source": helper, "command": command}) + "\n"
        for offset in range(0, len(payload), CHUNK):
            await ws.send(payload[offset:offset + CHUNK])
        await asyncio.wait_for(ready.wait(), startup_timeout)

    try:
        for descriptor in (1, 2):
            blocking[descriptor] = os.get_blocking(descriptor)
            os.set_blocking(descriptor, False)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, main_task.cancel)
        receiver = asyncio.create_task(receive())
        sender = asyncio.create_task(transmit())
        beat = asyncio.create_task(keepalive())
        startup = asyncio.create_task(start())
        tasks = [receiver, sender, beat, startup]
        active = set(tasks)
        while True:
            finished, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            if receiver in finished:
                return receiver.result()
            for task in finished:
                task.result()
    except asyncio.CancelledError:
        return 130
    except (ConnectionClosed, ConnectionError, OSError, ValueError, asyncio.TimeoutError) as error:
        message = "aish: pipe transport failed (" + type(error).__name__ + "); no automatic replay\n"
        with contextlib.suppress(OSError):
            await write_fd(2, message.encode())
        return 255
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if ready.is_set():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send("Q\n"), 1)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.close(), 3)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        for descriptor, was_blocking in blocking.items():
            os.set_blocking(descriptor, was_blocking)
