"""Test-Generation agent: synthesizes fine-grained differential unit tests for the unit's callable fragments.

It aims at control-flow boundaries (each branch condition Layer 0 found, both ways), data-flow and type boundaries
(empty, zero, negative, large, null, str vs bytes, integer limits) and state mutations (methods run on a fresh
instance; the object's state afterwards is compared). Its cases are validated on the legacy code through the
acm-sandbox MCP server before anything is translated, in the source language's own harness (runner.py for
Python, sandbox/polyglot.py for the others):
  - each case runs twice; a case whose two runs differ (time, randomness) is dropped as nondeterministic
  - a case that calls the code wrongly (arity, unknown names), that the harness cannot express in the source
    language (e.g. a struct argument), or that crashes the harness is dropped
  - Python sources: executed lines are traced; if statements of the fragment were never executed, the agent
    gets one more round with those lines (coverage-guided), once
The validated cases, with the legacy results as the oracle, are kept for every attempt at the unit. Each case
carries the legacy call (`python` for runner.py, else `source_call`) and the translation's call (`java` for the
Java harness, `target_call` for any other target; Python -> Python reuses `python`).
"""
import ast
import json
import os
import re
import textwrap

from layer2.prompts import testgen_system
from layer2.skeleton import positional
from layer2.values import ValueProblem, show, tagged
from sandbox.compare import compare_results

from .analyzer import contract_for
from .base import Agent, fit
from .context import dependencies, facts, numbered_source

MAX_CASES = 10
COVERAGE_ROUNDS = int(os.environ.get("ACM_L2_COVERAGE_ROUNDS", "1"))
WRONG_CALL = re.compile(r"(takes|got) .*(positional|argument)|unexpected keyword|missing \d+ required|"
                        r"__init__\(\) (takes|got|missing)|not callable|object is not subscriptable", re.I)
INVALID = ("NameError", "ImportError", "ModuleNotFoundError", "SyntaxError", "UnboundLocalError", "IndentationError")
NOT_RUN = ("crash", "timeout", "unresolved", "untestable")
NO_DEFAULTS = ("go", "rust", "c")  # targets without default arguments (Java has overloads for them)


def with_defaults(params, args):
    """Positional arguments completed with the parameters' literal defaults. -> (args, problem or None)."""
    from layer2.skeleton import literal
    out = list(args)
    for p in params[len(out):]:
        if p.get("kind") in ("var_positional", "var_keyword"):
            break
        if p.get("default") is None:
            return out, f"missing argument {p['name']}"
        try:
            literal(p["default"])
        except (ValueError, SyntaxError):
            return out, f"parameter {p['name']} has a non-literal default ({p['default']})"
        out.append({"$literal": p["default"]})
    return out, None


def statement_lines(f):
    """Lines of the fragment that hold a statement (from Python's own parser when the code parses as Python 3,
    else non-blank, non-comment lines)."""
    lines = set()
    for seg, part in zip(f["segments"], f["source"].split("# ...\n")):
        try:
            tree = ast.parse(textwrap.dedent(part))
            body = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)
                    and not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant)
                             and isinstance(n.value.value, str))]
            lines |= {seg["start_line"] + n.lineno - 1 for n in body
                      if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        except SyntaxError:
            for k, line in enumerate(part.splitlines()):
                s = line.strip()
                if s and not s.startswith(("#", "@", "def ", "class ", '"""', "'''", "else:", "try:", "finally:")):
                    lines.add(seg["start_line"] + k)
    return lines


def _params_text(params):
    return ", ".join(p["name"] + (f"={p['default']}" if p.get("default") else "") +
                     (" (*args)" if p.get("kind") == "var_positional" else
                      " (**kwargs)" if p.get("kind") == "var_keyword" else "") for p in params)


class TestGenerator(Agent):
    name = "testgen"
    max_tokens = 2600

    def __init__(self, llm, hub, skeleton, bundle, legacy_dir, legacy_image):
        super().__init__(llm, hub, skeleton, bundle)
        self.legacy_dir, self.legacy_image = str(legacy_dir), legacy_image
        self.src = skeleton.src
        self.roots = {m.split(".")[0] for m in self.frag.values() for m in [m["module"]]}

    def run(self, job, contract):
        cached = job.load_store("tests.json")
        if cached:
            return cached, {"cached": True}
        tests = {"cases": [], "dropped": [], "coverage": {}, "untestable": {}}
        meta = {"calls": 0, "tool_calls": [], "models": []}
        for f in job.fragments:
            if not self.sk.testable(f["id"]):
                tests["untestable"][f["id"]] = f"{f['kind']}: nothing to call (compile check only)"
                continue
            why = self._receiver_problem(job, f["id"])
            if why:
                tests["untestable"][f["id"]] = why
                continue
            self._fragment(job, f, contract, tests, meta)
        job.save_store("tests.json", tests)
        return tests, meta

    def _receiver_problem(self, job, fid):
        """A method is called on a fresh object: its class's constructor must be migrated (or in this unit)."""
        if self.splice or self.src.role(fid) not in ("method", "property"):
            return None
        cls = self.src.class_of(fid)
        init_id, _ = self.src.constructor(cls) if cls else (None, [])
        if init_id and init_id not in job.ids and \
                not self.hub.call("workspace", "get_pieces", {"fragment_id": init_id}, who=self.name):
            return f"its class's constructor ({init_id}) is not migrated yet"
        if init_id and self.target.id in ("go", "rust") and self.src.class_of(init_id) != cls:
            return f"its class inherits its constructor ({init_id}); {self.target.name} has no inherited constructors"
        return None

    # ------------------------------------------------------------------------------------------------ prompts
    def _system(self, job, n):
        return testgen_system(self.pair, job.req["migration"]["regime"], n)

    def _call_text(self, fid):
        if self.python_source:
            return self._python_call_text(fid)
        f, role = self.frag[fid], self.src.role(fid)
        cls = self.src.class_of(fid)
        cname = self.frag[cls]["qualname"] if cls else None
        params = _params_text(self.src.params(fid))
        sig = (f.get("signature") or {}).get("text", "")
        if role == "constructor":
            text = f"new {cname}({params})   (constructs an instance: its arguments go in \"args\", no \"init\")"
        elif role in ("method", "property"):
            _, cparams = self.src.constructor(cls) if cls else (None, [])
            text = (f"{cname}({_params_text(cparams)}) then .{self.src.call(fid)['member']}" +
                    ("" if role == "property" else f"({params})") + "   (\"init\" holds the constructor's arguments)")
        elif cname:
            text = f"{cname}.{self.src.call(fid)['member']}({params})   (static)"
        else:
            text = f"{self.src.call(fid)['member']}({params})"
        return text + (f"\n{self.source.name} signature: {sig}" if sig else "")

    def _python_call_text(self, fid):
        pc = self.sk.python_call(fid)
        mod, call = pc["module"], pc["callable"]
        params = _params_text(pc["params"])
        if not pc["needs_init"]:
            return f"from {mod} import ...;  {call}({params})" + (
                "   (constructs an instance: its arguments go in \"args\"/\"kwargs\", no \"init\")"
                if pc["kind"] == "constructor" else "")
        cls = call.rsplit(".", 1)[0]
        _, cparams = self.sk.constructor_params(fid)
        if pc["kind"] == "property":
            return f"{cls}({_params_text(cparams)}).{call.rsplit('.', 1)[1]}   (a property: give init, no args)"
        return f"{cls}({_params_text(cparams)}).{call.rsplit('.', 1)[1]}({params})"

    def _user(self, job, f, contract, extra=""):
        c = {k: v for k, v in contract_for(contract, f["id"]).items() if k in ("params", "returns", "raises",
                                                                               "side_effects", "hazards")}
        sections = [(f"FRAGMENT {f['id']} ({f['kind']}, {f['file']})", numbered_source(f), 0),
                    ("HOW A CASE CALLS IT", self._call_text(f["id"]), 0),
                    ("LAYER 0 FACTS", facts(f), 1),
                    ("CONTRACT (Analyzer)", json.dumps(c, ensure_ascii=False), 2),
                    ("DEPENDENCIES", dependencies(job, self.sk, self.target.id, 200, self.source.name), 4),
                    ("", extra, 0)]
        return fit(sections, self.budget(system=self._system(job, "6 to 10")))

    # ------------------------------------------------------------------------------------------------ cases
    def _build(self, f, raw, start):
        """Agent cases -> harness cases for both sides. Returns (cases, dropped)."""
        fid = f["id"]
        role = self.src.role(fid)
        receiver = role in ("method", "property")
        cparams = self.sk.constructor_params(fid)[1] if receiver else []
        pc = self.sk.python_call(fid) if self.python_source else None
        tag = lambda v: tagged(v, self.py2)  # noqa: E731
        cases, dropped = [], []
        for k, c in enumerate(raw[:MAX_CASES + 4]):
            cid = f"{fid}#{start + k + 1}"
            try:
                if not isinstance(c, dict):
                    raise ValueProblem("a case must be an object")
                args = c.get("args") or []
                kwargs = c.get("kwargs") or {}
                init = c.get("init") or {}
                if role == "constructor" and not args and not kwargs and isinstance(init, dict):
                    args, kwargs = init.get("args") or [], init.get("kwargs") or {}  # the constructor's own arguments
                if not isinstance(args, list) or not isinstance(kwargs, dict) or not isinstance(init, dict):
                    raise ValueProblem("args is a list, kwargs and init are objects")
                if role == "property":
                    args, kwargs = [], {}
                iargs, ikw = init.get("args") or [], init.get("kwargs") or {}
                case = {"case_id": cid, "fragment": fid, "purpose": str(c.get("purpose", ""))[:200]}
                if self.python_source:
                    py = {"case_id": cid, "module": pc["module"], "callable": pc["callable"],
                          "args": [tag(a) for a in args], "kwargs": {k2: tag(v) for k2, v in kwargs.items()},
                          "trace": {"file": f["file"]}}
                    if pc.get("attr"):
                        py["attr"] = True
                    if pc["needs_init"]:
                        py["init"] = {"args": [tag(a) for a in iargs], "kwargs": {k2: tag(v) for k2, v in ikw.items()}}
                    case["python"] = py
                    if self.splice:
                        cases.append(case)
                        continue
                    tinit = []
                    if receiver:
                        ipos, problem = positional(cparams, iargs, ikw)
                        if problem:
                            raise ValueProblem(f"constructor call: {problem}")
                        tinit = [tag(a) for a in ipos]
                    tpos, problem = positional(pc["params"], args, kwargs)
                    if problem:
                        raise ValueProblem(problem)
                    if self.target.id in NO_DEFAULTS:  # the call spells out the defaults the source relied on
                        tpos, problem = with_defaults(pc["params"], tpos)
                        if problem:
                            raise ValueProblem(problem)
                    targs = [tag(a) for a in tpos]
                else:
                    if kwargs or ikw:
                        raise ValueProblem(f"keyword arguments do not exist in {self.source.name}")
                    targs, tinit = [tag(a) for a in args], [tag(a) for a in iargs]
                    case["source_call"] = {**self.src.call(fid), "args": targs}
                    if receiver:
                        case["source_call"]["init"] = tinit
                if self.java:
                    case["java"] = self.sk.java_call(fid, targs, tinit)
                else:
                    case["target_call"] = self.sk.target_call(fid, targs, tinit)
                cases.append(case)
            except (ValueProblem, TypeError, AttributeError) as e:
                dropped.append({"case_id": cid, "reason": f"not expressible: {e}", "case": c})
        return cases, dropped

    def _run_legacy(self, job, cases, tag):
        """Each case twice on the legacy code -> {case_id: result} (reruns as <id>~rerun)."""
        if self.python_source:
            runs = [c["python"] for c in cases] + [{**c["python"], "case_id": c["case_id"] + "~rerun"} for c in cases]
            res = self.hub.call("sandbox", "run_python_cases", {
                "code_dir": self.legacy_dir, "cases": runs, "image": self.legacy_image,
                "log_dir": str(job.store / "legacy"), "log_name": tag}, who=self.name)
            return {r["case_id"]: r for r in res}, None
        runs = [{"case_id": c["case_id"], "call": c["source_call"]} for c in cases]
        runs += [{"case_id": c["case_id"] + "~rerun", "call": c["source_call"]} for c in cases]
        files = sorted({self.frag[c["fragment"]]["file"] for c in cases})
        r = self.hub.call("sandbox", "run_cases", {
            "language": self.source.id, "code_dir": self.legacy_dir, "cases": runs, "sources": files,
            "log_dir": str(job.store / "legacy"), "log_name": tag}, who=self.name)
        problem = None if r["built"] else "the legacy code does not build in the sandbox: " + "; ".join(
            f"{e.get('file')}:{e.get('line')}: {e.get('message')}" for e in r.get("errors", [])[:2])
        return {x["case_id"]: x for x in r["results"]}, problem

    def _legacy(self, job, cases, tag):
        """Run each case twice on the legacy code; returns (valid cases with their result, dropped)."""
        if not cases:
            return [], []
        by, build_problem = self._run_legacy(job, cases, tag)
        valid, dropped = [], []
        for c in cases:
            r, again = by.get(c["case_id"]), by.get(c["case_id"] + "~rerun")
            ex = (r or {}).get("exception") or {}
            reason = None
            if build_problem:
                reason = build_problem[:300]
            elif not r or r["status"] in NOT_RUN:
                reason = f"legacy run {r['status'] if r else 'missing'}" + (f": {r.get('message', '')[:160]}"
                                                                            if r and r.get("message") else "")
            elif self.python_source and (ex.get("type") in INVALID or (
                    ex.get("type") == "TypeError" and WRONG_CALL.search(ex.get("message", "")))):
                reason = f"wrong call: {ex['type']}: {ex.get('message', '')[:160]}"
            elif again and not compare_results(r, {**again, "case_id": r["case_id"]}, "strict")["equivalent"]:
                reason = "nondeterministic: two legacy runs differ"
            if reason:
                dropped.append({"case_id": c["case_id"], "reason": reason, "purpose": c["purpose"]})
            else:
                if ex and self.python_source:  # an exception of neither the language nor the project (e.g.
                    mod = ex.get("module") or "builtins"  # struct.error): no target equivalent
                    ex["foreign"] = mod not in ("builtins", "exceptions") and mod.split(".")[0] not in self.roots
                valid.append({**c, "legacy": r})
        return valid, dropped

    def _fragment(self, job, f, contract, tests, meta):
        answer, m = self.ask(self._system(job, "6 to 10"), self._user(job, f, contract), purpose=f"tests {f['id']}")
        meta["calls"] += m["calls"]
        meta["models"] += m["models"]
        built, dropped = self._build(f, answer.get("cases") or [], 0)
        short = re.sub(r"\W", "_", f["id"].rsplit(".", 1)[-1])
        valid, d2 = self._legacy(job, built, f"legacy-{job.attempt}-{short}")
        dropped += d2
        rounds = 0
        if self.python_source:  # only runner.py traces executed lines
            want = statement_lines(f)
            covered = set().union(*[set(c["legacy"].get("lines") or []) for c in valid]) & want if valid else set()
            while COVERAGE_ROUNDS > rounds and want - covered:
                rounds += 1
                lines_by_no = {}
                for seg, part in zip(f["segments"], f["source"].split("# ...\n")):
                    for k, line in enumerate(part.splitlines()):
                        lines_by_no[seg["start_line"] + k] = line
                missing = "\n".join(f"{n:>5}| {lines_by_no.get(n, '')}" for n in sorted(want - covered)[:25])
                so_far = "\n".join(f"- {c['purpose'] or c['case_id']}: " + ", ".join(show(a) for a in c["python"]["args"])
                                   for c in valid[:12])
                extra = (f"CASES SO FAR\n{so_far or '(none were valid)'}\n\nThese statement lines were NEVER executed "
                         f"by the cases so far:\n{missing}\nWrite 1 to 4 more cases that execute them (skip lines no "
                         "JSON input can reach).")
                answer, m = self.ask(self._system(job, "1 to 4"), self._user(job, f, contract, extra),
                                     purpose=f"coverage {f['id']}")
                meta["calls"] += m["calls"]
                more, d3 = self._build(f, answer.get("cases") or [], len(built))
                built += more
                v2, d4 = self._legacy(job, more, f"legacy-{job.attempt}-{short}-cov{rounds}")
                valid += v2
                dropped += d3 + d4
                covered |= set().union(*[set(c["legacy"].get("lines") or []) for c in v2]) & want if v2 else set()
            tests["coverage"][f["id"]] = {"statements": len(want), "executed": len(covered),
                                          "percent": round(100 * len(covered) / len(want)) if want else 100,
                                          "never_executed": sorted(want - covered), "coverage_rounds": rounds}
        tests["cases"] += valid[:MAX_CASES + 4]
        tests["dropped"] += dropped
