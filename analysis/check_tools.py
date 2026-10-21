"""Verify the Layer 0 static-analysis toolchain, then run it on a sample repo.

python -m analysis.check_tools [--repo PATH] [--java PATH] [--skip-sample]

1. verify: every tool runs on a built-in fixture (Python 3 + Python 2 + Java) and must produce a known answer.
2. sample: every tool runs on a repo (default: RepoTransBench alingse_jsoncsv with its Java target).
Logs: logs/analysis/<timestamp>-check/{verify/, sample-<repo>/} with each tool's raw JSON, plus summary.json.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from sandbox.executor import IMAGES, _docker_run, _hardening
from sandbox.logs import new_run, rel, write_json, write_text

ROOT = Path(__file__).resolve().parent
RTB = ROOT.parent / "benchmarks" / "repotransbench"
DEFAULT_SAMPLE = "alingse_jsoncsv"


def run_tools(out_dir, repo=None, java=None, verify=False, timeout=1800):
    """Run analysis/tools/run_tools.py in a network-less acm-sandbox/analysis container; repo mounted read-only."""
    out_dir.mkdir(parents=True, exist_ok=True)
    args = [*_hardening(mem="4g", cpus="2", pids="512"), "--read-only", "--tmpfs", "/tmp:rw,size=2g",
            "--mount", f"type=bind,src={ROOT / 'tools'},dst=/analysis,readonly",
            "--mount", f"type=bind,src={out_dir.resolve()},dst=/out"]
    for src, dst in ((repo, "/code"), (java, "/java")):
        if src:
            args += ["--mount", f"type=bind,src={Path(src).resolve()},dst={dst},readonly"]
    args += [IMAGES["analysis"], "python", "/analysis/run_tools.py", *(["--verify"] if verify else [])]
    p = _docker_run(args, "", timeout)
    write_text(out_dir / "console.log", p.stdout + p.stderr)
    return p


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(RTB / "source_projects" / "Python" / DEFAULT_SAMPLE))
    ap.add_argument("--java", help="translated Java project (default: the RepoTransBench target of --repo, if any)")
    ap.add_argument("--skip-sample", action="store_true")
    a = ap.parse_args()
    run = new_run("analysis", "check")

    start = time.time()
    p = run_tools(run / "verify", verify=True)
    verify = _load(run / "verify" / "verify.json") or {}
    if not verify:
        sys.exit(f"verification did not run (exit {p.returncode}); see {run / 'verify' / 'console.log'}\n{p.stderr[-2000:]}")
    print(f"== verify: each tool on the built-in fixture ({time.time() - start:.0f}s)")
    for name, r in verify.items():
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {name:12} {r['version']:10} {r['checks']}"
              + (f"\n       {r['error']}" if r.get("error") else ""))

    sample = None
    if not a.skip_sample:
        repo = Path(a.repo)
        java = Path(a.java) if a.java else RTB / "target_projects" / "Python" / "Java" / repo.name
        java = java if java.is_dir() else None
        start = time.time()
        run_tools(run / f"sample-{repo.name}", repo=repo, java=java)
        sample = _load(run / f"sample-{repo.name}" / "tools.json") or {"tools": {}}
        print(f"\n== sample: {repo.name}" + (" (+ Java target)" if java else "") + f" ({time.time() - start:.0f}s)")
        ctx = sample.get("context", {})
        print(f"       {len(ctx.get('files', []))} source files, packages={ctx.get('packages')}, "
              f"py2-only files={len(ctx.get('py2_files', []))}")
        for name, r in sample["tools"].items():
            print(f"[{'ok  ' if r['ok'] else 'FAIL'}] {name:12} {r['seconds']:>6}s  "
                  f"{json.dumps(r.get('stats') if r['ok'] else r.get('error'))}")

    tools = sorted(verify)
    rows = [{"tool": t, "version": verify[t]["version"], "verified": verify[t]["ok"],
             **({"sample_ok": sample["tools"].get(t, {}).get("ok"),
                 "sample_stats": sample["tools"].get(t, {}).get("stats")} if sample else {})} for t in tools]
    write_json(run / "summary.json", {"image": IMAGES["analysis"], "sample": Path(a.repo).name if sample else None,
                                      "tools": rows})
    ok = all(r["verified"] for r in rows) and (not sample or all(r["sample_ok"] for r in rows))
    print(f"\n{sum(r['verified'] for r in rows)}/{len(rows)} tools verified"
          + (f", {sum(bool(r['sample_ok']) for r in rows)}/{len(rows)} ran on the sample" if sample else "")
          + f"\nlogs: {run}  (raw outputs: {rel(run)}/sample-*/<tool>.json)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
