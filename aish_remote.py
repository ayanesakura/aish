"""Remote pipe relay, sent as readable Python source to the WebShell shell."""

import base64
import contextlib
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty

MAX_PENDING = 1048576


def emit(tag, payload=b""):
    frame = b"\x1e" + tag + payload + b"\n"
    while frame:
        written = os.write(1, frame)
        frame = frame[written:]


def main():
    command, nonce, idle = sys.argv[1:]
    original = termios.tcgetattr(0)
    child = None
    finished = False
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        tty.setraw(0, termios.TCSANOW)
        child = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        os.set_blocking(child.stdin.fileno(), False)
        emit(b"R", nonce.encode())
        sources = {child.stdout.fileno(): b"O", child.stderr.fileno(): b"E"}
        incoming = bytearray()
        pending = bytearray()
        close_input = False
        destination = child.stdin.fileno()
        last_input = time.monotonic()
        while sources or child.poll() is None:
            readers = list(sources)
            if len(pending) < MAX_PENDING:
                readers.append(0)
            writable = [destination] if destination is not None and pending else []
            readable, writable, _ = select.select(readers, writable, [], 1)
            if 0 in readable:
                data = os.read(0, 65536)
                if not data:
                    return
                last_input = time.monotonic()
                incoming.extend(data)
                while b"\n" in incoming:
                    frame, _, remainder = incoming.partition(b"\n")
                    incoming[:] = remainder
                    if frame[:1] == b"D":
                        if not close_input and destination is not None:
                            pending.extend(base64.b64decode(frame[1:], validate=True))
                    elif frame == b"C":
                        close_input = True
                    elif frame == b"Q":
                        return
                    elif frame != b"H":
                        raise ValueError("invalid input frame")
                if len(incoming) > 131072:
                    raise ValueError("oversized input frame")
            if destination in writable:
                try:
                    written = os.write(destination, pending)
                    del pending[:written]
                except BlockingIOError:
                    pass
                except BrokenPipeError:
                    pending.clear()
                    close_input = True
            if destination is not None and close_input and not pending:
                child.stdin.close()
                destination = None
            for descriptor in list(sources):
                if descriptor in readable:
                    data = os.read(descriptor, 12288)
                    if data:
                        emit(sources[descriptor], base64.b64encode(data))
                    else:
                        del sources[descriptor]
            if 0 in readers and time.monotonic() - last_input > float(idle):
                return
        code = child.wait()
        emit(b"X", str(code if code >= 0 else 128 - code).encode())
        finished = True
    finally:
        if child is not None:
            if not finished:
                for signum in (signal.SIGHUP, signal.SIGKILL):
                    try:
                        os.killpg(child.pid, signum)
                    except ProcessLookupError:
                        break
                    try:
                        child.wait(timeout=2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
        with contextlib.suppress(termios.error):
            termios.tcsetattr(0, termios.TCSANOW, original)


if __name__ == "__main__":
    main()
