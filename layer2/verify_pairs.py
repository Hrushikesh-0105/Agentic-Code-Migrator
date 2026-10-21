"""Known-answer verification of Layer 2 across language pairs (python -m layer2 --verify-pairs [SRC->TGT ...]).

The same small API is written in seven source languages (fixtures below): twice(x), safe_div(a, b) (floor
division; an error on b == 0, except in C where it returns 0) and a Counter class (constructor, bump). For each
pair, Layer 0 builds the fixture's bundle, Layer 1 orchestrates it and Layer 2's agents translate it through the
real MCP servers and the Docker sandbox: the legacy code runs in its own language's harness, the translation is
built with the target's toolchain and run in its harness, and sandbox/compare.py compares them across languages.
Only the LLM is scripted (answers keyed by agent and fragment), so every outcome is known in advance:

  every pair       all units migrate; the assembled target builds; every differential test is equivalent
                   (errors matched across languages: ValueError ~ Err(..) ~ error ~ IllegalArgumentException ...)
  python3 -> rust  safe_div first uses `/` (truncates): inequivalent on (-7, 2), repaired to floor division
  c -> python3     safe_div first uses int(a / b) (truncates): inequivalent on (-7, 2), repaired to `//`
  python3 -> go    twice first imports "fmt" without using it: the compile error is caught, then repaired
The 13th benchmark direction (Python -> Java) is covered by python -m layer2 --verify.
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
from layer2.layout import make_skeleton
from layer2.llm import CHAINS
from layer2.source import Source, leaf
from sandbox.logs import write_json, write_text

PAIRS = [("python3", "rust"), ("python3", "go"), ("python3", "cpp"), ("java", "python3"), ("java", "csharp"),
         ("java", "go"), ("c", "python3"), ("c", "rust"), ("cpp", "python3"), ("csharp", "java"),
         ("javascript", "python3"), ("matlab", "python3")]

FLOOR_C = """    long q = a / b;
    if (a % b != 0 && ((a < 0) != (b < 0))) {
        q -= 1;
    }
    return q;"""
FIXTURES = {
    "python3": {"arith.py": '''def twice(x):
    return x * 2


def safe_div(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a // b


class Counter:
    def __init__(self, name, start):
        self.name = name
        self.count = start

    def bump(self, by):
        self.count += by
        return self.count
'''},
    "java": {"src/demo/Calc.java": """package demo;

public class Calc {
    public static long twice(long x) {
        return x * 2;
    }

    public static long safe_div(long a, long b) {
        if (b == 0) {
            throw new IllegalArgumentException("division by zero");
        }
        return Math.floorDiv(a, b);
    }
}
""", "src/demo/Counter.java": """package demo;

public class Counter {
    private String name;
    private long count;

    public Counter(String name, long start) {
        this.name = name;
        this.count = start;
    }

    public long bump(long by) {
        count += by;
        return count;
    }
}
"""},
    "c": {"calc.c": f"""long twice(long x) {{
    return x * 2;
}}

long safe_div(long a, long b) {{
    if (b == 0) {{
        return 0;
    }}
{FLOOR_C}
}}
"""},
    "cpp": {"calc.cpp": f"""#include <stdexcept>
#include <string>

long long twice(long long x) {{
    return x * 2;
}}

long long safe_div(long long a, long long b) {{
    if (b == 0) {{
        throw std::invalid_argument("division by zero");
    }}
{FLOOR_C.replace("long q", "long long q")}
}}

class Counter {{
public:
    std::string name;
    long long count;

    Counter(std::string name, long long start) : name(name), count(start) {{}}

    long long bump(long long by) {{
        count += by;
        return count;
    }}
}};
"""},
    "csharp": {"Calc.cs": """using System;

namespace Demo
{
    public static class Calc
    {
        public static long twice(long x)
        {
            return x * 2;
        }

        public static long safe_div(long a, long b)
        {
            if (b == 0)
            {
                throw new ArgumentException("division by zero");
            }
            long q = a / b;
            if (a % b != 0 && ((a < 0) != (b < 0)))
            {
                q -= 1;
            }
            return q;
        }
    }

    public class Counter
    {
        public string name;
        public long count;

        public Counter(string name, long start)
        {
            this.name = name;
            this.count = start;
        }

        public long bump(long by)
        {
            count += by;
            return count;
        }
    }
}
"""},
    "javascript": {"calc.js": """function twice(x) {
  return x * 2;
}

function safe_div(a, b) {
  if (b === 0) {
    throw new RangeError("division by zero");
  }
  return Math.floor(a / b);
}

class Counter {
  constructor(name, start) {
    this.name = name;
    this.count = start;
  }

  bump(by) {
    this.count += by;
    return this.count;
  }
}

module.exports = { twice, safe_div, Counter };
"""},
    "matlab": {"twice.m": "function y = twice(x)\n  y = x * 2;\nend\n",
               "safe_div.m": "function q = safe_div(a, b)\n  if b == 0\n    error('calc:zero', 'division by zero');\n"
                             "  end\n  q = floor(a / b);\nend\n"},
}

T = lambda code, imports=(), declaration=None: {"code": code, "imports": list(imports),  # noqa: E731
                                                "declaration": declaration}
PY = {
    "fn:twice": T("def {member}(x):\n    return x * 2"),
    "fn:safe_div": T('def {member}(a, b):\n    if b == 0:\n        raise ValueError("division by zero")\n'
                     "    return a // b"),
    "class:Counter": T("", declaration="class Counter"),
    "class:Calc": T("", declaration="class Calc"),
    "ctor:Counter": T("def __init__(self, name, start):\n    self.name = name\n    self.count = start"),
    "fn:bump": T("def {member}(self, by):\n    self.count += by\n    return self.count"),
}
RUST = {
    "fn:twice": T("pub fn {member}(x: i64) -> i64 {\n    x * 2\n}"),
    "fn:safe_div": T("pub fn {member}(a: i64, b: i64) -> Result<i64, String> {\n    if b == 0 {\n"
                     "        return Err(\"division by zero\".to_string());\n    }\n    let q = a / b;\n"
                     "    if a % b != 0 && ((a < 0) != (b < 0)) {\n        Ok(q - 1)\n    } else {\n        Ok(q)\n"
                     "    }\n}"),
    "class:Counter": T("pub name: String,\npub count: i64,", declaration="pub struct Counter"),
    "ctor:Counter": T("pub fn new(name: String, start: i64) -> Self {\n    Counter { name, count: start }\n}"),
    "fn:bump": T("pub fn {member}(&mut self, by: i64) -> i64 {\n    self.count += by;\n    self.count\n}"),
}
GO = {
    "fn:twice": T("func {member}(x int64) int64 {\n\treturn x * 2\n}"),
    "fn:safe_div": T("func {member}(a, b int64) (int64, error) {\n\tif b == 0 {\n"
                     "\t\treturn 0, errors.New(\"division by zero\")\n\t}\n\tq := a / b\n"
                     "\tif a%b != 0 && ((a < 0) != (b < 0)) {\n\t\tq--\n\t}\n\treturn q, nil\n}", ["errors"]),
    "class:Counter": T("Name  string\nCount int64", declaration="type Counter struct"),
    "class:Calc": T("", declaration="type Calc struct"),
    "ctor:Counter": T("func {member}(name string, start int64) *Counter {\n\treturn &Counter{Name: name, Count: start}\n}"),
    "fn:bump": T("func (c *Counter) {member}(by int64) int64 {\n\tc.Count += by\n\treturn c.Count\n}"),
}
CPP = {
    "fn:twice": T("inline long long {member}(long long x) {\n    return x * 2;\n}"),
    "fn:safe_div": T("inline long long {member}(long long a, long long b) {\n    if (b == 0) {\n"
                     "        throw std::invalid_argument(\"division by zero\");\n    }\n    long long q = a / b;\n"
                     "    if (a % b != 0 && ((a < 0) != (b < 0))) {\n        q -= 1;\n    }\n    return q;\n}",
                     ["<stdexcept>"]),
    "class:Counter": T("std::string name;\nlong long count;", ["<string>"], "class Counter"),
    "ctor:Counter": T("Counter(std::string name, long long start) : name(name), count(start) {}"),
    "fn:bump": T("long long {member}(long long by) {\n    count += by;\n    return count;\n}"),
}
CSHARP = {
    "class:Calc": T("", declaration="public static class Calc"),
    "fn:twice": T("public static long {member}(long x)\n{\n    return x * 2;\n}"),
    "fn:safe_div": T("public static long {member}(long a, long b)\n{\n    if (b == 0)\n    {\n"
                     "        throw new ArgumentException(\"division by zero\");\n    }\n"
                     "    return (long)Math.Floor((double)a / b);\n}"),
    "class:Counter": T("public string Name;\npublic long Count;", declaration="public class Counter"),
    "ctor:Counter": T("public Counter(string name, long start)\n{\n    Name = name;\n    Count = start;\n}"),
    "fn:bump": T("public long {member}(long by)\n{\n    Count += by;\n    return Count;\n}"),
}
JAVA = {
    "class:Calc": T("", declaration="public final class Calc"),
    "fn:twice": T("public static long {member}(long x) {\n    return x * 2;\n}"),
    "fn:safe_div": T("public static long {member}(long a, long b) {\n    if (b == 0) {\n"
                     "        throw new IllegalArgumentException(\"division by zero\");\n    }\n"
                     "    return Math.floorDiv(a, b);\n}"),
    "class:Counter": T("private String name;\nprivate long count;", declaration="public class Counter"),
    "ctor:Counter": T("public Counter(String name, long start) {\n    this.name = name;\n    this.count = start;\n}"),
    "fn:bump": T("public long {member}(long by) {\n    count += by;\n    return count;\n}"),
}
BY_TARGET = {"python3": PY, "rust": RUST, "go": GO, "cpp": CPP, "csharp": CSHARP, "java": JAVA}
OVERRIDE = {  # (source, target) -> key -> translation (the correct one)
    ("java", "python3"): {"fn:twice": T("@staticmethod\ndef {member}(x):\n    return x * 2"),
                          "fn:safe_div": T("@staticmethod\n" + PY["fn:safe_div"]["code"])},
    ("c", "python3"): {"fn:safe_div": T("def {member}(a, b):\n    if b == 0:\n        return 0\n    return a // b")},
    ("c", "rust"): {"fn:safe_div": T("pub fn {member}(a: i64, b: i64) -> i64 {\n    if b == 0 {\n        return 0;\n"
                                     "    }\n    let q = a / b;\n    if a % b != 0 && ((a < 0) != (b < 0)) {\n"
                                     "        q - 1\n    } else {\n        q\n    }\n}")},
}
BUGS = {  # (source, target) -> key -> the first translation (wrong); the Repair agent gets the correct one
    ("python3", "rust"): {"fn:safe_div": T("pub fn {member}(a: i64, b: i64) -> Result<i64, String> {\n"
                                           "    if b == 0 {\n        return Err(\"division by zero\".to_string());\n"
                                           "    }\n    Ok(a / b)\n}")},
    ("c", "python3"): {"fn:safe_div": T("def {member}(a, b):\n    if b == 0:\n        return 0\n    return int(a / b)")},
    ("python3", "go"): {"fn:twice": T("func {member}(x int64) int64 {\n\treturn x * 2\n}", ["fmt"])},
}
TESTS = {"fn:twice": [{"args": [21]}, {"args": [-4]}, {"args": [0]}],
         "fn:safe_div": [{"args": [7, 2]}, {"args": [-7, 2]}, {"args": [1, 0]}],
         "ctor:Counter": [{"args": ["c", 10]}],
         "fn:bump": [{"init": {"args": ["c", 10]}, "args": [5]}, {"init": {"args": ["c", 1]}, "args": [-3]}]}


class PairLLM:
    """Stands in for Groq for one pair: answers each agent from the tables above, keyed by the fragments the
    prompt is about; the code is filled with the member names of the target layout."""
    provider = "scripted"

    def __init__(self, bundle, sk, pair):
        self.pacer, self.usage, self.calls = SimpleNamespace(tpm=8000), {}, []
        self.sk, self.pair, self.src = sk, pair, Source(bundle)
        self.frag = {f["id"]: f for f in bundle["fragments"]}
        self.translated = set()

    def describe(self):
        return {"provider": "scripted", "chains": {a: ["scripted"] for a in CHAINS}, "tokens_per_minute_per_model": 8000}

    def key(self, fid):
        f = self.frag[fid]
        if f["kind"] in ("module_header", "module_init"):
            return "header"
        if f["kind"] == "class":
            return "class:" + leaf(f)
        if self.src.role(fid) == "constructor":
            return "ctor:" + leaf(self.frag[self.src.class_of(fid)])
        return "fn:" + leaf(f)

    def translation(self, fid, buggy):
        k = self.key(fid)
        t = (BUGS.get(self.pair, {}).get(k) if buggy else None) or OVERRIDE.get(self.pair, {}).get(k) or \
            BY_TARGET[self.pair[1]].get(k) or T("")
        member = self.sk.location(fid).get("member") or ""
        return {"id": fid, **t, "code": t["code"].replace("{member}", member)}

    def chat(self, agent, messages, tools=None, json_mode=True, max_tokens=0, effort="low", purpose="",
             tool_choice="auto"):
        self.calls.append({"agent": agent, "purpose": purpose, "messages": messages})
        u = self.usage.setdefault(agent, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        u["calls"] += 1
        user = messages[1]["content"]
        ids = list(dict.fromkeys(re.findall(r"^(?:FRAGMENT|CURRENT TRANSLATION OF|LEGACY SOURCE OF) (\S+)", user,
                                            re.M)))
        if agent == "analyzer":
            answer = {"fragments": [{"id": i, "purpose": "scripted", "hazards": []} for i in ids]}
        elif agent == "testgen":
            answer = {"cases": [] if purpose.startswith("coverage") else TESTS.get(self.key(ids[0]), [])}
        elif agent == "translator":
            answer = {"fragments": []}
            for i in ids:
                answer["fragments"].append(self.translation(i, buggy=i not in self.translated))
                self.translated.add(i)
        elif agent == "repair":
            answer = {"fragments": [self.translation(i, buggy=False) for i in ids]}
        else:
            answer = {"diagnosis": "scripted: the translation divides differently", "suspects": [], "fix": "floor"}
        return {"tool_calls": [], "model": "scripted", "usage": {}, "seconds": 0.0, "finish_reason": "stop",
                "content": json.dumps(answer)}


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def run_pairs(run, echo, pairs=None):
    pairs = pairs or PAIRS
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        echo(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    echo(f"== Layer 0: {len(pairs)} fixture bundles")
    repos = []
    for s, t in pairs:
        d = run / "fixtures" / f"calc_{s}_to_{t}"
        for rel, code in FIXTURES[s].items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(code, encoding="utf-8", newline="\n")
        repos.append({**layer0.resolve(str(d), None, t, s), "pair": (s, t)})
    s0 = layer0.build(repos, run / "layer0")
    ok0 = {r["repo"]: r["ok"] for r in s0["repos"]}
    check("Layer 0 builds every fixture bundle", all(ok0.values()),
          ", ".join(f"{k.split('calc_')[-1]}: {r['stats']['fragments']} fragments" if r["ok"] else f"{k}: FAILED"
                    for k, r in zip(ok0, s0["repos"])))

    total = {"tests": 0, "equivalent": 0}
    for r in repos:
        s, t = r["pair"]
        name, bdir = r["name"], run / "layer0" / r["name"]
        if not ok0.get(name):
            check(f"{s} -> {t}", False, "no bundle")
            continue
        echo(f"\n== {s} -> {t}")
        bundle = bundle_contract.load(bdir)
        out = run / f"{s}-{t}"
        llm = PairLLM(bundle, make_skeleton(bundle), (s, t))
        l2 = AgentLayer2(bundle, bdir, r["code"], out, llm=llm)
        try:
            migrate(bdir, r["code"], out, l2, verbose=False)
        finally:
            l2.close()
        st = _read(out / "state.json")
        val = run_contract.validate(out, bdir)
        build = build_check(out / "migrated", t, out / "build_check")
        ev = report.layer2_evidence(out, st)
        write_json(out / "layer2_summary.json", ev)
        write_text(out / "report.md", report.render(out, val, build, "agents (scripted LLM)", None, None, ev))
        units = st["result"]["units_by_status"]
        total["tests"] += ev["differential_tests"]
        total["equivalent"] += ev["tests_equivalent"]
        want = {"fn:twice", "fn:safe_div"} | ({"fn:bump"} if s not in ("c", "matlab") else set())
        tested = {llm.key(c.split("#")[0]) for c in _tested_cases(out)}
        check(f"{s} -> {t}: every unit migrates, the target builds, every differential test is equivalent",
              set(units) == {"migrated"} and build["ok"] and all(x["ok"] for x in val)
              and ev["differential_tests"] and ev["tests_equivalent"] == ev["differential_tests"] and want <= tested,
              f"units {units}; build {'ok' if build['ok'] else build['errors'][:2]} ({build.get('tool')}); "
              f"tests {ev['tests_equivalent']}/{ev['differential_tests']} equivalent over {sorted(tested)}; "
              f"contract {sum(x['ok'] for x in val)}/{len(val)}")
        for k in BUGS.get((s, t), {}):
            fid = next(f for f in bundle["fragments"] if llm.key(f["id"]) == k)["id"]
            unit = next(u for u in st["units"] if fid in u["fragments"])
            l2dir = out / "units" / unit["unit"] / "attempt-1" / "layer2"
            f0 = _read(l2dir / "failure-0.json") if (l2dir / "failure-0.json").exists() else None
            v = _read(out / "units" / unit["unit"] / "attempt-1" / "verdict.json")
            expect = "compile_error" if t == "go" else "inequivalent"
            check(f"{s} -> {t}: the planted bug in {k[3:]} is caught ({expect}) and repaired",
                  f0 and f0["kind"] == expect and v["outcome"] == "pass"
                  and [x["agent"] for x in v["trace"]].count("repair") == 1,
                  (f0["summary"][:160] if f0 else "no failure record") + f"; then {v['outcome']}: {v['reason'][:80]}")
    check("all pairs: differential tests across languages", total["tests"] > 0 and total["equivalent"] == total["tests"],
          f"{total['equivalent']}/{total['tests']} equivalent over {len(pairs)} pairs")
    ok = all(c["ok"] for c in checks)
    write_json(run / "summary.json", {"kind": "verify-pairs", "ok": ok, "pairs": [f"{s}->{t}" for s, t in pairs],
                                      "checks": checks})
    echo(f"\n{sum(c['ok'] for c in checks)}/{len(checks)} known-answer checks passed\nlogs: {run}")
    return ok


def _tested_cases(out):
    ids = []
    for p in (out / "layer2" / "units").glob("*/tests.json"):
        ids += [c["case_id"] for c in _read(p)["cases"]]
    return ids
