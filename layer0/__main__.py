"""Layer 0: static analysis and decomposition -> the bundle Layer 1 consumes.

python -m layer0 [REPO ...] [--java PATH] [--target java|python3]
python -m layer0 --verify

REPO is a path, or the name of a RepoTransBench Python project (its Java target is then used automatically).
With no REPO, the three default test pairs are built. Everything runs in a network-less acm-sandbox/analysis
container with the repo mounted read-only. Output: logs/layer0/<timestamp>[-label]/
    <repo>/  manifest.json  fragments.jsonl  dependency_graph.json  migration_order.json   <- input to Layer 1
             validation.json  report.md  raw/ (each tool's output)
    console.log  job.json  summary.json (committed run history)
"""
import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from sandbox.executor import IMAGES, _hardening
from sandbox.logs import new_run, rel, write_json, write_text

from . import contract, report
from .audit import audit

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
RTB = REPO_ROOT / "benchmarks" / "repotransbench"
# Python -> Java pairs that need significant changes (see layer0/README.md for how they were chosen)
DEFAULT_PAIRS = ["pavlov99_json-rpc", "pallets_itsdangerous", "jmespath_jmespath.py"]


def source_root(repo):
    """src/ layout: the importable packages live in repo/src, so module names must be computed from there."""
    src = repo / "src"
    if src.is_dir() and not (src / "__init__.py").exists() and \
            any((p / "__init__.py").exists() for p in src.iterdir() if p.is_dir()):
        return src, "src"
    return repo, "."


RTB_NAMES = {"python3": "Python", "java": "Java", "csharp": "C#", "c": "C", "cpp": "C++", "javascript": "JavaScript",
             "matlab": "Matlab", "go": "Go", "rust": "Rust"}
TARGET_PREFERENCE = ("java", "python3", "csharp", "go", "rust", "cpp")  # when a project has several targets


def detect_language(path):
    """The dominant source language of a directory, by file extension (tests and build output ignored)."""
    from langs import all_languages
    counts = {}
    for p in Path(path).rglob("*"):
        if p.is_file() and not any(x in p.parts for x in ("node_modules", ".git", "build", "target", "coverage")):
            for l in all_languages():
                if l.id != "python2" and p.suffix.lower() in l.extensions:
                    counts[l.id] = counts.get(l.id, 0) + 1
    if counts.get("c") and counts.get("cpp") and counts["cpp"] >= counts["c"]:
        counts.pop("c")
    return max(counts, key=counts.get) if counts else "python3"


def resolve(arg, java=None, target=None, source_language=None):
    """A repo argument -> {name, repo, code, source_root, source_language, target_language, java}. A RepoTransBench
    project name is looked up in every source language (or `source_language`); its benchmark target (tests) is
    found for `target`, or for the first target the benchmark has for it."""
    from langs import get
    path, lang = Path(arg), get(source_language).id if source_language else None
    if not path.is_dir():
        found = [(l, RTB / "source_projects" / n / arg) for l, n in RTB_NAMES.items()
                 if (RTB / "source_projects" / n / arg).is_dir() and (lang is None or l == lang)]
        if not found:
            sys.exit(f"not a directory or RepoTransBench project: {arg}")
        lang, path = found[0]
    lang = lang or detect_language(path)
    tgt = get(target).id if target else None
    src_name = RTB_NAMES.get("python3" if lang == "python2" else lang)
    targets = [l for l, n in RTB_NAMES.items() if src_name and (RTB / "target_projects" / src_name / n / path.name).is_dir()]
    if tgt is None:
        tgt = next((t for t in TARGET_PREFERENCE if t in targets), "python3" if lang != "python3" else None)
    tests = RTB / "target_projects" / (src_name or "") / RTB_NAMES.get(tgt or "", "") / path.name
    if java is None and tgt == "java" and tests.is_dir():
        java = tests
    if lang in ("python2", "python3"):
        code, root_rel = source_root(path.resolve())
    else:
        code, root_rel = path.resolve(), "."
    return {"name": path.name, "repo": path.resolve(), "code": code, "source_root": root_rel,
            "source_language": lang, "target_language": tgt,
            "java": Path(java).resolve() if java else None, "target_tests": tests.resolve() if tests.is_dir() else None}


def run_container(run, mounts, command, timeout):
    """Run the pipeline in acm-sandbox/analysis, streaming its output to the terminal and console.log."""
    name = "acm-" + uuid.uuid4().hex[:12]
    args = ["docker", "run", "--rm", "--name", name, *_hardening(mem="4g", cpus="2", pids="512"),
            "--read-only", "--tmpfs", "/tmp:rw,size=2g",
            "--mount", f"type=bind,src={ROOT / 'pipeline'},dst=/layer0,readonly",
            "--mount", f"type=bind,src={REPO_ROOT / 'analysis' / 'tools'},dst=/analysis,readonly",
            "--mount", f"type=bind,src={run.resolve()},dst=/out"]
    for src, dst in mounts:
        args += ["--mount", f"type=bind,src={src},dst={dst},readonly"]
    args += [IMAGES["analysis"], "python", "/layer0/build.py", *command]
    deadline = time.time() + timeout
    with open(run / "console.log", "w", encoding="utf-8") as log:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace")
        for line in p.stdout:
            sys.stdout.write("   " + line)
            log.write(line)
            if time.time() > deadline:
                subprocess.run(["docker", "kill", name], capture_output=True)
                break
        return p.wait()


def finish(bundle, source_dir):
    """Host-side contract validation, graph-quality audit and the readable report for one bundle."""
    checks = contract.validate(bundle, source_dir)
    write_json(bundle / "validation.json", checks)
    quality = audit(bundle) if all(c["ok"] for c in checks) else None
    if quality:
        write_json(bundle / "audit.json", quality)
    write_text(bundle / "report.md", report.render(bundle, checks, quality))
    return checks, quality


def verify(run):
    """Layer 0 on its built-in fixture, in `run`: fixture checks + host-side contract checks. Returns the summary."""
    print(f"== Layer 0 verify (fixture with known answers) -> {run}")
    code = run_container(run, [], ["--verify"], timeout=900)
    bundle = run / "verify" / "bundle"
    fixture = json.loads((run / "verify" / "verify.json").read_text()) if (run / "verify" / "verify.json").exists() else []
    checks = finish(bundle, run / "verify" / "fixture" / "src")[0] if (bundle / "manifest.json").exists() else []
    print("\n== host-side contract validation of the fixture bundle")
    for c in checks:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}")
    ok = code == 0 and fixture and all(c["ok"] for c in fixture) and checks and all(c["ok"] for c in checks)
    summary = {"kind": "verify", "ok": bool(ok), "fixture_checks": fixture, "contract_checks": checks}
    write_json(run / "summary.json", summary)
    print(f"\n{sum(c['ok'] for c in fixture)}/{len(fixture)} fixture checks, "
          f"{sum(c['ok'] for c in checks)}/{len(checks)} contract checks\nlogs: {run}")
    return summary


def build(repos, run, target=None):
    """Layer 0 on resolved repos (see resolve()), one bundle per repo in run/<name>/. Returns the summary."""
    if len({r["name"] for r in repos}) != len(repos):
        sys.exit("two repos share a name")
    mounts, job = [], {"repos": []}
    for i, r in enumerate(repos):
        mounts.append((r["code"], f"/repos/{i}"))
        if r["java"]:
            mounts.append((r["java"], f"/javas/{i}"))
        job["repos"].append({"name": r["name"], "code": f"/repos/{i}", "java": f"/javas/{i}" if r["java"] else None,
                             "out": f"/out/{r['name']}", "source_root": r["source_root"],
                             "source_language": r.get("source_language"),
                             "target_language": target or r.get("target_language")})
    write_json(run / "job.json", job)
    print(f"== Layer 0 on {len(repos)} repo(s) -> {run}")
    start = time.time()
    code = run_container(run, mounts, ["/out/job.json"], timeout=1800 * len(repos))

    rows = []
    print("\n== bundles (input to Layer 1), validated on the host against the original sources")
    for r in repos:
        bundle = run / r["name"]
        if not (bundle / "manifest.json").exists():
            err = json.loads((bundle / "error.json").read_text()) if (bundle / "error.json").exists() else {}
            rows.append({"repo": r["name"], "ok": False, "error": err.get("error", f"container exit {code}")})
            print(f"[FAIL] {r['name']}: {rows[-1]['error']}")
            continue
        checks, quality = finish(bundle, r["code"])
        man = json.loads((bundle / "manifest.json").read_text())
        ok = all(c["ok"] for c in checks)
        rows.append({"repo": r["name"], "ok": ok, "regime": man["migration"]["regime"],
                     "source_root": r["source_root"], "stats": man["stats"],
                     "validation": f"{sum(c['ok'] for c in checks)}/{len(checks)}",
                     "failed_checks": [c["check"] for c in checks if not c["ok"]],
                     "graph_quality": {k: quality[k] for k in ("edge_evidence", "call_recall")} if quality else None,
                     "tool_failures": [t for t, x in man["toolchain"]["tools"].items() if not x["ok"]],
                     "bundle": rel(bundle)})
        s = man["stats"]
        print(f"[{'PASS' if ok else 'FAIL'}] {r['name']:24} {man['migration']['regime']:15} "
              f"{s['fragments']:4} fragments  {s['edges']:4} edges  {s['steps']:4} steps  {s['waves']:3} waves  "
              f"{s['cycles']} cycles  contract {rows[-1]['validation']}"
              + (f"  evidence {quality['edge_evidence']}  recall {quality['call_recall']}" if quality else ""))
        for c in checks:
            if not c["ok"]:
                print(f"       FAIL {c['check']}: {c['detail']}")
    summary = {"kind": "build", "format": f"{contract.FORMAT_NAME} {contract.FORMAT_VERSION}",
               "seconds": round(time.time() - start, 1), "repos": rows}
    write_json(run / "summary.json", summary)
    print(f"\nlogs: {run}  (open <repo>/report.md for a readable view)")
    return summary


def main():
    ap = argparse.ArgumentParser(prog="python -m layer0", description=__doc__.split("\n")[0])
    ap.add_argument("repos", nargs="*", help="paths or RepoTransBench project names (default: the 3 test pairs)")
    ap.add_argument("--java", help="Java target project (only with a single REPO)")
    ap.add_argument("--target", help="target language: python3, java, csharp, go, rust, cpp ... (default: the "
                                      "benchmark's target for the project; java first for Python sources)")
    ap.add_argument("--source-language", help="source language when it cannot be inferred (a langs id or name)")
    ap.add_argument("--verify", action="store_true", help="run the pipeline on a built-in fixture with known answers")
    a = ap.parse_args()

    if a.verify:
        sys.exit(0 if verify(new_run("layer0", "verify"))["ok"] else 1)
    if a.java and len(a.repos) != 1:
        sys.exit("--java needs exactly one REPO")
    repos = [resolve(r, a.java, a.target, a.source_language) for r in (a.repos or DEFAULT_PAIRS)]
    run = new_run("layer0", repos[0]["name"] if len(repos) == 1 else f"{len(repos)}repos")
    summary = build(repos, run, a.target)
    sys.exit(0 if all(x["ok"] for x in summary["repos"]) else 1)


if __name__ == "__main__":
    main()
