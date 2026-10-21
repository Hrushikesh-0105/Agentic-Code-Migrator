"""Compare LLMs on Layer 1's real report tasks, to choose the model (python -m layer1.llm_bench).

Each candidate writes the two texts Layer 1 asks for, from a finished run that has an escalation:
the run summary, and the escalation brief of its first escalated unit. For each: was the text accepted by the
grounding check, after how many calls, how long it took, and the text itself (bench.md), so quality can be read
side by side. Output: logs/llm_bench/<timestamp>/{bench.json, bench.md, calls.jsonl}.

    python -m layer1.llm_bench                                  # every default/fallback model of each keyed provider
    python -m layer1.llm_bench --models groq:qwen/qwen3.8-27b gemini:gemini-3.5-flash-lite
    python -m layer1.llm_bench --run logs/layer1/<run>/<repo>   # another run (needs an escalation)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from layer0 import contract as bundle_contract
from sandbox.logs import LOG_ROOT, new_run, write_json, write_text

from . import llm as L


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def find_run():
    """The newest Layer 1 repo directory whose run escalated something."""
    for d in sorted(LOG_ROOT.glob("layer1/*/*/escalations.json"), key=lambda p: p.parent.parent.name, reverse=True):
        if _read(d) and (d.parent / "validation.json").exists() and (d.parent / "run.json").exists():
            return d.parent
    sys.exit("no finished Layer 1 run with an escalation; make one, e.g. python -m layer1 --bundles "
             "logs/layer0/<run> pallets_itsdangerous --dummy-fail itsdangerous.signer.Signer.get_signature")


def tasks(run):
    st, val = _read(run / "state.json"), _read(run / "validation.json")
    build = _read(run / "build_check" / "build_check.json") if (run / "build_check" / "build_check.json").exists() else None
    cfg = _read(run / "run.json")
    b = bundle_contract.load(cfg["bundle"])
    frag = {f["id"]: f for f in b["fragments"]}
    case = _read(run / "escalations.json")[0]
    attempts = []
    for a in range(1, case["attempts"] + 1):
        ev = _read(run / "units" / case["unit"] / f"attempt-{a}" / "evaluation.json")
        attempts.append({"attempt": a, "outcome": ev["outcome"], "reason": ev["reason"], "problems": ev["problems"][:3]})
    unit_of = {f: f"step-{s['step']:04d}" for s in b["order"]["steps"] for f in s["fragments"]}
    deps = {unit_of[d] for f in case["fragments"] for d in frag[f]["dependents"] if d not in case["fragments"]}
    return {"summary": lambda llm, log: L.run_summary(llm, st, val, build, cfg["layer2"]["name"], log=log),
            "brief": lambda llm, log: L.escalation_brief(llm, case, [frag[f] for f in case["fragments"]], attempts,
                                                         len(deps), b["manifest"]["migration"], log=log)}


def main():
    ap = argparse.ArgumentParser(prog="python -m layer1.llm_bench", description=__doc__.split("\n")[0])
    ap.add_argument("--run", help="a Layer 1 repo directory with an escalation (default: the newest)")
    ap.add_argument("--models", nargs="*", help="provider:model ... (default: every default and fallback model of "
                                                "each provider that has a key)")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    L.load_env()
    run = Path(a.run) if a.run else find_run()
    cands = [m.split(":", 1) for m in a.models] if a.models else \
        [(p, m) for p, cls in L.PROVIDERS.items() if os.environ.get(cls.key_env)
         for m in (cls.default_model, *cls.fallback_models)]
    out = new_run("llm_bench")
    log = out / "calls.jsonl"
    todo = tasks(run)
    print(f"== {len(cands)} model(s) on the report tasks of {run} -> {out}")
    rows = []
    for prov, model in cands:
        cls = L.PROVIDERS[prov]
        llm = cls(os.environ.get(cls.key_env, ""), model, retries=2)
        llm.model = model  # no probe: this benchmark is the probe
        row = {"provider": prov, "model": model}
        for name, fn in todo.items():
            start = time.time()
            r = fn(llm, log)
            row[name] = {"accepted": r["source"] == "llm", "calls": r["attempts"], "seconds": round(time.time() - start, 1),
                         "problems": r["problems"][:3], "text": r["text"]}
        rows.append(row)
        print(f"{prov + ':' + model:40} " + "  ".join(
            f"{n} {'ok ' if row[n]['accepted'] else 'NO '} {row[n]['calls']} call(s) {row[n]['seconds']:5}s" for n in todo)
            + "".join(f"\n     {n}: {row[n]['problems'][0][:150]}" for n in todo if row[n]["problems"]))
    write_json(out / "bench.json", {"run": str(run), "results": rows})
    md = [f"# LLM benchmark on `{run}`", ""]
    for r in rows:
        for n in todo:
            x = r[n]
            md += [f"## {r['provider']}:{r['model']} - {n} ({'accepted' if x['accepted'] else 'NOT accepted'}, "
                   f"{x['calls']} call(s), {x['seconds']} s)", "", x["text"], ""]
            md += [f"- problem: {p}" for p in x["problems"]] + ([""] if x["problems"] else [])
    write_text(out / "bench.md", "\n".join(md))
    print(f"\ntexts side by side: {out / 'bench.md'}")


if __name__ == "__main__":
    main()
