"""Layer 1: deterministic orchestration of Layer 0's fragments through Layer 2.

python -m layer1 [REPO ...]              Layer 0 on the repos (default: its 3 test pairs), then Layer 1 on each bundle
python -m layer1 --layer2 agents ...     the real Layer 2 (LLM agents + MCP + Docker sandbox; needs GROQ_API_KEY);
                                         default: the dummy, which passes every unit without translating
python -m layer1 --scope ID ...          migrate only these fragments / modules / classes and what they depend on
python -m layer1 --bundles DIR [REPO ...]  Layer 1 on existing bundles (a Layer 0 run dir, or one bundle dir)
python -m layer1 --verify                known-answer scenarios on the Layer 0 fixture (retries, escalation,
                                         blocking, human-in-the-loop, crash + resume, determinism, ...)
python -m layer1 --resume DIR            continue an unfinished run (crashed, or paused for a human)

Output: logs/layer1/<timestamp>[-label]/
    <repo>/  plan.json  events.jsonl  units/<unit>/attempt-<n>/{request,verdict,evaluation}.json
             units/<unit>/attempt-<n>/layer2/ and layer2/ (agents: llm_calls.jsonl, mcp_calls.jsonl, workspace/)
             state.json  translations.jsonl  escalations.json  migrated/   <- the migrated codebase
             validation.json  build_check/  report.md  report_summary.json  llm/calls.jsonl
    graph.mmd  console.log  summary.json (committed run history)
"""
import argparse
import json
import sys
from pathlib import Path

from layer0 import __main__ as layer0
from layer0 import contract as bundle_contract
from sandbox.logs import LOG_ROOT, new_run, rel, write_json, write_text

from . import contract, report
from . import llm as llm_text
from .build_check import build_check
from .orchestrator import MAX_HUMAN_RETRIES, mermaid, migrate


class Console:
    """print() that is also kept in the run's console.log."""

    def __init__(self, run):
        self.f = open(Path(run) / "console.log", "a", encoding="utf-8")

    def __call__(self, *parts):
        line = " ".join(str(p) for p in parts)
        print(line, flush=True)
        self.f.write(line + "\n")
        self.f.flush()


def parse_script(items):
    """--dummy-fail ID[=action,action,...] (default: fail on every attempt)."""
    script = {}
    for item in items or []:
        fid, _, acts = item.partition("=")
        script[fid] = acts.split(",") if acts else ["fail"]
    return script


def ask_human(payload):
    """--hitl prompt: show an escalated unit and ask for a decision on stdin (a terminal, or piped answers).
    With no input left the run pauses; `--resume` asks again."""
    try:
        return _ask(payload)
    except EOFError:
        print("   (no input: pausing)")
        return None


def _ask(payload):
    c = payload["case"]
    print(f"\n== HUMAN REVIEW: {payload['unit']} ({c['reason']} after {c['attempts']} attempt(s))")
    print(f"   last outcome: {c['last_outcome']}: {c['last_reason']}")
    for p in c["problems"] + payload["problems_with_last_decision"]:
        print(f"   problem: {p}")
    for f in payload["fragments"]:
        print(f"   --- {f['id']} ({f['kind']}, {f['file']})")
        print("".join("   | " + l + "\n" for l in f["source"].splitlines()[:40]), end="")
    print(f"   last request: {payload['last_request']}\n   last verdict: {payload['last_verdict']}")
    if payload.get("brief"):
        print("   brief for the reviewer:\n" + "".join("   " + l + "\n" for l in payload["brief"].splitlines()), end="")
    while True:
        choice = input(f"   [a]ccept a translation / [r]etry with guidance ({payload['human_retries_left']} left) "
                       "/ [s]kip: ").strip().lower()[:1]
        if choice == "a":
            path = input("   JSON file with a list of pieces (see layer1/schemas/verdict.json, Piece): ").strip()
            try:
                return {"action": "accept", "translations": json.loads(Path(path).read_text(encoding="utf-8"))}
            except (OSError, ValueError) as e:
                print(f"   cannot read {path}: {e}")
        elif choice == "r":
            return {"action": "retry", "guidance": input("   guidance for Layer 2: ").strip()}
        elif choice == "s":
            return {"action": "skip", "note": input("   note (optional): ").strip()}


def make_layer2(cfg, bundle, run_cfg=None, out=None):
    if cfg.get("name") == "agents":
        from layer2.agentic import AgentLayer2
        return AgentLayer2(bundle, run_cfg["bundle"], run_cfg["source_root"], out,
                           max_repairs=cfg.get("max_repairs", 2))
    from layer2.dummy import DummyLayer2
    frag_ids = {f["id"] for f in bundle["fragments"]}
    return DummyLayer2(bundle, {k: v for k, v in cfg.get("script", {}).items() if k in frag_ids})


_LLMS = {}


def get_llm(cfg, echo=print):
    """The report-writing LLM for a run's config ({"mode", "model"}), created once per process."""
    key = (cfg.get("mode", "off"), cfg.get("model"))
    if key not in _LLMS:
        _LLMS[key] = llm_text.from_env(*key)
        echo(f"   LLM for reports and escalation briefs: {_LLMS[key][1]}")
    return _LLMS[key][0]


def finish(out, echo, check_build=True):
    """Contract validation, build check and report for one finished repo; returns its summary row."""
    cfg = json.loads((out / "run.json").read_text(encoding="utf-8"))
    st = json.loads((out / "state.json").read_text(encoding="utf-8"))
    if "units" not in st:
        write_text(out / "report.md", report.render(out, []))
        echo(f"[FAIL] {cfg['repo']}: bundle rejected: {st['aborted'][0]}")
        return {"repo": cfg["repo"], "ok": False, "aborted": st["aborted"], "out": rel(out)}
    checks = contract.validate(out, cfg["bundle"])
    write_json(out / "validation.json", checks)
    lang = st["migration"]["target_language"]
    build = build_check(out / "migrated", lang, out / "build_check") if check_build else None
    l2 = report.layer2_evidence(out, st) if cfg["layer2"]["name"] != "dummy" else None
    if l2:
        write_json(out / "layer2_summary.json", l2)
    summary = llm_text.run_summary(get_llm(cfg.get("llm", {}), echo), st, checks, build, cfg["layer2"]["name"],
                                   log=out / "llm" / "calls.jsonl", evidence=l2)
    write_json(out / "report_summary.json", summary)
    write_text(out / "report.md", report.render(out, checks, build, cfg["layer2"]["name"], rel(cfg["bundle"]),
                                                summary, l2))
    res = st["result"]
    ok = all(x["ok"] for x in checks) and res["ok"]
    row = {"repo": cfg["repo"], "ok": ok, "regime": st["migration"]["regime"], "assembly": st["assembly_mode"],
           "layer2": cfg["layer2"]["name"], "bundle": rel(cfg["bundle"]), "units": res["units_by_status"],
           "fragments": res["fragments_by_status"],
           "counters": {k: res["counters"][k] for k in ("invocations", "retries", "invalid_verdicts",
                                                        "escalated_units", "blocked_units", "vendored_units")},
           "contract": f"{sum(x['ok'] for x in checks)}/{len(checks)}",
           "failed_checks": [x["check"] for x in checks if not x["ok"]],
           "build": {k: build[k] for k in ("ok", "tool", "files")} | {"errors": len(build["errors"])} if build else None,
           "report_summary": {"source": summary["source"], "model": summary["model"], "attempts": summary["attempts"]},
           "out": rel(out)}
    if cfg.get("scope"):
        row["scope"] = cfg["scope"]
    if l2:
        row["layer2_evidence"] = {k: l2[k] for k in ("units_verified_by_tests", "units_compiled_only",
                                                     "differential_tests", "tests_equivalent", "units_repaired",
                                                     "llm_tokens", "llm_calls", "mcp_calls")}
    u = res["units_by_status"]
    echo(f"[{'PASS' if ok else 'FAIL'}] {cfg['repo']:24} {st['migration']['regime']:15} "
         f"{sum(u.values()):4} units: " + ", ".join(f"{v} {k}" for k, v in u.items()) +
         f"  | {res['counters']['invocations']} Layer 2 calls  | contract {row['contract']}" +
         (f"  | build {'ok' if build['ok'] else 'FAILED'} ({build['tool']}, {build['files']} files)" if build else ""))
    for x in checks:
        if not x["ok"]:
            echo(f"       FAIL {x['check']}: {x['detail']}")
    if build and not build["ok"]:
        for e in build["errors"][:3]:
            echo(f"       build: {e}")
    return row


def run_one(out, bundle_dir, source, opts, echo):
    bundle = bundle_contract.load(bundle_dir)
    cfg = {"repo": bundle["manifest"]["repo"]["name"], "bundle": str(Path(bundle_dir).resolve()),
           "source_root": str(Path(source).resolve()), "max_attempts": opts["max_attempts"], "hitl": opts["hitl"],
           "vendored": opts["vendored"], "layer2": opts["layer2"], "llm": opts["llm"], "scope": opts["scope"]}
    write_json(out / "run.json", cfg)
    echo(f"\n== {cfg['repo']}: Layer 1 -> {out}")
    return resume_one(out, echo, opts["build"], resume=False)


def resume_one(out, echo, check_build=True, resume=True):
    cfg = json.loads((out / "run.json").read_text(encoding="utf-8"))
    bundle = bundle_contract.load(cfg["bundle"])
    layer2 = make_layer2(cfg["layer2"], bundle, cfg, out)
    if cfg["layer2"]["name"] == "agents":
        d = layer2.describe()
        echo("   Layer 2 agents: " + "; ".join(f"{a} {' > '.join(c)}" for a, c in d["llm"]["chains"].items()))
        echo("   MCP servers: " + "; ".join(f"{s['name']} ({len(s['tools'])} tools)" for s in d["mcp_servers"].values()))
    try:
        r = migrate(cfg["bundle"], cfg["source_root"], out, layer2,
                    max_attempts=cfg["max_attempts"], hitl=cfg["hitl"], vendored=cfg["vendored"],
                    human=ask_human if cfg["hitl"] == "prompt" else None, resume=resume, echo=echo,
                    llm=get_llm(cfg.get("llm", {}), echo), scope=cfg.get("scope"))
    except BaseException as e:
        if type(e).__name__ != "QuotaExhausted":
            raise
        echo(f"[STOPPED] {cfg['repo']}: {e}\n   the run is checkpointed; continue with: python -m layer1 --resume {out}")
        return {"repo": cfg["repo"], "ok": False, "stopped": str(e), "out": rel(out)}
    finally:
        if hasattr(layer2, "close"):
            layer2.close()
    if r["status"] == "paused":
        echo(f"[PAUSED] {cfg['repo']}: {r['pending']['unit']} waits for a human decision; "
             f"continue with: python -m layer1 --resume {out}")
        return {"repo": cfg["repo"], "ok": False, "paused": r["pending"]["unit"], "out": rel(out)}
    return finish(out, echo, check_build)


def write_summary(run, rows, kind="migrate"):
    write_json(run / "summary.json", {"kind": kind, "max_human_retries": MAX_HUMAN_RETRIES, "repos": rows})


def bundles_from(path, names):
    path = Path(path)
    found = [path] if (path / "manifest.json").exists() else \
        sorted(p for p in path.iterdir() if (p / "manifest.json").exists())
    found = [p for p in found if not names or p.name in names]
    if not found:
        sys.exit(f"no Layer 0 bundles in {path}" + (f" for {names}" if names else ""))
    return found


def main():
    ap = argparse.ArgumentParser(prog="python -m layer1", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("repos", nargs="*", help="paths or RepoTransBench project names (default: Layer 0's 3 test pairs)")
    ap.add_argument("--bundles", help="use existing Layer 0 bundles: a logs/layer0/<run> dir or one bundle dir")
    ap.add_argument("--source", help="the repo's source root (with --bundles, if it is not a RepoTransBench project)")
    ap.add_argument("--target", help="target language for Layer 0 (python3, java, csharp, go, rust, cpp; default: the "
                                      "benchmark's target of each project)")
    ap.add_argument("--max-attempts", type=int, default=3, help="Layer 2 attempts per unit before escalating (3)")
    ap.add_argument("--hitl", choices=["defer", "prompt"], default="defer",
                    help="escalations: record and move on (defer), or ask in the terminal (prompt)")
    ap.add_argument("--vendored", choices=["skip", "migrate"], default="skip", help="vendored code, e.g. a bundled six.py")
    ap.add_argument("--dummy-fail", action="append", metavar="ID[=ACTIONS]",
                    help="make the dummy Layer 2 misbehave on a fragment, e.g. pkg.mod.f=fail,pass "
                         "(actions: pass fail error needs_human malformed raise)")
    ap.add_argument("--layer2", choices=["dummy", "agents"], default="dummy",
                    help="dummy: passes every unit without translating; agents: Analyzer, Test-Gen, Translator, "
                         "Executor (Docker), Verdict and Repair agents through MCP servers, on Groq (GROQ_API_KEY)")
    ap.add_argument("--max-repairs", type=int, default=2, help="agents: Repair rounds per attempt (2)")
    ap.add_argument("--scope", action="append", metavar="ID",
                    help="migrate only fragments matching ID (a fragment id, module, class or fnmatch pattern) and "
                         "everything they depend on; the rest is out_of_scope. Repeatable")
    ap.add_argument("--no-build-check", action="store_true", help="skip compiling the migrated codebase")
    ap.add_argument("--llm", choices=["auto", "groq", "gemini", "off"], default="auto",
                    help="LLM for report summaries and escalation briefs (never for decisions): auto = every provider "
                         "with a key (GROQ_API_KEY, GEMINI_API_KEY), in the order ACM_LLM_PROVIDERS (groq,gemini); "
                         "keys come from the environment or .env; without a usable one, templates are used")
    ap.add_argument("--llm-model", help="model of the first provider (default: ACM_GROQ_MODEL / ACM_GEMINI_MODEL, "
                                        f"else {llm_text.GroqLLM.default_model} / {llm_text.GeminiLLM.default_model})")
    ap.add_argument("--verify", action="store_true", help="known-answer scenarios on the Layer 0 fixture")
    ap.add_argument("--resume", metavar="DIR", help="continue an unfinished run: logs/layer1/<run>/<repo>")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # LLM text can hold characters a Windows console code page lacks
        stream.reconfigure(errors="replace")

    if a.verify:
        from .verify import run_verify
        run = new_run("layer1", "verify")
        sys.exit(0 if run_verify(run, Console(run)) else 1)
    if a.resume:
        out = Path(a.resume).resolve()
        echo = Console(out.parent)
        row = resume_one(out, echo, not a.no_build_check)
        summ = out.parent / "summary.json"
        rows = json.loads(summ.read_text(encoding="utf-8"))["repos"] if summ.exists() else []
        write_summary(out.parent, [row if r["repo"] == row["repo"] else r for r in rows] or [row])
        sys.exit(0 if row["ok"] else 1)

    opts = {"max_attempts": a.max_attempts, "hitl": a.hitl, "vendored": a.vendored,
            "script": parse_script(a.dummy_fail), "build": not a.no_build_check,
            "llm": {"mode": a.llm, "model": a.llm_model}, "scope": a.scope,
            "layer2": {"name": "dummy", "script": parse_script(a.dummy_fail)} if a.layer2 == "dummy" else
            {"name": "agents", "max_repairs": a.max_repairs}}
    if a.bundles:
        jobs = []
        for b in bundles_from(a.bundles, a.repos):
            name = bundle_contract.load(b)["manifest"]["repo"]["name"]
            src = Path(a.source) if a.source else layer0.resolve(name, None)["code"]
            jobs.append((b, src))
    else:
        repos = [layer0.resolve(r, None, a.target) for r in (a.repos or layer0.DEFAULT_PAIRS)]
        label = repos[0]["name"] if len(repos) == 1 else f"{len(repos)}repos"
        l0 = layer0.build(repos, new_run("layer0", label), a.target)
        jobs = []
        for r, row in zip(repos, l0["repos"]):
            if row["ok"]:
                jobs.append((LOG_ROOT / row["bundle"], r["code"]))
            else:
                print(f"[SKIP] {r['name']}: Layer 0 failed, no bundle")
    if not jobs:
        sys.exit("nothing to migrate")
    run = new_run("layer1", jobs[0][0].name if len(jobs) == 1 else f"{len(jobs)}repos")
    echo = Console(run)
    write_text(run / "graph.mmd", mermaid())
    echo(f"== Layer 1 (LangGraph orchestration, Layer 2 = {a.layer2}) on {len(jobs)} bundle(s) -> {run}")
    rows = [run_one(run / Path(b).name, b, src, opts, echo) for b, src in jobs]
    write_summary(run, rows)
    echo(f"\nlogs: {run}  (open <repo>/report.md; the migrated codebase is <repo>/migrated/)")
    sys.exit(0 if all(r["ok"] for r in rows) else 1)


if __name__ == "__main__":
    main()
