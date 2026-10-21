"""Measure how many RepoTransBench Python source projects run green in the sandbox.

python -m sandbox.rtb_smoke [N] [--out report.json]
Each project: per-project image (deps baked in, network on) -> its own run_tests.sh (network off) -> JUnit XML.
Logs: logs/rtb_smoke/<timestamp>/summary.json, plus projects/<project>/ with each project's suite output.
"""
import argparse
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from .executor import ROOT, build_log_path, build_project_image, run_suite
from .logs import new_run, rel, write_json

RTB = ROOT.parent / "benchmarks" / "repotransbench" / "source_projects" / "Python"
# The benchmark's run_tests.sh is used unmodified: its `pip install` lines become no-ops (deps are baked into
# the per-project image) and pytest is told to emit JUnit XML.
RTB_ENV = {"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PYTEST_ADDOPTS": "-p no:cacheprovider --junitxml=/work/.acm/junit.xml"}


def _category(log, proj):
    """Best-effort reason for a non-green project, for triage."""
    if re.search(r"\b(venv|virtualenv)\b", (proj / "run_tests.sh").read_text(encoding="utf-8", errors="replace")):
        return "env: script creates its own venv"
    for pattern, cat in [(r"requires a different Python", "env: python version"),
                         (r"No matching distribution found", "env: script installs at test time"),
                         (r"(poetry|tox|nox|make): command not found", "env: build tool missing"),
                         (r"dependency install failed", "env: dependency install failed"),
                         (r"No module named '([^']+)'", "env: missing dependency"),
                         (r"fixture '[^']+' not found", "env: missing pytest plugin"),
                         (r"(Temporary failure in name resolution|Network is unreachable|ConnectionError)",
                          "test needs network")]:
        if re.search(pattern, log):
            return cat
    return "tests fail"


def snapshot_date(proj):
    """When RepoTransBench validated this project; dependencies are resolved as of then."""
    ts = json.loads((proj / "test_summary.json").read_text(encoding="utf-8"))["timestamp"]
    return ts.split(".")[0] + "Z"


def _build_log(image=None, error=None):
    if error:
        m = re.search(r"\(log: ([^)]+)\)", error)
        return rel(m.group(1)) if m else None
    path = build_log_path(image)
    return rel(path) if path.exists() else None  # images built before logging existed have no log


def smoke(proj, log_dir=None):
    """Build `proj`'s image and run its suite; `log_dir` receives the suite logs (default: logs/suite/...)."""
    try:
        image = build_project_image("py3", proj, exclude_newer=snapshot_date(proj))
    except RuntimeError as e:
        detail = re.sub(r" \(log: [^)]+\)", "", str(e))  # the path goes in build_log, relative to logs/
        return {"project": proj.name, "green": False, "category": _category(str(e), proj), "detail": detail[-400:],
                "build_log": _build_log(error=str(e))}
    r = run_suite(image, proj, timeout=600, command="bash run_tests.sh", env=RTB_ENV, log_dir=log_dir)
    counts = {k: v for k, v in r["summary"].items() if v}
    green = counts.get("passed", 0) > 0 and not counts.get("failure") and not counts.get("error")
    return {"project": proj.name, "green": green, "counts": counts, "returncode": r["returncode"],
            "category": None if green else _category(r["log_tail"], proj),
            "detail": None if green else r["log_tail"][-400:],
            "image": image, "build_log": _build_log(image), "suite_log": rel(r["log_dir"])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int, nargs="?", default=20)
    ap.add_argument("--out", help="also copy the report here (it is always written to the run's log folder)")
    a = ap.parse_args()
    run, rows = new_run("rtb_smoke"), []
    for proj in sorted(RTB.iterdir())[:a.n]:
        rows.append(smoke(proj, log_dir=run / "projects" / proj.name))
        x = rows[-1]
        print(f"{'green' if x['green'] else 'RED  '} {x['project']:45} {x.get('counts') or ''} {x['category'] or ''}",
              flush=True)
        write_json(run / "summary.json", rows)  # rewritten per project, so an interrupted run keeps its progress
    if a.out:
        shutil.copy(run / "summary.json", a.out)
    green = sum(x["green"] for x in rows)
    print(f"\n{green}/{len(rows)} green; non-green by cause: {dict(Counter(x['category'] for x in rows if not x['green']))}")
    print(f"report: {run / 'summary.json'}")


if __name__ == "__main__":
    main()
