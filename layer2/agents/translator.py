"""Translator agent (heavy open-weight coding model): synthesizes the unit's fragments into the target layout,
following the Analyzer's contract. It sees the fragment, the target file as it stands (an outline from the
acm-workspace MCP server), the translated API of every dependency, a few legacy test results (behaviour to
match), and, on a later attempt, why the earlier ones failed and any human guidance. Its rules are built for the
language pair from langs/ (layer2/prompts.py)."""
import json
import re

from layer2 import pieces as P
from layer2.mcp_client import ToolCallError
from layer2.prompts import translator_system
from layer2.values import show

from .analyzer import contract_for
from .base import Agent, fit
from .context import dependencies, numbered_source

CONTRACT_KEYS = ("params", "returns", "raises", "side_effects", "hazards", "target_notes", "java_notes",
                 "python3_notes")
OVERLOAD = re.compile(r"~\d+$")
INHERITS_CONSTRUCTORS = ("python3", "javascript")  # targets where a subclass inherits its base's constructor


def call_text(c):
    """A test case's call, as readable source-like text."""
    if "python" in c:
        py = c["python"]
        call = py["callable"]
        if py.get("init") is not None:
            cls, _, meth = call.rpartition(".")
            call = f"{cls}({', '.join(show(a) for a in py['init']['args'])}).{meth}"
        args = ", ".join([show(a) for a in py["args"]] + [f"{k}={show(v)}" for k, v in py["kwargs"].items()])
        return call if py.get("attr") else f"{call}({args})"
    call = c.get("source_call") or {}
    args = ", ".join(show(a) for a in call.get("args", []))
    cls = (call.get("class") or "").replace("$", ".").split(".")[-1]
    if call.get("kind") == "constructor":
        return f"new {cls}({args})"
    if call.get("kind") in ("method", "property"):
        init = ", ".join(show(a) for a in call.get("init", []))
        return f"{cls}({init}).{call['member']}" + ("" if call["kind"] == "property" else f"({args})")
    return f"{cls + '.' if cls else ''}{call.get('member')}({args})"


def case_line(c):
    """One legacy test case as `call -> result`, for prompts."""
    r = c.get("legacy") or {}
    if r.get("status") == "ok":
        res = show(r["value"]) if r.get("value") is not None else "returns (nothing)"
    elif r.get("status") == "exception":
        res = f"raises {r['exception']['type']}: {r['exception']['message'][:120]}"
    else:
        res = r.get("status", "?")
    out = (r.get("stdout") or "").strip()
    after = [show(a) for a in (r.get("args_after") or []) if a is not None] if "python" not in c else []
    return f"{call_text(c)} -> {res}" + (f"   (prints {out[:80]!r})" if out else "") + \
        (f"   (arguments afterwards: {', '.join(after)[:120]})" if after else "")


class Translator(Agent):
    name = "translator"
    max_tokens = 3600

    def system(self, job):
        src = "Python 2" if self.py2 else self.source.name
        return translator_system(self.pair, src, "Python 2.7" if self.py2 else "Python 3.11")

    def target_outlines(self, job):
        out, seen = [], set()
        for f in job.fragments:
            path = self.sk.location(f["id"])["file"]
            if path in seen:
                continue
            seen.add(path)
            try:
                text = self.hub.call("workspace", "outline_file", {"path": path}, who=self.name)
            except ToolCallError:
                text = "(the file does not exist yet)"
            out.append(f"--- {path}\n{text.strip()}")
        return "\n".join(out)

    def _class_notes(self, f, loc):
        fid, T = f["id"], self.target
        src = self.sk.src
        notes = [f"Your declaration must declare {loc['name']}" + (" (a static nested class)"
                                                                  if self.java and loc.get("slot") else "")]
        own = [k for k in self.frag if src.class_of(k) == fid and src.is_constructor(k)]
        if own:
            notes.append(f"Its constructor is the fragment {own[0]}" + (" (and its overloads)" if len(own) > 1 else "")
                         + ": declare constructors only there, never in this class fragment's code.")
        else:
            init, params = self.sk.class_constructor(fid, inherited_only=True)
            if init and T.id not in INHERITS_CONSTRUCTORS:
                sig = ", ".join(p["name"] + (f"={p['default']}" if p.get("default") else "") for p in params)
                notes.append(f"This class has no constructor of its own: it inherits {init}({sig}). {T.name} does "
                             "not inherit constructors, so declare one with those parameters that calls the base's.")
        return "\n".join(notes)

    def _overload_notes(self, f, loc):
        """Targets without overloading: each overload of a source name is a separate function."""
        fid = f["id"]
        if self.splice or self.target.id in ("java", "csharp", "cpp"):
            return ""
        base = OVERLOAD.sub("", fid)
        group = sorted(k for k in self.frag if OVERLOAD.sub("", k) == base)
        if len(group) < 2:
            return ""
        sigs = "; ".join(f"{self.sk.location(k)['member']}: {(self.frag[k].get('signature') or {}).get('text', '')}"
                         for k in group)
        if fid == base:
            return (f"The source overloads this name ({sigs}). {self.target.name} has no overloading: the other "
                    f"overloads are translated as the separate functions named there. Write `{loc['member']}` so "
                    "that it also accepts their argument lists and dispatches to them (by argument count and type).")
        return f"This is one overload of a source name ({sigs}); here it is the separate function `{loc['member']}`."

    def fragment_block(self, f, contract):
        loc, c = self.sk.location(f["id"]), contract_for(contract, f["id"])
        keep = {k: c[k] for k in CONTRACT_KEYS if c.get(k)}
        head = f"FRAGMENT {f['id']} ({f['kind']}, {f['file']})\nTARGET: {self.sk.describe(f['id'])}"
        if not self.splice and loc["role"] == "class":
            head += "\n" + self._class_notes(f, loc)
        notes = self._overload_notes(f, loc) if loc.get("member") else ""
        if notes:
            head += "\n" + notes
        return head, numbered_source(f) + ("\nCONTRACT: " + json.dumps(keep, ensure_ascii=False) if keep else "")

    def history(self, job, previous):
        rows = []
        mark = "#" if self.target.id == "python3" else "%" if self.target.id == "matlab" else "//"
        for p in previous[-2:]:
            rows.append(f"Attempt {p['attempt']}: {p['outcome']} - {p['summary']}")
            if p.get("code"):
                rows.append("Its code:\n" + "\n".join(f"{mark} {fid}\n{code}" for fid, code in p["code"].items())[:2500])
        for fb in job.req["feedback"]:
            if not any(p["attempt"] == fb["attempt"] for p in previous):
                rows.append(f"Attempt {fb['attempt']}: {fb['outcome']} - {fb['reason']} {'; '.join(fb['problems'][:3])}")
        return "\n".join(rows)

    def run(self, job, contract, tests, previous):
        sections = [("UNIT", f"{job.unit}, attempt {job.attempt} of {job.req['max_attempts']}: "
                             f"{', '.join(job.ids)}", 0)]
        for f in job.fragments:
            h, body = self.fragment_block(f, contract)
            sections.append((h, body, 0))
        examples = [c for c in tests.get("cases", []) if c["fragment"] in job.ids][:6]
        if examples:
            sections.append(("BEHAVIOUR OF THE LEGACY CODE (test cases your translation must reproduce)",
                             "\n".join(case_line(c) for c in examples), 2))
        if not self.splice:
            sections.append(("TARGET FILES AS THEY STAND (bodies elided)", self.target_outlines(job), 3))
        sections.append(("DEPENDENCIES (already migrated: where they are and their API)",
                         dependencies(job, self.sk, self.target.id, 700, self.source.name), 4))
        hist = self.history(job, previous)
        if hist:
            sections.append(("EARLIER ATTEMPTS FAILED - do not repeat their mistakes", hist, 1))
        if job.req.get("human_guidance"):
            sections.append(("GUIDANCE FROM A HUMAN REVIEWER (follow it)", job.req["human_guidance"], 0))
        user = fit(sections, self.budget(system=self.system(job)))
        answer, meta = self.ask(self.system(job), user, purpose=f"translate {job.unit}",
                                effort="medium" if job.attempt > 1 else "low")
        outputs = self.outputs(answer, job)
        missing = [f["id"] for f in job.fragments if f["id"] not in outputs]
        if missing:
            again, m2 = self.ask(self.system(job), user + f"\n\nYour answer had no entry for {missing}. Return all "
                                                          f"{len(job.ids)} fragments.", purpose=f"translate {job.unit} (missing)")
            outputs.update({k: v for k, v in self.outputs(again, job).items() if k not in outputs})
            meta["calls"] += m2["calls"]
        return outputs, meta

    @staticmethod
    def outputs(answer, job):
        got = {}
        for x in answer.get("fragments") or []:
            if isinstance(x, dict) and x.get("id") in job.ids:
                got[x["id"]] = {"imports": [str(i) for i in x.get("imports") or []], "code": str(x.get("code") or ""),
                                "declaration": x.get("declaration")}
        return got

    def pieces(self, job, outputs):
        return {f["id"]: P.build(self.sk, f, outputs.get(f["id"], {"code": ""}), self.java) for f in job.fragments}
