"""End-to-end acceptance checks for the sandbox: python -m sandbox.acceptance [--skip-rtb]

Logs: logs/acceptance/<timestamp>/ holds acceptance.log (console transcript), summary.json (one row per check)
and one sub-folder per check with the raw sandbox results it was judged on.
"""
import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

from .executor import IMAGES, ROOT, differential, run_cases, run_suite
from .logs import new_run, write_json
from .rtb_smoke import RTB, smoke

EXAMPLES = ROOT / "examples"
RUN = None  # this run's log folder, set by main()
results = []


def say(text):
    print(text, flush=True)
    with open(RUN / "acceptance.log", "a", encoding="utf-8") as f:
        f.write(text + "\n")


def check(name, fn):
    start = time.time()
    try:
        ok, detail = fn()
    except Exception as e:  # a crashing check is a failing check
        ok, detail = False, f"{type(e).__name__}: {e}"
    results.append({"check": name, "ok": ok, "seconds": round(time.time() - start, 1), "detail": detail})
    say(f"[{'PASS' if ok else 'FAIL'}] {name} ({time.time() - start:.1f}s)\n       {detail}")
    write_json(RUN / "summary.json", results)


def docker_out(image, *cmd):
    p = subprocess.run(["docker", "run", "--rm", "--network", "none", IMAGES[image], *cmd],
                       capture_output=True, text=True)
    return (p.stdout + p.stderr).strip()


def interpreters():
    got = {"py27": docker_out("py27", "python", "-c", "print 1/2, type('')"),
           "py3": docker_out("py3", "python", "-c", "print(1/2, type(''))"),
           "java17": docker_out("java17", "bash", "-c", "java -version 2>&1 | head -1; mvn -v | head -1")}
    ok = (got["py27"] == "0 <type 'str'>" and got["py3"] == "0.5 <class 'str'>"
          and 'version "17' in got["java17"] and "Apache Maven" in got["java17"])
    return ok, json.dumps(got)


def calc_cases():
    return [json.loads(l) for l in (EXAMPLES / "calc" / "cases.jsonl").read_text().splitlines() if l.strip()]


def verdicts(variant):
    vs = differential(EXAMPLES / "calc" / "py2", EXAMPLES / "calc" / variant, calc_cases(), per_case_timeout=2,
                      log_dir=RUN / f"differential-{variant}")
    return {v["case_id"]: v["equivalent"] for v in vs}


def naive_migration():
    want = {"avg-1": False, "avg-empty": True, "shout-1": True, "evens-1": False,
            "mutate-1": True, "counter-1": True, "spin-1": True}
    got = verdicts("py3_naive")
    return got == want, f"verdicts={got}"


def fixed_migration():
    got = verdicts("py3_fixed")
    return all(got.values()), f"verdicts={got}"


PROBES = [
    {"case_id": "global-1", "module": "probes", "callable": "touch_global"},
    {"case_id": "global-2", "module": "probes", "callable": "touch_global"},
    {"case_id": "file-1", "module": "probes", "callable": "write_cwd_file"},
    {"case_id": "file-2", "module": "probes", "callable": "write_cwd_file"},
    {"case_id": "net", "module": "probes", "callable": "net"},
    {"case_id": "ro-code", "module": "probes", "callable": "write_code_dir"},
    {"case_id": "fork", "module": "probes", "callable": "fork_many"},
    {"case_id": "after-fork", "module": "probes", "callable": "whoami"},
    {"case_id": "oom", "module": "probes", "callable": "hog_memory"},
    {"case_id": "after-oom", "module": "probes", "callable": "whoami"},
]


def isolation(image):
    def run():
        # small per-case memory cap: the OOM probe only needs to prove the cap exists, not fill a large one
        r = {x["case_id"]: x for x in run_cases(image, EXAMPLES / "probes", PROBES, per_case_timeout=10,
                                                case_mem_mb=128, log_dir=RUN / f"isolation-{image}")}
        def val(cid):
            v = r[cid].get("value") or {}
            return base64.b64decode(v["v"]).decode() if v.get("t") == "bytes" else v.get("v")  # py2 native str
        expect = {
            "fresh interpreter per case": val("global-1") == val("global-2") == "1",
            "fresh cwd per case": val("file-1") is False and val("file-2") is False,
            "no network": r["net"]["status"] == "exception",
            "code mounted read-only": r["ro-code"]["status"] == "exception",
            "pids limited": val("fork") == "limited",
            "recovers after fork storm": val("after-fork") == "1000",
            "memory limited (MemoryError)": (r["oom"].get("exception") or {}).get("type") == "MemoryError",
            "recovers after OOM": val("after-oom") == "1000",
        }
        bad = [k for k, v in expect.items() if not v]
        summary = {k: (x["status"], (x.get("value") or {}).get("v"), (x.get("exception") or {}).get("type"))
                   for k, x in r.items()}
        return not bad, f"failed={bad} results={summary}" if bad else f"all {len(expect)} properties hold"
    return run


def determinism():
    strip = lambda rs: [{k: v for k, v in x.items() if k != "duration_ms"} for x in rs]
    a, b = [strip(run_cases("py27", EXAMPLES / "calc" / "py2", calc_cases(), per_case_timeout=2,
                            log_dir=RUN / "determinism", log_name=f"run{i}")) for i in (1, 2)]
    return a == b, "two runs identical (excluding duration_ms)" if a == b else "runs differ"


def java_offline():
    r = run_suite("java17", ROOT / "images" / "java17" / "warmup", log_dir=RUN / "java-offline")
    outcomes = [t["outcome"] for t in r["tests"]]
    ok = r["returncode"] == 0 and outcomes == ["passed", "passed"]
    return ok, f"returncode={r['returncode']} tests={outcomes}" + ("" if ok else f"\n{r['log_tail']}")


# Regression set: projects covering plain pytest, coverage-wrapped runs, requirements/setup deps and the
# undeclared pytest-mock plugin. Broader benchmark coverage is measured by `python -m sandbox.rtb_smoke`.
RTB_REGRESSION = ["alastair_python-musicbrainzngs", "alingse_jsoncsv", "7ossam81_EvoloPy"]


def repotransbench():
    if not RTB.is_dir():
        return False, f"dataset not found at {RTB}"
    rows = [smoke(RTB / name, log_dir=RUN / "rtb" / name) for name in RTB_REGRESSION]
    lines = [f"{'ok ' if x['green'] else 'BAD'} {x['project']}: {x.get('counts')} {x['category'] or ''}" for x in rows]
    return all(x["green"] for x in rows), "\n       ".join(lines)


def main():
    global RUN
    RUN = new_run("acceptance")
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-rtb", action="store_true", help="skip the RepoTransBench regression projects")
    skip_rtb = ap.parse_args().skip_rtb
    check("images: real Py2.7, Py3.11, JDK17+Maven", interpreters)
    check("differential: naive Py3 migration flags hazards", naive_migration)
    check("differential: correct Py3 migration is equivalent", fixed_migration)
    check("isolation: py27 sandbox", isolation("py27"))
    check("isolation: py3 sandbox", isolation("py3"))
    check("determinism: repeated run is identical", determinism)
    check("suite mode: JUnit5+Mockito offline in java17", java_offline)
    if not skip_rtb:
        check("suite mode: RepoTransBench regression projects green in py3", repotransbench)
    passed = sum(r["ok"] for r in results)
    say(f"\n{passed}/{len(results)} checks passed\nlogs: {RUN}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
