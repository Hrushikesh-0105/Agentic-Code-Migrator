"""The real Layer 2: multi-agent translation and differential verification of one unit per Layer 1 request.

    Analyzer -> Test-Generation -> Translator -> [Executor & Sandbox -> Verdict -> (Repair -> ...)] -> verdict

  Analyzer        migration contract (types, I/O, exceptions, hazards); cached for the unit's later attempts
  Test-Gen        differential cases, validated on the legacy code (run twice), coverage-guided; cached likewise
  Translator      the unit's code in the target layout; on a later attempt it sees why earlier ones failed
  Executor        candidate tree = committed codebase + this unit; compile; run the cases on both sides
  Verdict         deterministic comparison -> Equivalent / Inequivalent + a structured failure record
  Repair          surgical patches from the failure record; then Executor and Verdict again, up to
                  `max_repairs` rounds per attempt (Layer 1's retries are the outer loop, 3 attempts per unit)
Any language pair of langs/: the target layout comes from layer2/layout.py (Java: layer2/skeleton.py), the
prompts from layer2/prompts.py, the legacy and the translation run in their languages' harnesses
(sandbox/harness/, sandbox/polyglot.py), and sandbox/compare.py compares them across languages.
Outcome for Layer 1: pass = compiles and every differential test is equivalent (a unit with nothing callable must
compile); fail = still inequivalent or not compiling after the repair rounds (the failure record goes back as
evidence and comes back as feedback); needs_human = failed, and the Analyzer said the code cannot be expressed in
the target; error = infrastructure (LLM, MCP, Docker) failed. Every tool is reached through MCP servers
(layer2/servers/); LLM calls and MCP calls are logged under <run>/<repo>/layer2/, and each attempt's agent
outputs under <run>/<repo>/units/<unit>/attempt-<n>/layer2/.
"""
import json
import time
from pathlib import Path

from layer1.llm import LLMError
from layer1.protocol import PROTOCOL, Piece, check_pieces
from layer2.agents import Analyzer, Executor, Repairer, TestGenerator, Translator, VerdictAgent
from layer2.llm import AgentLLM, BadOutput, PromptTooLarge
from layer2.layout import make_skeleton
from layer2.mcp_client import MCPHub, ToolCallError

MAX_REPAIRS = 2


class AgentLayer2:
    name = "agents"

    def __init__(self, bundle, bundle_dir, source_root, out, llm=None, max_repairs=MAX_REPAIRS):
        self.bundle, self.out = bundle, Path(out)
        self.root = self.out / "layer2"
        self.root.mkdir(parents=True, exist_ok=True)
        mig = bundle["manifest"]["migration"]
        self.java = mig["target_language"] == "java"
        self.mode = "splice" if mig["source_language"].startswith("python") and \
            mig["target_language"].startswith("python") else "compose"
        self.sk = make_skeleton(bundle)
        self.max_repairs = max_repairs
        legacy_image = "py27" if mig["source_language"] == "python2" else "py3"  # Python sources (runner.py)
        logs = self.root / "servers"
        self.hub = MCPHub({
            "analysis": ["layer2.servers.analysis", "--bundle", str(bundle_dir), "--log", str(logs / "analysis.log")],
            "workspace": ["layer2.servers.workspace", "--root", str(self.root / "workspace"), "--bundle",
                          str(bundle_dir), "--source", str(source_root), "--log", str(logs / "workspace.log")],
            "sandbox": ["layer2.servers.sandbox", "--allow", str(self.out), "--allow", str(source_root),
                        "--log", str(logs / "sandbox.log")]}, log=self.root / "mcp_calls.jsonl")
        self.llm = llm or AgentLLM(log=self.root / "llm_calls.jsonl")
        args = (self.llm, self.hub, self.sk, bundle)
        self.analyzer, self.translator = Analyzer(*args), Translator(*args)
        self.testgen = TestGenerator(*args, legacy_dir=source_root, legacy_image=legacy_image)
        self.verdict, self.repairer = VerdictAgent(*args), Repairer(*args)
        self.executor = Executor(self.hub, self.sk, bundle)
        (self.root / "config.json").write_text(json.dumps(self.describe(), indent=1), encoding="utf-8")

    def describe(self):
        out = {"layer2": self.name, "llm": self.llm.describe(), "max_repairs_per_attempt": self.max_repairs,
               "mcp_servers": self.hub.info, "comparison_profile": self.executor.profile,
               "regime": self.bundle["manifest"]["migration"]["regime"], "assembly": self.mode,
               "target_layout": type(self.sk).__name__}
        if getattr(self.sk, "exp", None) is not None:
            out["target_expectations"] = self.sk.exp.summary()
        return out

    def close(self):
        self.hub.close()

    # ------------------------------------------------------------------------------------------------ process
    def process(self, req):
        from layer2.agents.context import Job
        job = Job(req, self.out, self.root)
        start, trace = time.time(), []
        usage0 = {k: dict(v) for k, v in self.llm.usage.items()}
        mcp0 = self.hub.calls

        stage = ["layer2"]

        def step(agent, status, t0, detail=""):
            trace.append({"agent": agent, "status": status, "seconds": round(time.time() - t0, 2),
                          "detail": detail[:300]})

        def verdict(outcome, reason, pieces=(), record=None, notes=""):
            ev = {"compiled": record.get("compiled") if record else None, "tests_generated": 0, "tests_passed": 0,
                  "tests_failed": 0, "failing": [], "log_dir": str(job.dir), "notes": notes}
            if record:
                ev.update(tests_generated=record["tests"], tests_passed=record["passed"], tests_failed=record["failed"],
                          failing=[f["case_id"] for f in record["failing"]])
            usage = {f"{a}_{k}": v[k] - usage0.get(a, {}).get(k, 0) for a, v in self.llm.usage.items()
                     for k in ("calls", "prompt_tokens", "completion_tokens")
                     if v[k] - usage0.get(a, {}).get(k, 0)}
            usage.update(mcp_calls=self.hub.calls - mcp0, seconds=round(time.time() - start, 1))
            v = {"protocol": dict(PROTOCOL), "unit": req["unit"], "attempt": req["attempt"], "outcome": outcome,
                 "reason": reason[:600], "translations": [p for ps in pieces for p in ps] if pieces else [],
                 "evidence": ev, "trace": trace, "usage": usage}
            job.save("verdict.json", v)
            return v

        record, outputs, pieces, tests = None, {}, {}, {}
        try:
            t = time.time()
            stage[0] = "analyzer"
            self.hub.call("workspace", "sync_committed", {"dependencies": [
                {"id": d["id"], "status": d["status"], "translations": d["translations"]} for d in req["dependencies"]]},
                who="layer2")
            contract, m = self.analyzer.run(job)
            job.save("contract.json", contract)
            step("analyzer", "cached" if m.get("cached") else "ok", t,
                 f"{sum(len(c.get('hazards', [])) for c in contract['fragments'])} hazards; tools used: "
                 f"{[x['tool'] for x in m.get('tool_calls', [])]}")
            t = time.time()
            stage[0] = "testgen"
            tests, m = self.testgen.run(job, contract)
            mine = [c for c in tests["cases"] if c["fragment"] in job.ids]
            cov = tests.get("coverage", {})
            step("testgen", "cached" if m.get("cached") else "ok", t,
                 f"{len(mine)} valid cases, {len(tests['dropped'])} dropped; coverage " +
                 ", ".join(f"{k.rsplit('.', 1)[-1]} {v['percent']}%" for k, v in cov.items()))
            t = time.time()
            stage[0] = "translator"
            previous = job.load_store("attempts.json") or []
            outputs, m = self.translator.run(job, contract, tests, previous)
            pieces = self.translator.pieces(job, outputs)
            job.save("translation-0.json", {"outputs": outputs, "pieces": pieces, "models": m.get("models")})
            step("translator", "ok", t, f"{len(outputs)}/{len(job.ids)} fragments; model {m.get('models')}")
            record, edit_problems = None, []
            for rnd in range(self.max_repairs + 1):
                t = time.time()
                stage[0] = "executor"
                rep = self.executor.run(job, pieces, tests, f"r{rnd}")
                job.save(f"execution-{rnd}.json", {k: v for k, v in rep.items() if k != "results"})
                step("executor", "compiled" if rep["compiled"] else "compile_error", t,
                     f"round {rnd}: {len(rep['comparisons'])} cases run in {rep['seconds']} s")
                t = time.time()
                stage[0] = "verdict"
                record = self.verdict.judge(job, rep, tests, outputs)
                if edit_problems:
                    record["edit_problems"] = edit_problems
                job.save(f"failure-{rnd}.json" if not record["equivalent"] else f"verdict-{rnd}.json", record)
                step("verdict", "equivalent" if record["equivalent"] else "inequivalent", t, record["summary"])
                if record["equivalent"]:
                    problems = check_pieces([Piece(**p) for ps in pieces.values() for p in ps], job.fragments,
                                            self.mode,
                                            [Piece(**p) for d in req["dependencies"] for p in d["translations"]])
                    if problems:  # Layer 1 would refuse them: treat as a failure to repair
                        record.update(equivalent=False, kind="assembly_problem", assembly_problems=problems,
                                      summary="pieces refused: " + "; ".join(problems[:2]))
                    else:
                        break
                if rnd == self.max_repairs:
                    break
                t = time.time()
                stage[0] = "repair"
                outputs, m = self.repairer.run(job, outputs, record, contract)
                edit_problems = m.get("edit_problems") or []
                pieces = self.translator.pieces(job, outputs)
                job.save(f"translation-{rnd + 1}.json", {"outputs": outputs, "pieces": pieces, "repair": m})
                step("repair", "patched" if m.get("changed") else "no_change", t,
                     f"changed {m.get('changed')}" + (f"; {m['edit_problems'][:2]}" if m.get("edit_problems") else ""))
                if not m.get("changed"):  # the same code would fail the same way
                    break
            notes = self._notes(record, tests, job)
            if record["equivalent"]:
                self.hub.call("workspace", "record_pass", {"pieces": pieces, "unit": job.unit}, who="layer2")
                return verdict("pass", record["summary"], pieces.values(), record, notes)
            previous.append({"attempt": job.attempt, "outcome": record["kind"], "summary": record["summary"],
                             "code": {fid: o.get("code", "")[:1200] for fid, o in outputs.items()}})
            job.save_store("attempts.json", previous)
            human = [c.get("needs_human") for c in contract["fragments"] if c.get("needs_human")]
            if human:
                return verdict("needs_human", f"{record['summary']}; the Analyzer says: {human[0]}", pieces.values(),
                               record, notes)
            return verdict("fail", record["summary"], pieces.values(), record, notes)
        except (LLMError, ToolCallError, PromptTooLarge, BadOutput) as e:
            step(stage[0], "error", start, f"{type(e).__name__}: {e}")
            if stage[0] == "repair" and record and not record.get("equivalent"):
                # the translation was tested and failed; only its repair broke off: a failed attempt whose evidence
                # (and code) the next attempt learns from, not an infrastructure error
                previous = job.load_store("attempts.json") or []
                previous.append({"attempt": job.attempt, "outcome": record["kind"], "summary": record["summary"],
                                 "code": {fid: o.get("code", "")[:1200] for fid, o in outputs.items()}})
                job.save_store("attempts.json", previous)
                return verdict("fail", record["summary"], pieces.values(), record,
                               self._notes(record, tests, job) + f"; repair broke off: {type(e).__name__}")
            return verdict("error", f"{stage[0]}: {type(e).__name__}: {e}",
                           notes="infrastructure or model output failure")

    @staticmethod
    def _notes(record, tests, job):
        cov = [f"{k.rsplit('.', 1)[-1]} {v['executed']}/{v['statements']} statements"
               for k, v in tests.get("coverage", {}).items() if k in job.ids and v.get("statements") is not None]
        dropped = [d for d in tests.get("dropped", []) if d["case_id"].split("#")[0] in job.ids]
        parts = [record["summary"]]
        if cov:
            parts.append("legacy coverage: " + ", ".join(cov))
        if dropped:
            parts.append(f"{len(dropped)} generated case(s) dropped (" + ", ".join(sorted({d['reason'].split(':')[0]
                                                                                       for d in dropped})) + ")")
        if record.get("hazard_notes"):
            parts.append("comparison notes: " + ", ".join(record["hazard_notes"]))
        return "; ".join(parts)
