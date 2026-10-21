"""Verdict agent: compares the runtime behaviour of the legacy and the translated code and decides Equivalent or
Inequivalent. The decision is deterministic (sandbox/compare.py through the acm-sandbox MCP server: status,
exception type, return value, argument mutation, object state, stdout); an LLM never decides it.
On a mismatch it writes the structured failure record the Repair agent consumes: the failing inputs, both sides'
outcomes, the value diffs, the translation's stack trace, compile errors with the exact line, and a lightweight
LLM's diagnosis that localizes the suspect lines."""
import json

from layer2.javatext import numbered
from layer2.prompts import verdict_system
from layer2.values import show

from .base import Agent, fit
from .context import numbered_source
from .translator import case_line


def outcome(r):
    if not r:
        return "?"
    if r.get("status") == "ok":
        return "returns " + show(r["value"])
    if r.get("status") == "exception":
        return f"raises {r['exception']['type']}: {r['exception'].get('message', '')[:160]}"
    if r.get("status") == "unresolved":
        return "cannot be called: " + r.get("message", "")[:300]
    if r.get("status") == "untestable":
        return "untestable: the harness cannot pass these arguments to the declared parameter types: " + \
            r.get("message", "")[:240]
    return r.get("status", "?")


def _diff_text(d):
    leg = d.get("legacy")
    mig = d.get("migrated")
    f = lambda v: show(v) if isinstance(v, dict) and "t" in v else repr(v)[:150]  # noqa: E731
    return f"{d['path']}: {d['kind']}" + (f" (legacy {f(leg)} vs translation {f(mig)})" if leg is not None or mig is not None else "")


class VerdictAgent(Agent):
    name = "verdict"
    max_tokens = 1500

    def judge(self, job, rep, tests, outputs):
        cases = {c["case_id"]: c for c in tests.get("cases", [])}
        comps = rep.get("comparisons") or []
        failing = []
        res = {r.get("case_id"): r for r in rep.get("results") or []}
        for v in comps:
            if v["equivalent"]:
                continue
            c = cases[v["case_id"]]
            tb = (res.get(v["case_id"], {}).get("exception") or {}).get("traceback") or ""
            failing.append({"case_id": v["case_id"], "fragment": c["fragment"], "purpose": c["purpose"],
                            "call": case_line(c).split(" -> ")[0], "legacy": outcome(c["legacy"]),
                            "translation": outcome(res.get(v["case_id"])),
                            "diffs": [_diff_text(d) for d in v["diffs"][:6]],
                            "stack_trace": "\n".join(tb.splitlines()[:8])})
        untestable = tests.get("untestable", {})
        n, passed = len(comps), sum(v["equivalent"] for v in comps)
        if rep["assembly_problems"]:
            kind = "assembly_problem"
        elif not rep["compiled"]:
            kind = "compile_error"
        elif failing and all(f["translation"].startswith(("cannot be called", "untestable")) for f in failing):
            kind = "unresolved_api"
        elif failing:
            kind = "inequivalent"
        elif n:
            kind = "equivalent"
        else:
            kind = "compiled_only"
        record = {"equivalent": kind in ("equivalent", "compiled_only"), "kind": kind, "compiled": rep["compiled"],
                  "tests": n, "passed": passed, "failed": len(failing), "failing": failing[:8],
                  "compile_errors": rep.get("compile_errors", [])[:8], "assembly_problems": rep["assembly_problems"],
                  "untestable": {k: v for k, v in untestable.items() if k in job.ids},
                  "hazard_notes": sorted({h["hazard"] for v in comps for h in v.get("hazard_notes", [])}),
                  "profile": rep.get("profile"), "harness_errors": rep.get("harness_errors") or []}
        record["summary"] = self.summary(record)
        if kind in ("inequivalent", "unresolved_api"):
            record["analysis"], record["llm"] = self.diagnose(job, record, outputs)
        return record

    @staticmethod
    def summary(r):
        if r["kind"] == "assembly_problem":
            return "the pieces do not fit the skeleton: " + "; ".join(r["assembly_problems"][:2])
        if r["kind"] == "compile_error":
            e = r["compile_errors"][:2]
            return f"does not compile ({len(r['compile_errors'])} error(s)): " + "; ".join(
                f"{x['fragment']} line {x['line']}: {x['message']}" for x in e)
        if r["kind"] in ("inequivalent", "unresolved_api"):
            f = r["failing"][0]
            return (f"{r['failed']}/{r['tests']} differential tests differ, e.g. {f['call']}: legacy {f['legacy']}, "
                    f"translation {f['translation']}")[:400]
        if r["kind"] == "equivalent":
            return f"{r['passed']}/{r['tests']} differential tests equivalent ({r['profile']})"
        return "compiles; no behavioural tests for this unit (" + "; ".join(
            f"{k.rsplit('.', 1)[-1]}: {v}" for k, v in r["untestable"].items())[:200] + ")"

    def diagnose(self, job, record, outputs):
        code = "\n\n".join(f"// ===== {fid}\n" + numbered(o.get("code", "")) for fid, o in outputs.items())
        sections = [("FAILING CASES", json.dumps(record["failing"][:4], indent=1, ensure_ascii=False), 0),
                    ("TRANSLATED CODE (line numbers per fragment)", code, 1)]
        for f in job.fragments:
            sections.append((f"LEGACY {f['id']}", numbered_source(f), 2))
        try:
            answer, meta = self.ask(verdict_system(self.pair), fit(sections, self.budget(system=verdict_system(self.pair))),
                                    purpose=f"diagnose {job.unit}")
            return {k: answer.get(k) for k in ("diagnosis", "suspects", "fix")}, meta
        except Exception as e:  # noqa: BLE001  the deterministic record is enough to repair from
            return {"diagnosis": None, "error": f"{type(e).__name__}: {e}"[:300]}, {}
