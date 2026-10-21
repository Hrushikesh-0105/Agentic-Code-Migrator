"""Persistent run logs under <repo>/logs (override with ACM_LOG_DIR). Layout: see logs/README.md."""
import datetime
import json
import os
import re
from pathlib import Path

LOG_ROOT = Path(os.environ.get("ACM_LOG_DIR") or Path(__file__).resolve().parent.parent / "logs")


def _safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name))


def new_run(kind, label=None):
    """Create and return a fresh directory logs/<kind>/<YYYYmmdd-HHMMSS>[-label]/ for one run."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = LOG_ROOT / kind / (f"{stamp}-{_safe(label)}" if label else stamp)
    d, n = base, 1
    while True:  # two runs in the same second must not overwrite each other
        try:
            d.mkdir(parents=True)
            return d
        except FileExistsError:
            n += 1
            d = base.with_name(f"{base.name}-{n}")


def run_dir(log_dir, kind, label=None):
    """`log_dir` if the caller nests this run inside its own (e.g. rtb_smoke), else a fresh top-level run."""
    if log_dir is None:
        return new_run(kind, label)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    return Path(log_dir)


def rel(path):
    """Path as recorded in summaries: relative to the log root, so logs stay valid when the repo moves."""
    try:
        return Path(path).resolve().relative_to(LOG_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


def write_text(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")
    return Path(path)


def write_json(path, obj):
    return write_text(path, json.dumps(obj, indent=2, default=str) + "\n")


def write_jsonl(path, rows):
    return write_text(path, "".join(json.dumps(r, default=str) + "\n" for r in rows))
