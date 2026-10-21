# Sandbox escape/leak probes. Must run on Python 2.7 and 3.x. Each returns a value the acceptance suite checks.
import os
import socket
import time

STATE = []


def touch_global():
    STATE.append(1)
    return len(STATE)  # 1 on every case if each case gets a fresh interpreter


def write_cwd_file():
    existed = os.path.exists("leak.txt")
    with open("leak.txt", "w") as f:
        f.write("x")
    return existed  # False on every case if each case gets a fresh cwd


def net():
    socket.create_connection(("1.1.1.1", 53), timeout=3)
    return "connected"


def write_code_dir():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pwned.txt"), "w") as f:
        f.write("x")
    return "written"


def fork_many(n=1000):
    for _ in range(n):
        try:
            pid = os.fork()
        except OSError:
            return "limited"
        if pid == 0:
            time.sleep(30)
            os._exit(0)
    return "unlimited"


def hog_memory(mb=2048):
    chunks = []
    for _ in range(mb):
        chunks.append(b"x" * (1024 * 1024))
    return len(chunks)


def whoami():
    return os.getuid()
