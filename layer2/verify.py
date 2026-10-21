"""Known-answer verification of Layer 2 (python -m layer2 --verify), integrated with Layer 0 and Layer 1.

Everything is real except the language model: Layer 0 builds its fixture bundle (a Python 2 package), Layer 1
orchestrates it, and Layer 2's agents reach the MCP servers (analysis, workspace, sandbox) and the Docker sandbox
(Python 2.7 for the legacy side, javac + the Java harness for the translation). The LLM is scripted, keyed by agent
and fragment, so every outcome is known in advance:

  double        correct overloads (long, double); a generated case with the wrong arity is dropped
  is_even/odd   a cycle, translated together, correct
  fact          1st translation does not compile (localized to fact); the 1st repair compiles but overflows
                `long` on fact(25) (caught as inequivalent); the 2nd repair uses BigInteger and passes
  report        always formats `v / 2` as 2.0 (Python 2 floors): inequivalent, escalated after 3 attempts, with the
                failure fed back into attempts 2 and 3. Its first tests miss the `raise`: one coverage round adds it
  Square.area   `/ 2.0` (true division): inequivalent (9 vs 9.0), diagnosed, repaired with floorDiv
  Shape.area    NotImplementedError ~ UnsupportedOperationException;  lookup: KeyError ~ NoSuchElementException
  describe      a Python 2 str result matches a Java String (a hazard note, not a difference)
  analyzer      for Square.area it calls the MCP tool analysis.get_dependencies before answering
A second run exhausts the LLM's quota midway: the run stops (resumable), and --resume ends exactly like the first.
Also: the cross-language comparator on known pairs, and the MCP servers' path roots and error reporting.
"""
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

from layer0 import __main__ as layer0
from layer0 import contract as bundle_contract
from layer1 import contract as run_contract
from layer1 import report
from layer1.build_check import build_check
from layer1.orchestrator import migrate
from layer2.agentic import AgentLayer2
from layer2.llm import CHAINS, QuotaExhausted
from layer2.mcp_client import ToolCallError
from sandbox.compare import compare_results
from sandbox.logs import write_json, write_text

J = json.dumps


def frag(fid, code, **kw):
    return {"id": fid, "code": code, **kw}


TRANSLATE = {
    "pkg::header": [{"fragments": [frag("pkg::header", "")]}],
    "pkg.shapes::header": [{"fragments": [frag("pkg.shapes::header", "")]}],
    "pkg.util::header": [{"fragments": [frag("pkg.util::header", "public static final long SCALE = 2;")]}],
    "pkg.shapes.Shape": [{"fragments": [
        frag("pkg.shapes.Shape", "public static long sides = 0;\nprotected long size;", declaration="public class Shape"),
        frag("pkg.shapes.Shape.__init__", "public Shape(long size) {\n    this.size = size;\n}")]}],
    "pkg.util.double": [{"fragments": [frag("pkg.util.double",
        "public static long double_(long x) {\n    return x * SCALE;\n}\n\n"
        "public static double double_(double x) {\n    return x * SCALE;\n}")]}],
    "pkg.util.is_even": [{"fragments": [
        frag("pkg.util.is_even", "public static boolean isEven(long n) {\n    if (n == 0) {\n        return true;\n    }\n"
                                 "    return isOdd(n - 1);\n}"),
        frag("pkg.util.is_odd", "public static boolean isOdd(long n) {\n    if (n == 0) {\n        return false;\n    }\n"
                                "    return isEven(n - 1);\n}")]}],
    "pkg.util.fact": [{"fragments": [frag("pkg.util.fact",  # missing semicolon: does not compile
        "public static long fact(long n) {\n    return n <= 1 ? 1 : n * fact(n - 1)\n}")]}],
    "pkg.util.report": [{"fragments": [frag("pkg.util.report",  # true division: 4 -> "2.0", Python 2 prints 2
        "public static String report(Map<String, Long> d) {\n    if (d == null || d.isEmpty()) {\n"
        "        throw new IllegalArgumentException(\"empty\");\n    }\n    StringBuilder out = new StringBuilder();\n"
        "    for (Map.Entry<String, Long> e : d.entrySet()) {\n"
        "        out.append(e.getKey()).append(\"=\").append(e.getValue() / 2.0).append(\"\\n\");\n    }\n"
        "    return out.toString();\n}", imports=["java.util.Map"])]}],
    "pkg.shapes.Shape.area": [{"fragments": [frag("pkg.shapes.Shape.area",
        "public Number area() {\n    throw new UnsupportedOperationException();\n}")]}],
    "pkg.shapes.Square": [{"fragments": [frag("pkg.shapes.Square", "public static long sides = 4;\n\n"
                                              "public Square(long size) {\n    super(size);\n}",
                                              declaration="public class Square extends Shape")]}],
    "pkg.shapes.Square.area": [{"fragments": [frag("pkg.shapes.Square.area",  # true division
        "public Number area() {\n    return Util.double_(this.size) * this.size / 2.0;\n}")]}],
    "pkg.shapes.Cube": [{"fragments": [frag("pkg.shapes.Cube", "", declaration="public class Cube extends Square"),
                                       frag("pkg.shapes.Cube.__init__", "public Cube(long size) {\n    super(size);\n}")]}],
    "pkg.shapes.make": [{"fragments": [frag("pkg.shapes.make",
        "public static Square make(long size) {\n    return new Square(size);\n}")]}],
    "pkg.shapes.Square.describe": [{"fragments": [frag("pkg.shapes.Square.describe",
        "public String describe() {\n    return \"square \" + area().longValue();\n}")]}],
    "pkg.shapes::init": [{"fragments": [frag("pkg.shapes::init",
        "public static final Map<String, LongFunction<Square>> REGISTRY = Map.of(\"square\", ShapesModule::make);",
        imports=["java.util.Map", "java.util.function.LongFunction"])]}],
    "pkg.shapes.lookup": [{"fragments": [frag("pkg.shapes.lookup",
        "public static Square lookup(String name) {\n    LongFunction<Square> f = REGISTRY.get(name);\n"
        "    if (f == null) {\n        throw new NoSuchElementException(name);\n    }\n    return f.apply(1);\n}",
        imports=["java.util.NoSuchElementException", "java.util.function.LongFunction"])]}],
}
REPAIR = {
    "pkg.util.fact": [{"fragments": [frag("pkg.util.fact",  # compiles, still `long`: fact(25) overflows
                                          "public static long fact(long n) {\n    return n <= 1 ? 1 : n * fact(n - 1);\n}")]},
                      {"fragments": [frag("pkg.util.fact",
                                          "public static BigInteger fact(long n) {\n    return n <= 1 ? BigInteger.ONE : "
                                          "BigInteger.valueOf(n).multiply(fact(n - 1));\n}",
                                          imports=["java.math.BigInteger"])]}],
    "pkg.shapes.Square.area": [{"fragments": [frag("pkg.shapes.Square.area",
        "public Number area() {\n    return Math.floorDiv(Util.double_(this.size) * this.size, 2L);\n}")]}],
    "pkg.util.report": [{"fragments": []}],
}
DIAGNOSE = {"diagnosis": "Python 2 `/` on ints floors; the translation divides as floating point.",
            "suspects": [{"fragment": "?", "line": 2, "why": "true division"}], "fix": "use floor division"}
TESTS = {
    "pkg.util.double": [{"args": [5]}, {"args": [0]}, {"args": [-3]}, {"args": [2.5]}, {"args": [1000000000]},
                        {"purpose": "wrong arity", "args": [1, 2]}],
    "pkg.util.is_even": [{"args": [0]}, {"args": [1]}, {"args": [4]}, {"args": [7]}],
    "pkg.util.is_odd": [{"args": [0]}, {"args": [3]}],
    "pkg.util.fact": [{"args": [0]}, {"args": [1]}, {"args": [5]}, {"args": [25]}],
    "pkg.util.report": [{"args": [{"a": 4}]}],
    "pkg.util.report#coverage": [{"purpose": "empty dict raises", "args": [{}]}],
    "pkg.shapes.Shape.__init__": [{"args": [3]}, {"args": [0]}],
    "pkg.shapes.Shape.area": [{"init": {"args": [2]}}],
    "pkg.shapes.Square.area": [{"init": {"args": [3]}}, {"init": {"args": [4]}}, {"init": {"args": [0]}}],
    "pkg.shapes.Square.describe": [{"init": {"args": [3]}}],
    "pkg.shapes.Cube.__init__": [{"args": [2]}],
    "pkg.shapes.make": [{"args": [3]}],
    "pkg.shapes.lookup": [{"args": ["square"]}, {"args": ["circle"]}],
}


class ScriptedLLM:
    """Stands in for Groq. Answers each agent from a script keyed by the fragment the prompt is about; records
    every call; can run out of quota after a number of calls."""
    provider = "scripted"

    def __init__(self, quota_after=None):
        self.pacer, self.usage, self.calls, self.quota_after = SimpleNamespace(tpm=8000), {}, [], quota_after
        self.used = {}

    def describe(self):
        return {"provider": "scripted", "chains": {a: ["scripted"] for a in CHAINS}, "tokens_per_minute_per_model": 8000}

    def _next(self, table, key):
        seq = table.get(key)
        if seq is None:
            return None
        n = self.used.get((id(table), key), 0)
        self.used[(id(table), key)] = n + 1
        return seq[min(n, len(seq) - 1)]

    def chat(self, agent, messages, tools=None, json_mode=True, max_tokens=0, effort="low", purpose="",
             tool_choice="auto"):
        if self.quota_after is not None and len(self.calls) >= self.quota_after:
            raise QuotaExhausted("scripted: daily quota exhausted")
        self.calls.append({"agent": agent, "purpose": purpose, "messages": messages, "tools": bool(tools)})
        u = self.usage.setdefault(agent, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        u["calls"] += 1
        user = messages[1]["content"]
        ids = re.findall(r"^(?:FRAGMENT|CURRENT TRANSLATION OF|LEGACY) (\S+)", user, re.M)
        key = ids[0] if ids else ""
        out = {"tool_calls": [], "model": "scripted", "usage": {}, "seconds": 0.0, "finish_reason": "stop"}
        if agent == "analyzer":
            if key == "pkg.shapes.Square.area" and tools and tool_choice == "auto" and len(messages) == 2:
                out.update(content="", tool_calls=[{"id": "call_1", "type": "function", "function": {
                    "name": "analysis__get_dependencies", "arguments": J({"fragment_id": key})}}])
                return out
            answer = {"fragments": [{"id": i, "purpose": "scripted", "hazards": []} for i in dict.fromkeys(ids)]}
        elif agent == "testgen":
            answer = {"cases": TESTS.get(key + ("#coverage" if purpose.startswith("coverage") else ""), [])}
        elif agent == "translator":
            answer = self._next(TRANSLATE, key) or {"fragments": []}
        elif agent == "repair":
            answer = self._next(REPAIR, key) or {"fragments": []}
        else:
            answer = DIAGNOSE
        out["content"] = J(answer)
        return out


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


class Run:
    def __init__(self, run, name, bundle_dir, src, llm, resume=False, layer2=None):
        self.out, self.bundle = run / name, bundle_contract.load(bundle_dir)
        self.llm = llm
        self.l2 = layer2 or AgentLayer2(self.bundle, bundle_dir, src, self.out, llm=llm)
        self.stopped = None
        try:
            migrate(bundle_dir, src, self.out, self.l2, verbose=False, resume=resume)
        except QuotaExhausted as e:
            self.stopped = str(e)
        finally:
            self.l2.close()
        if self.stopped:
            return
        self.st = _read(self.out / "state.json")
        self.val = run_contract.validate(self.out, bundle_dir)
        self.build = build_check(self.out / "migrated", "java", self.out / "build_check")
        self.ev = report.layer2_evidence(self.out, self.st)
        write_json(self.out / "layer2_summary.json", self.ev)
        write_text(self.out / "report.md", report.render(self.out, self.val, self.build, "agents (scripted LLM)",
                                                         None, None, self.ev))
        self.unit_of = {f: u["unit"] for u in self.st["units"] for f in u["fragments"]}
        self.units = {u["unit"]: u for u in self.st["units"]}

    def u(self, fid):
        return self.units[self.unit_of[fid]]

    def l2file(self, fid, name, attempt=1):
        p = self.out / "units" / self.unit_of[fid] / f"attempt-{attempt}" / "layer2" / name
        return _read(p) if p.exists() else None

    def verdict(self, fid, attempt=1):
        return _read(self.out / "units" / self.unit_of[fid] / f"attempt-{attempt}" / "verdict.json")


def run_verify(run, echo):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        echo(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    echo("== Layer 0: fixture bundle (Python 2 package, Java target)")
    (run / "layer0").mkdir(parents=True)
    s0 = layer0.verify(run / "layer0")
    bj, src = run / "layer0" / "verify" / "bundle", run / "layer0" / "verify" / "fixture" / "src"
    check("Layer 0 builds the fixture bundle", s0["ok"], f"{len(s0['fixture_checks'])} fixture + "
                                                         f"{len(s0['contract_checks'])} contract checks")
    if not s0["ok"]:
        return _finish(run, checks, echo)

    echo("\n== Layer 1 + Layer 2 agents (scripted LLM, real MCP servers and Docker sandbox)")
    a = Run(run, "agents", bj, src, ScriptedLLM())
    st = a.st["result"]["units_by_status"]
    n = len(a.st["units"])
    check("every unit migrated except `report`, escalated after 3 attempts (nothing depends on it)",
          st == {"migrated": n - 1, "escalated": 1} and a.u("pkg.util.report")["status"] == "escalated"
          and a.u("pkg.util.report")["attempts"] == 3, f"{n} units: {st}")
    ok_val = all(x["ok"] for x in a.val)
    check("Layer 1 run contract and the javac build of the migrated codebase", ok_val and a.build["ok"],
          f"contract {sum(x['ok'] for x in a.val)}/{len(a.val)}" + "".join(f"; FAIL {x['check']}: {x['detail']}"
                                                                          for x in a.val if not x["ok"])
          + f"; build {'ok' if a.build['ok'] else a.build['errors'][:2]} ({a.build['files']} files)")

    f0, f1 = a.l2file("pkg.util.fact", "failure-0.json"), a.l2file("pkg.util.fact", "failure-1.json")
    vf = a.verdict("pkg.util.fact")
    check("fact: the compile error is localized to `fact` with javac's message",
          f0 and f0["kind"] == "compile_error" and f0["compile_errors"][0]["fragment"] == "pkg.util.fact"
          and "expected" in f0["compile_errors"][0]["message"],
          f"{f0['compile_errors'][0]['fragment']} line {f0['compile_errors'][0]['line']}: "
          f"{f0['compile_errors'][0]['message']}" if f0 else "no failure record")
    over = [c for c in (f1 or {}).get("failing", []) if c["call"] == "fact(25)"]
    check("fact: the repaired code compiles but overflows `long` on fact(25); the next repair (BigInteger) passes",
          f1 and f1["kind"] == "inequivalent" and over and vf["outcome"] == "pass" and a.u("pkg.util.fact")["attempts"] == 1
          and [t["agent"] for t in vf["trace"]].count("repair") == 2 and vf["evidence"]["tests_passed"] == 4,
          (f"fact(25): legacy {over[0]['legacy'][:40]}..., translation {over[0]['translation']}; " if over else "")
          + f"final: {vf['evidence']['tests_passed']}/{vf['evidence']['tests_generated']} after 2 repairs")
    sa0, vsa = a.l2file("pkg.shapes.Square.area", "failure-0.json"), a.verdict("pkg.shapes.Square.area")
    check("Square.area: true division is caught (Python 2 floors: 9 vs 9.0), diagnosed, and repaired once",
          sa0 and sa0["kind"] == "inequivalent" and any("return: type" in d for c in sa0["failing"] for d in c["diffs"])
          and sa0.get("analysis", {}).get("diagnosis") and vsa["outcome"] == "pass"
          and [t["agent"] for t in vsa["trace"]].count("repair") == 1,
          f"{sa0['summary'][:120]}; then {vsa['reason']}" if sa0 else "no failure record")
    r2 = [c for c in a.llm.calls if c["agent"] == "translator" and "FRAGMENT pkg.util.report" in c["messages"][1]["content"]]
    fb = r2[1]["messages"][1]["content"] if len(r2) > 1 else ""
    check("report: inequivalent on every attempt; attempts 2 and 3 are told why the earlier ones failed",
          len(r2) == 3 and "EARLIER ATTEMPTS FAILED" in fb and "2.0" in fb
          and a.verdict("pkg.util.report", 3)["outcome"] == "fail",
          f"{len(r2)} translations; attempt 2's prompt quotes: "
          + (re.search(r"Attempt 1: .{0,140}", fb).group(0) if re.search(r"Attempt 1: .{0,140}", fb) else "?"))
    tr = _read(a.out / "layer2" / "units" / a.unit_of["pkg.util.report"] / "tests.json")
    td = _read(a.out / "layer2" / "units" / a.unit_of["pkg.util.double"] / "tests.json")
    cov = tr["coverage"]["pkg.util.report"]
    check("Test-Gen: an invalid case is dropped on the legacy run; a coverage round reaches the missed `raise`",
          [d["reason"].split(":")[0] for d in td["dropped"]] == ["wrong call"] and cov["coverage_rounds"] == 1
          and cov["percent"] == 100 and len(tr["cases"]) == 2,
          f"double dropped: {[d['reason'][:60] for d in td['dropped']]}; report coverage "
          f"{cov['executed']}/{cov['statements']} after {cov['coverage_rounds']} round")
    vs = {f: a.verdict(f)["evidence"] for f in ("pkg.shapes.Shape.area", "pkg.shapes.lookup", "pkg.shapes.Square.describe",
                                                "pkg.util.double", "pkg.util.is_even")}
    desc = a.l2file("pkg.shapes.Square.describe", "verdict-0.json")
    check("cross-language equivalence: exceptions mapped, Python 2 str ~ Java String, int vs float overloads",
          all(e["tests_generated"] and e["tests_passed"] == e["tests_generated"] for e in vs.values())
          and desc and "byte_string_compared_as_text" in desc["hazard_notes"],
          "; ".join(f"{k.split('.')[-1]} {e['tests_passed']}/{e['tests_generated']}" for k, e in vs.items()))
    mcp = [json.loads(l) for l in (a.out / "layer2" / "mcp_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    tool_msgs = [m for c in a.llm.calls if c["agent"] == "analyzer" for m in c["messages"]
                 if m.get("role") == "tool" or (m.get("role") == "user"
                                                and m["content"].startswith("Results of the tools you called"))]
    by_server = {}
    for m in mcp:
        by_server[m["server"]] = by_server.get(m["server"], 0) + 1
    check("MCP: the Analyzer's tool call went through acm-analysis and its result reached the model",
          any(m["server"] == "analysis" and m["tool"] == "get_dependencies" and m["by"] == "analyzer" for m in mcp)
          and tool_msgs and "pkg.util.double" in tool_msgs[0]["content"],
          f"{len(mcp)} MCP calls: {by_server}; tool result {len(tool_msgs[0]['content']) if tool_msgs else 0} chars")
    failed = [m for m in mcp if not m.get("ok")]
    expected = [m for m in failed if m["tool"] == "outline_file" and "does not exist" in m.get("error", "")]
    check("MCP: every sandbox and workspace action of the agents is an MCP call; none failed unexpectedly",
          by_server.get("sandbox", 0) > 0 and by_server.get("workspace", 0) > 0 and failed == expected,
          f"sandbox {by_server.get('sandbox', 0)}, workspace {by_server.get('workspace', 0)}, analysis "
          f"{by_server.get('analysis', 0)}; {len(expected)} expected errors (outline of a file not created yet)"
          + (f"; UNEXPECTED: {[m['tool'] + ': ' + m.get('error', '')[:80] for m in failed if m not in expected][:3]}"
             if failed != expected else ""))
    ev = a.ev
    check("Layer 1 report carries Layer 2's evidence (verified units, tests, repairs, cost)",
          ev["units_verified_by_tests"] >= 9 and ev["units_repaired"] >= 2 and ev["differential_tests"] > 20
          and "## Layer 2 verification" in (a.out / "report.md").read_text(encoding="utf-8"),
          f"{ev['units_verified_by_tests']} units verified by tests, {ev['units_compiled_only']} compile-only; "
          f"{ev['tests_equivalent']}/{ev['differential_tests']} tests equivalent; {ev['units_repaired']} repaired")

    echo("\n== quota exhausted midway, then --resume")
    q = Run(run, "quota", bj, src, ScriptedLLM(quota_after=14))
    db = q.out / "checkpoints.sqlite"
    check("an exhausted LLM quota stops the run (not a unit failure) with its checkpoint kept",
          q.stopped and db.exists() and not (q.out / "state.json").exists(), q.stopped or "did not stop")
    r = Run(run, "quota", bj, src, ScriptedLLM(), resume=True)
    same = r.st["result"]["units_by_status"] == a.st["result"]["units_by_status"] and \
        {u["unit"]: (u["status"], u["attempts"]) for u in r.st["units"]} == \
        {u["unit"]: (u["status"], u["attempts"]) for u in a.st["units"]}
    check("--resume continues and ends like the uninterrupted run", same and all(x["ok"] for x in r.val),
          f"{r.st['result']['units_by_status']}; contract {sum(x['ok'] for x in r.val)}/{len(r.val)}")

    echo("\n== comparator and MCP servers")
    T = lambda t, v=None: {"t": t, "v": v}  # noqa: E731
    ok = lambda v: {"status": "ok", "value": v, "args_after": [], "self_after": None, "stdout": ""}  # noqa: E731
    exc = lambda t, h: {"status": "exception", "exception": {"type": t, "hierarchy": h, "message": ""},  # noqa: E731
                        "args_after": [], "self_after": None, "stdout": ""}
    pairs = [
        ("tuple ~ Java list", ok(T("tuple", [T("int", "1")])), ok(T("list", [T("int", "1")])), True),
        ("str vs byte[] differ", ok(T("text", "a")), ok(T("bytes", "YQ==")), False),
        ("int vs double differ", ok(T("int", "9")), ok(T("float", 9.0)), False),
        ("ValueError ~ NumberFormatException", exc("ValueError", ["ValueError", "Exception"]),
         exc("NumberFormatException", ["NumberFormatException", "IllegalArgumentException", "RuntimeException"]), True),
        ("project exception keeps its name", exc("BadData", ["BadData", "Exception"]),
         exc("BadData", ["BadData", "RuntimeException"]), True),
        ("KeyError vs ArithmeticException differ", exc("KeyError", ["KeyError", "LookupError", "Exception"]),
         exc("ArithmeticException", ["ArithmeticException", "RuntimeException"]), False),
        ("library exception (struct.error) ~ any unchecked Java exception",
         exc("error", ["error", "Exception"]) | {"exception": {"type": "error", "hierarchy": ["error", "Exception"],
                                                              "message": "", "module": "struct", "foreign": True}},
         exc("IllegalArgumentException", ["IllegalArgumentException", "RuntimeException"]), True),
        ("project exception vs a generic Java one differ", exc("BadData", ["BadData", "Exception"]),
         exc("IllegalArgumentException", ["IllegalArgumentException", "RuntimeException"]), False),
        ("object fields by normalized name", ok({"t": "object", "class": "Signer", "v": T("dict", [[T("text", "_secret_key"), T("int", "1")]])}),
         ok({"t": "object", "class": "Signer", "v": T("dict", [[T("text", "secretKey"), T("int", "1")]])}), True),
    ]
    got = [(name, compare_results({**l, "case_id": "c"}, {**m, "case_id": "c"}, "python-java")["equivalent"] == want)
           for name, l, m, want in pairs]
    check("python-java comparator: known equivalent and inequivalent pairs", all(g for _, g in got),
          "; ".join(f"{n}: {'ok' if g else 'WRONG'}" for n, g in got))
    l2 = AgentLayer2(bundle_contract.load(bj), bj, src, run / "mcp-probe", llm=ScriptedLLM())
    try:
        try:
            l2.hub.call("sandbox", "run_java_cases", {"code_dir": str(Path.home()), "cases": [], "log_dir": str(run)})
            refused = None
        except ToolCallError as e:
            refused = str(e)
        try:
            l2.hub.call("analysis", "get_fragment", {"fragment_id": "pkg.util.nope"})
            err = None
        except ToolCallError as e:
            err = str(e)
        info = l2.hub.info
    finally:
        l2.close()
    check("MCP servers: paths outside their roots are refused; tool errors reach the caller",
          refused and "outside the allowed roots" in refused and err and "no fragment" in err
          and {s["name"] for s in info.values()} == {"acm-analysis", "acm-workspace", "acm-sandbox"},
          f"{refused[:80] if refused else 'NOT refused'}; {err[:80] if err else 'no error'}; protocol "
          f"{next(iter(info.values()))['protocol']}")
    shutil.rmtree(run / "mcp-probe", ignore_errors=True)
    return _finish(run, checks, echo)


def _finish(run, checks, echo):
    ok = all(c["ok"] for c in checks)
    write_json(run / "summary.json", {"kind": "verify", "ok": ok, "checks": checks})
    echo(f"\n{sum(c['ok'] for c in checks)}/{len(checks)} known-answer checks passed\nlogs: {run}")
    return ok
