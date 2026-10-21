"""Repair agent (heavy coding model): consumes the Verdict agent's structured failure record (failing inputs,
both sides' outcomes, value and state diffs, stack traces, compile errors at their exact lines, the localized
suspects) and patches the translated fragments. A short fragment (up to EDIT_THRESHOLD lines) comes back whole,
corrected; a long one as find/replace edits, applied all-or-nothing. Either way the change is recorded as a
unified diff (the patch). It may read the workspace and Layer 0 through MCP before answering."""
import difflib
import json

from layer2 import pieces as P
from layer2.prompts import repair_system

from .analyzer import contract_for
from .base import Agent, fit
from .context import numbered_source

EDIT_THRESHOLD = 80
TOOLS = {"workspace": ["read_file", "outline_file"], "analysis": ["get_fragment", "get_dependencies"]}


def marked(code, lines):
    """The code numbered, with the lines the failure points at marked `<- FAILING` (UniTrans-style)."""
    return "\n".join(f"{n:>4}| {line}" + ("   <- FAILING" if n in lines else "")
                     for n, line in enumerate(code.splitlines(), 1))


def failing_lines(fid, code, record):
    """Line numbers (in the fragment's code) the compile errors and the diagnosis point at."""
    out = set()
    rows = [l.strip() for l in code.splitlines()]
    for e in record.get("compile_errors") or []:
        if e.get("fragment") == fid and e.get("code"):
            out |= {k + 1 for k, r in enumerate(rows) if r and r == e["code"].strip()}
    for s in ((record.get("analysis") or {}).get("suspects") or []):
        if isinstance(s, dict) and s.get("fragment") in (fid, "?") and isinstance(s.get("line"), int):
            out.add(s["line"])
    return out


def unmark(text):
    """A model may copy the `<- FAILING` markers back: they are not code."""
    return "\n".join(l.split("   <- FAILING")[0] for l in text.split("\n"))


def unified(old, new, fid):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{fid}", f"b/{fid}", n=1))


class Repairer(Agent):
    name = "repair"
    max_tokens = 3400

    def run(self, job, outputs, record, contract):
        system = repair_system(self.pair, "Python 2.7" if self.py2 else "Python 3.11")
        evidence = {k: record[k] for k in ("kind", "summary", "compile_errors", "failing", "analysis",
                                           "edit_problems", "harness_errors") if record.get(k)}
        sections = [("FAILURE RECORD (from the Verdict agent)", json.dumps(evidence, indent=1, ensure_ascii=False), 0)]
        mode = {}
        for f in job.fragments:
            o = outputs.get(f["id"], {})
            code = o.get("code", "")
            mode[f["id"]] = "WHOLE" if code.count("\n") < EDIT_THRESHOLD else "EDITS"
            cur = (f"declaration: {o.get('declaration')}\n" if o.get("declaration") else "") + \
                (f"imports: {o.get('imports')}\n" if o.get("imports") else "") + \
                marked(code, failing_lines(f["id"], code, record))
            sections.append((f"CURRENT TRANSLATION OF {f['id']} [{mode[f['id']]}] ({self.sk.describe(f['id'])})",
                             cur, 0))
            sections.append((f"LEGACY SOURCE OF {f['id']}", numbered_source(f), 1))
            c = {k: v for k, v in contract_for(contract, f["id"]).items() if k in ("params", "returns", "raises", "hazards")}
            sections.append(("CONTRACT", json.dumps(c, ensure_ascii=False), 3))
        tools = [t for s, names in TOOLS.items() for t in self.hub.openai_tools(s, names)]
        user = fit(sections, self.budget(system=system) - 3500)  # room for one round of tool results
        answer, meta = self.ask(system, user, tools=tools, rounds=1, purpose=f"repair {job.unit}",
                                effort="medium")
        new, notes, patches = {k: dict(v) for k, v in outputs.items()}, [], {}
        for x in answer.get("fragments") or []:
            fid = x.get("id") if isinstance(x, dict) else None
            if fid not in new:
                continue
            old = new[fid].get("code", "")
            if x.get("code"):
                new[fid]["code"] = unmark(str(x["code"]))
            elif x.get("edits"):
                code, problems = P.apply_edits(old, [{k: unmark(str(v)) for k, v in e.items()} if isinstance(e, dict)
                                                     else e for e in x["edits"]])
                if problems:  # all or nothing: a half-applied patch leaves broken code
                    notes += [f"{fid}: edits not applied: {p}" for p in problems]
                else:
                    new[fid]["code"] = code
            if x.get("imports") is not None:
                new[fid]["imports"] = [str(i) for i in x["imports"]]
            if x.get("declaration"):
                new[fid]["declaration"] = x["declaration"]
            if new[fid].get("code", "") != old:
                patches[fid] = unified(old, new[fid]["code"], fid)
        changed = [fid for fid in new if new[fid] != outputs.get(fid)]
        return new, {**meta, "changed": changed, "edit_problems": notes, "patches": patches}
