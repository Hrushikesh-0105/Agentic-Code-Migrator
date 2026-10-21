"""Build the sandbox images: python -m sandbox.build_images [py27 py3 java17 analysis]

Build output is shown live and saved to logs/build/<timestamp>-base/<image>.log, with a summary.json.
"""
import subprocess
import sys
import time

from .executor import IMAGES, ROOT
from .logs import new_run, rel, write_json

# build context per image; the Layer 0 analysis image lives with the analysis package
CONTEXTS = {**{name: ROOT / "images" / name for name in IMAGES}, "analysis": ROOT.parent / "analysis" / "image"}


def build(name, log_path):
    """`docker build` one image, teeing its output to the terminal and `log_path`; returns the exit code."""
    with open(log_path, "w", encoding="utf-8") as log:
        p = subprocess.Popen(["docker", "build", "--progress=plain", "-t", IMAGES[name], str(CONTEXTS[name])],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace")
        for line in p.stdout:
            sys.stdout.write(line)
            log.write(line)
        return p.wait()


def main(names):
    run, rows = new_run("build", "base"), []
    for name in names or IMAGES:
        print(f"==> building {IMAGES[name]}", flush=True)
        start, log = time.time(), run / f"{name}.log"
        code = build(name, log)
        rows.append({"image": IMAGES[name], "ok": code == 0, "returncode": code,
                     "seconds": round(time.time() - start, 1), "log": rel(log)})
        write_json(run / "summary.json", rows)
        if code != 0:
            sys.exit(f"build of {IMAGES[name]} failed (exit {code}); log: {log}")
    print(f"logs: {run}")


if __name__ == "__main__":
    main(sys.argv[1:])
