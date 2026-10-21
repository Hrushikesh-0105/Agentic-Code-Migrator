# Runs INSIDE the sandbox container. Must stay compatible with Python 2.7 AND 3.x.
# stdin : one JSON test case per line
# stdout: one line per case: SENTINEL + JSON result
# Case options besides module/callable/args/kwargs/init: "attr": true reads the attribute (e.g. a property) instead
# of calling it; "trace": {"file": "pkg/mod.py"} records the lines of that file the case executed ("lines").
from __future__ import print_function

import base64
import importlib
import inspect
import itertools
import json
import math
import os
import random
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SENTINEL = "__HARNESS_RESULT__"
MAX_ITEMS = 10000
PY2 = sys.version_info[0] == 2
if PY2:
    from StringIO import StringIO  # accepts both str and unicode
    TEXT, BYTES, INTS = unicode, str, (int, long)  # noqa: F821
else:
    from io import StringIO
    TEXT, BYTES, INTS = str, bytes, (int,)


# ---------- value encoding: keeps Py2/Py3 type differences visible ----------
def encode(v, depth=0):
    if depth > 40:
        return {"t": "repr", "v": repr(v)}
    if v is None:
        return {"t": "none"}
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, INTS):  # int and py2 long both -> "int"; string avoids JSON bigint loss
        return {"t": "int", "v": str(v)}
    if isinstance(v, float):
        return {"t": "float", "v": repr(v) if (math.isnan(v) or math.isinf(v)) else v}
    if isinstance(v, TEXT):
        return {"t": "text", "v": v}
    if isinstance(v, (BYTES, bytearray)):  # NOTE: a py2 native `str` lands here
        return {"t": "bytes", "v": base64.b64encode(bytes(v)).decode("ascii")}
    d = depth + 1
    if isinstance(v, list):
        return {"t": "list", "v": [encode(x, d) for x in v]}
    if isinstance(v, tuple):
        return {"t": "tuple", "v": [encode(x, d) for x in v]}
    if isinstance(v, dict):
        return {"t": "dict", "v": [[encode(k, d), encode(x, d)] for k, x in v.items()]}
    if isinstance(v, (set, frozenset)):
        return {"t": "set", "v": [encode(x, d) for x in v]}
    if hasattr(v, "__iter__") and not hasattr(v, "__dict__"):
        # py3 map/filter/zip/range/dict views vs py2 lists: keep the difference visible
        items = [encode(x, d) for x in itertools.islice(v, MAX_ITEMS)]
        return {"t": "iterator", "kind": type(v).__name__, "v": items}
    if hasattr(v, "__dict__"):
        cls = getattr(v, "__class__", type(v)).__name__  # py2 old-style classes report type 'instance'
        # attribute names are identifiers, not data: normalise py2 byte-str names to text
        attrs = dict((k.decode("utf-8") if isinstance(k, BYTES) else k, x) for k, x in vars(v).items())
        return {"t": "object", "class": cls, "v": encode(attrs, d)}
    return {"t": "repr", "v": repr(v)}


def native(s):
    """JSON text -> the interpreter's native `str` (bytes on py2, text on py3)."""
    return s.encode("utf-8") if PY2 else s


def decode(v):
    """Tagged JSON -> value. Plain JSON is accepted too; plain strings become the NATIVE str."""
    if isinstance(v, dict) and "t" in v:
        t, x = v["t"], v.get("v")
        if t == "none":
            return None
        if t == "bool":
            return bool(x)
        if t == "int":
            return int(x)
        if t == "float":
            return float(x)
        if t == "text":
            return TEXT(x)
        if t == "bytes":
            return base64.b64decode(x)
        if t == "str":
            return native(x)
        if t == "list":
            return [decode(i) for i in x]
        if t == "tuple":
            return tuple(decode(i) for i in x)
        if t == "set":
            return set(decode(i) for i in x)
        if t == "dict":
            return dict((decode(k), decode(i)) for k, i in x)
        raise ValueError("cannot decode tag %r" % t)
    if isinstance(v, list):
        return [decode(i) for i in v]
    if isinstance(v, dict):
        return dict((native(k), decode(i)) for k, i in v.items())
    if isinstance(v, TEXT):
        return native(v)
    return v


# ---------- single case execution ----------
class CaseTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise CaseTimeout()


def _text(x):
    try:
        return TEXT(x)
    except Exception:
        return repr(x)


def _as_text(s):
    return s.decode("utf-8", "replace") if isinstance(s, BYTES) else s


def _captured(buf):
    try:
        return _as_text(buf.getvalue())
    except UnicodeDecodeError:  # py2 StringIO mixing non-ascii str and unicode
        return repr(buf.buflist)


def resolve(module, attr, init):
    obj = importlib.import_module(module)
    parts = attr.split(".")
    if init is not None:  # "Class.method" called on a fresh instance
        cls = obj
        for part in parts[:-1]:
            cls = getattr(cls, part)
        inst = cls(*[decode(a) for a in init.get("args", [])],
                   **dict((str(k), decode(v)) for k, v in init.get("kwargs", {}).items()))
        return getattr(inst, parts[-1]), inst
    for part in parts:
        obj = getattr(obj, part)
    return obj, None


def _tracer(path, lines):
    def tracer(frame, event, arg):
        if frame.f_code.co_filename.replace("\\", "/").endswith(path):
            if event == "line":
                lines.add(frame.f_lineno)
            return tracer
        return None
    return tracer


def run_one(case, timeout):
    random.seed(case.get("seed", 0))
    out, err = StringIO(), StringIO()
    res = {"case_id": case.get("case_id"), "python": sys.version.split()[0]}
    args = [decode(a) for a in case.get("args", [])]
    kwargs = dict((str(k), decode(v)) for k, v in case.get("kwargs", {}).items())
    inst, lines = None, set()
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(math.ceil(timeout)))
    sys.stdout, sys.stderr = out, err
    if case.get("trace"):
        sys.settrace(_tracer(case["trace"]["file"], lines))
    start = time.time()
    try:
        target, inst = resolve(case["module"], case["callable"], case.get("init"))
        value = target if case.get("attr") else target(*args, **kwargs)
        res.update(status="ok", value=encode(value))
    except CaseTimeout:
        res.update(status="timeout")
    except BaseException as e:  # SystemExit etc. are observable behaviour too
        res.update(status="exception", exception={
            "type": e.__class__.__name__, "message": _text(e), "module": getattr(e.__class__, "__module__", ""),
            "hierarchy": [c.__name__ for c in inspect.getmro(e.__class__)],
            "traceback": _as_text(traceback.format_exc())})
    finally:
        sys.settrace(None)
        signal.alarm(0)
        sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    if case.get("trace"):
        res["lines"] = sorted(lines)
    res["duration_ms"] = int((time.time() - start) * 1000)
    res["args_after"] = [encode(a) for a in args]  # in-place mutation of arguments
    res["self_after"] = encode(inst) if inst is not None else None
    res["stdout"], res["stderr"] = _captured(out), _captured(err)
    return res


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:  # group already gone
        pass


def _opt(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except (IOError, OSError):
        return ""


def main():
    timeout = float(_opt("--timeout", 10.0))
    mem_mb = int(_opt("--mem-mb", 0))
    sys.path.insert(0, os.environ.get("ACM_CODE_DIR", "/code"))
    if "--one" in sys.argv:
        if mem_mb:  # deterministic MemoryError instead of cgroup thrashing / OOM-kill
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb << 20, mem_mb << 20))
        res = run_one(json.loads(sys.stdin.read()), timeout)
        with open(_opt("--result", None), "w") as f:
            f.write(json.dumps(res))
        return
    for line in sys.stdin:
        if not line.strip():
            continue
        case = json.loads(line)
        # Results/stderr go to files OUTSIDE the case's cwd, so processes the case forks can't hold a pipe open
        box, cwd = tempfile.mkdtemp(prefix="box-"), tempfile.mkdtemp(prefix="case-")
        result_path, log_path = os.path.join(box, "result.json"), os.path.join(box, "fd.log")
        start = time.time()
        with open(log_path, "w") as log:
            # fresh interpreter + fresh cwd per case: module globals and files cannot leak between cases
            child = subprocess.Popen(
                [sys.executable, __file__, "--one", "--timeout", str(timeout), "--mem-mb", str(mem_mb),
                 "--result", result_path],
                stdin=subprocess.PIPE, stdout=log, stderr=log, cwd=cwd, preexec_fn=os.setsid)
            killer = threading.Timer(timeout + 5, _kill_group, [child.pid])  # backstop if code swallows the alarm
            killer.start()
            child.communicate(line if PY2 else line.encode("utf-8"))
            killer.cancel()
            _kill_group(child.pid)  # kill anything the case forked, so it can't starve later cases
        try:
            res = json.loads(_read(result_path))
        except ValueError:  # segfault, OOM-kill, os._exit(), hard hang...
            hung = time.time() - start >= timeout
            res = {"case_id": case.get("case_id"), "status": "timeout" if hung else "crash",
                   "returncode": child.returncode, "stderr": _read(log_path)[-4000:]}
        shutil.rmtree(box, ignore_errors=True)
        shutil.rmtree(cwd, ignore_errors=True)  # /tmp is a small tmpfs shared by all cases
        print(SENTINEL + json.dumps(res))
        sys.stdout.flush()


if __name__ == "__main__":
    main()
