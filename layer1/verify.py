"""Known-answer verification of Layer 1 (python -m layer1 --verify), integrated with Layer 0.

Layer 0 first builds two bundles of its fixture (a small Python 2 package with inheritance, a mutual-recursion
cycle, recursion, a registry): one for a Java target and one for a Python 3 target. Layer 1 then runs scenarios
whose outcome is known in advance, with the dummy Layer 2 scripted to misbehave:

  all-pass          every unit migrated once, in order; the cycle sent as one unit; requests carry the migrated
                    dependencies; the composed Java stubs compile
  python3-identity  the dummy echoes the source, so the spliced codebase must equal the original byte for byte
  scripted          fail->pass retry, malformed verdict, exception, a unit that always fails (escalated after 3
                    attempts, its dependents blocked), a needs_human verdict (escalated at once)
  hitl              the same script with a scripted human: a retry with guidance, an invalid then a valid accept
  resume            a crash in the middle, then --resume: same result as an uninterrupted run, no unit redone
  determinism       a second identical run gives identical state, translations, requests and codebase
  bundle gate       a tampered bundle is refused before anything reaches Layer 2
  contract          the run validator detects planted violations
  schemas           the committed JSON schemas match the protocol models
  llm               report prose: escalation briefs reach the human; the grounding guard rejects invented numbers
                    and identifiers and falls back to the template; a live call through the provider chain
                    (skipped without a usable key)
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

from layer0 import __main__ as layer0
from layer0 import contract as bundle_contract
from layer2.dummy import DummyLayer2, JavaNames, SimulatedCrash
from sandbox.logs import write_json, write_text

from . import contract, protocol, report
from . import llm as llm_text
from .build_check import build_check
from .orchestrator import migrate

SCHEMAS = Path(__file__).resolve().parent / "schemas"
SCRIPT = {"pkg.util.fact": ["fail", "pass"], "pkg.util.report": ["malformed", "pass"],
          "pkg.shapes.make": ["raise", "pass"], "pkg.util.double": ["fail"], "pkg.shapes.lookup": ["needs_human"]}
GUIDANCE = "SCALE is an int: keep integer arithmetic"
BRIEF = "- Every attempt ended with a scripted failure.\n- The cause is unclear.\n- Retry with guidance, or accept a hand translation."


class FakeLLM:
    """Stands in for a real LLM: answers from a queue (the last answer repeats) and remembers the prompts."""
    model, substitution, provider = "fake", None, "fake"

    def __init__(self, *answers):
        self.answers, self.prompts = list(answers), []

    def chat(self, system, user, **kw):
        self.prompts.append(user)
        text = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {"text": text, "model": self.model, "provider": self.provider, "usage": {}, "seconds": 0.0}


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _tree(d):
    d = Path(d)
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()} if d.exists() else {}


class Scenario:
    def __init__(self, run, name, bundle_dir, source, script=None, hitl="defer", human=None, build=True,
                 layer2=None, resume_from=None, llm=None):
        self.out, self.bundle_dir = run / name, bundle_dir
        self.bundle = bundle_contract.load(bundle_dir)
        self.l2 = layer2 or DummyLayer2(self.bundle, script)
        self.r = migrate(bundle_dir, source, self.out, self.l2, hitl=hitl, human=human, verbose=False,
                         resume=resume_from is not None, llm=llm)
        self.st = _read(self.out / "state.json")
        self.val = contract.validate(self.out, bundle_dir) if "units" in self.st else []
        write_json(self.out / "validation.json", self.val)
        lang = self.bundle["manifest"]["migration"]["target_language"]
        self.build = build_check(self.out / "migrated", lang, self.out / "build_check") if build else None
        write_text(self.out / "report.md", report.render(self.out, self.val, self.build, "dummy (scripted)"))
        self.units = {u["unit"]: u for u in self.st.get("units", [])}
        self.unit_of = {f: u["unit"] for u in self.st.get("units", []) for f in u["fragments"]}

    def u(self, fid):
        return self.units[self.unit_of[fid]]

    def req(self, fid, attempt=1):
        return _read(self.out / "units" / self.unit_of[fid] / f"attempt-{attempt}" / "request.json")

    def ev(self, fid, attempt=1):
        return _read(self.out / "units" / self.unit_of[fid] / f"attempt-{attempt}" / "evaluation.json")

    def contract_ok(self):
        return all(x["ok"] for x in self.val), f"{sum(x['ok'] for x in self.val)}/{len(self.val)}" + \
            "".join(f"; FAIL {x['check']}: {x['detail']}" for x in self.val if not x["ok"])


def run_verify(run, echo):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        echo(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))

    # ------------------------------------------------------------------ Layer 0: the two fixture bundles
    echo("== Layer 0: fixture bundle for a Java target (its own verify), and for a Python 3 target")
    l0 = run / "layer0"
    (l0 / "verify").mkdir(parents=True)
    s0 = layer0.verify(l0 / "verify")
    src = l0 / "verify" / "verify" / "fixture" / "src"
    bj = l0 / "verify" / "verify" / "bundle"
    (l0 / "python3").mkdir()
    s0b = layer0.build([{"name": "fixture", "repo": src, "code": src, "source_root": ".", "java": None}],
                       l0 / "python3", "python3")
    bp = l0 / "python3" / "fixture"
    echo("\n== Layer 1 scenarios")
    check("Layer 0 builds both fixture bundles", s0["ok"] and s0b["repos"][0]["ok"],
          f"Java target: {len(s0['fixture_checks'])} fixture + {len(s0['contract_checks'])} contract checks; "
          f"Python 3 target: contract {s0b['repos'][0].get('validation')}")
    if not checks[-1]["ok"]:
        return _finish(run, checks, echo)
    n_units = len(bundle_contract.load(bj)["order"]["steps"])

    # ------------------------------------------------------------------ all-pass (Java, compose)
    a = Scenario(run, "all-pass", bj, src)
    c = a.st["result"]["counters"]
    check("all-pass: every unit migrated with one Layer 2 call each, in migration order",
          a.st["result"]["units_by_status"] == {"migrated": n_units} and c["invocations"] == n_units
          and a.l2.calls == [(f"step-{i:04d}", 1) for i in range(1, n_units + 1)],
          f"{n_units} units, {c['invocations']} calls")
    cyc = a.req("pkg.util.is_even")
    check("all-pass: the mutual-recursion cycle is sent as one unit",
          [f["id"] for f in cyc["fragments"]] == ["pkg.util.is_even", "pkg.util.is_odd"] and cyc["cycle"],
          f"{cyc['unit']}: {[f['id'] for f in cyc['fragments']]}")
    deps = {d["id"]: d for d in a.req("pkg.shapes.Square.area")["dependencies"]}
    want = {"pkg.shapes.Square", "pkg.shapes::header", "pkg.util.double", "pkg.util::header"}
    check("all-pass: a request carries the already-migrated dependencies with their translations",
          set(deps) == want and all(d["translations"] for d in deps.values())
          and "call" in deps["pkg.util.double"]["edge_kinds"] and "member" in deps["pkg.shapes.Square"]["edge_kinds"],
          "Square.area gets " + ", ".join(f"{k} ({'/'.join(d['edge_kinds'])})" for k, d in sorted(deps.items())))
    java = {k.rsplit("/", 1)[-1] for k in _tree(a.out / "migrated")}
    sq = (a.out / "migrated/src/main/java/com/demo/Square.java").read_text()
    cube = (a.out / "migrated/src/main/java/com/demo/Cube.java").read_text()
    util = (a.out / "migrated/src/main/java/com/demo/UtilModule.java").read_text()
    check("all-pass: the composed Java codebase has the expected classes and compiles",
          java == {"Cube.java", "PkgPackage.java", "Shape.java", "ShapesModule.java", "Square.java", "UtilModule.java"}
          and "public class Square extends com.demo.Shape {" in sq and "public class Cube extends com.demo.Square {" in cube
          and "public Cube(Object... args)" in cube and "public static Object double_(Object... args)" in util
          and a.build["ok"], f"{sorted(java)}; {a.build['tool']}: {'ok' if a.build['ok'] else a.build['errors'][:2]}")
    check("all-pass: run contract", *a.contract_ok())

    # ------------------------------------------------------------------ python3-identity (splice)
    b = Scenario(run, "python3-identity", bp, src)
    files = [x["file"] for x in b.bundle["manifest"]["files"]]
    same = [f for f in files if (b.out / "migrated" / f).read_bytes() == (src / f).read_bytes()]
    check("python3-identity: the spliced codebase equals the original byte for byte",
          b.st["result"]["units_by_status"] == {"migrated": len(b.st["units"])} and same == files,
          f"{len(same)}/{len(files)} files identical")
    check("python3-identity: the build check flags exactly the echoed Python 2 file",
          b.build and [e.split(":")[0] for e in b.build["errors"]] == ["pkg/util.py"],
          f"{b.build['tool']}: {b.build['errors']}")
    check("python3-identity: run contract", *b.contract_ok())

    # ------------------------------------------------------------------ scripted failures, hitl=defer
    s = Scenario(run, "scripted", bj, src, script=SCRIPT)
    fact, rep, make = s.u("pkg.util.fact"), s.u("pkg.util.report"), s.u("pkg.shapes.make")
    fb = s.req("pkg.util.fact", 2)["feedback"]
    check("scripted: a failed attempt is retried with its feedback, then committed",
          fact["status"] == "migrated" and fact["attempts"] == 2 and len(fb) == 1 and fb[0]["attempt"] == 1
          and fb[0]["outcome"] == "fail" and "scripted failure" in fb[0]["reason"],
          f"fact: {fact['status']} after {fact['attempts']} attempts; attempt-2 feedback: {fb[0]['reason'] if fb else None}")
    e1, e2 = s.ev("pkg.util.report"), s.ev("pkg.shapes.make")
    check("scripted: a malformed 'pass' and a Layer 2 exception count as failed attempts",
          not e1["valid"] and e1["outcome"] == "error" and "no translation for pkg.util.report" in e1["problems"]
          and rep["status"] == "migrated" and rep["attempts"] == 2 and e2["outcome"] == "error"
          and e2["problems"][0].startswith("Layer 2 raised RuntimeError") and make["status"] == "migrated",
          f"report: {e1['problems']}; make: {e2['problems']}")
    dbl, look = s.u("pkg.util.double"), s.u("pkg.shapes.lookup")
    check("scripted: retries are bounded at 3, then the unit is escalated; needs_human escalates at once",
          dbl["status"] == "escalated" and dbl["attempts"] == 3 and dbl["escalation"]["reason"] == "retries_exhausted"
          and look["status"] == "escalated" and look["attempts"] == 1 and look["escalation"]["reason"] == "needs_human",
          f"double: {dbl['attempts']} attempts ({dbl['escalation']['reason']}); "
          f"lookup: {look['attempts']} ({look['escalation']['reason']})")
    blocked = sorted(f for u in s.st["units"] if u["status"] == "blocked" for f in u["fragments"])
    check("scripted: exactly the transitive dependents of the escalated unit are blocked, and never sent",
          blocked == ["pkg.shapes.Square.area", "pkg.shapes.Square.describe"]
          and s.u("pkg.shapes.Square.describe")["root_causes"] == ["pkg.util.double"]
          and not any((s.out / "units" / s.unit_of[f]).exists() for f in blocked),
          f"blocked {blocked}, root cause {s.u('pkg.shapes.Square.describe')['root_causes']}")
    calls = s.st["result"]["counters"]["invocations"]
    check("scripted: Layer 2 call count and build", calls == n_units - 2 + 5 and s.build["ok"],
          f"{calls} calls = {n_units - 2} units sent + 5 retries; javac {'ok' if s.build['ok'] else 'FAILED'} "
          "without the blocked methods")
    check("scripted: run contract", *s.contract_ok())

    # ------------------------------------------------------------------ human in the loop
    payloads, names = [], JavaNames(bundle_contract.load(bj))

    def human(p):
        payloads.append(p)
        ids = [f["id"] for f in p["fragments"]]
        if ids == ["pkg.util.double"]:
            return {"action": "retry", "guidance": GUIDANCE}
        if ids == ["pkg.shapes.lookup"] and not p["problems_with_last_decision"]:
            return {"action": "accept", "note": "first try",
                    "translations": [{"fragment": "pkg.shapes.lookup", "target_file": "../Lookup.java", "code": "x"}]}
        if ids == ["pkg.shapes.lookup"]:
            return {"action": "accept", "note": "hand-written",
                    "translations": names.pieces(names.frag["pkg.shapes.lookup"])}
        return {"action": "skip"}

    h = Scenario(run, "hitl", bj, src, script=SCRIPT, hitl="prompt", human=human, llm=FakeLLM(BRIEF))
    dbl, look = h.u("pkg.util.double"), h.u("pkg.shapes.lookup")
    r4 = h.req("pkg.util.double", 4)
    check("hitl: a human retry with guidance gets a fresh budget and the unit migrates",
          dbl["status"] == "migrated" and dbl["by"] == "layer2" and dbl["attempts"] == 4 and dbl["human_retries"] == 1
          and r4["human_guidance"] == GUIDANCE and [x["attempt"] for x in r4["feedback"]] == [1, 2, 3],
          f"double: {dbl['status']} at attempt {dbl['attempts']}, guidance passed: {r4['human_guidance']!r}")
    check("hitl: an invalid human translation is refused and asked again; a valid one is committed",
          look["status"] == "migrated" and look["by"] == "human"
          and [d["action"] for d in look["decisions"]] == ["accept", "accept"]
          and any("unsafe target_file" in x for p in payloads[2:3] for x in p["problems_with_last_decision"]),
          f"lookup: {look['status']} by {look['by']}; re-asked with: "
          f"{[x for p in payloads[2:3] for x in p['problems_with_last_decision']]}")
    check("hitl: three pauses, nothing escalated or blocked",
          len(payloads) == 3 and set(h.st["result"]["units_by_status"]) == {"migrated"} and h.build["ok"],
          f"{len(payloads)} interrupts; units {h.st['result']['units_by_status']}")
    check("hitl: run contract", *h.contract_ok())

    # ------------------------------------------------------------------ crash + resume
    out = run / "resume"
    crash_at = "pkg.shapes.Square.area"
    first = DummyLayer2(bundle_contract.load(bj), {crash_at: ["crash"]})
    try:
        migrate(bj, src, out, first, verbose=False)
        crashed = False
    except SimulatedCrash:
        crashed = True
    check("resume: the crash stops the run with its checkpoint kept",
          crashed and (out / "checkpoints.sqlite").exists() and not (out / "state.json").exists(),
          f"crashed at {crash_at} after {len(first.calls)} Layer 2 calls")
    second = DummyLayer2(bundle_contract.load(bj))
    r = Scenario(run, "resume", bj, src, layer2=second, resume_from=out, build=False)
    k = int(r.unit_of[crash_at].split("-")[1])
    same = {name: (r.out / name).read_bytes() == (a.out / name).read_bytes()
            for name in ("state.json", "translations.jsonl")}
    check("resume: continues at the crashed unit, redoes nothing, ends exactly like an uninterrupted run",
          second.calls == [(f"step-{i:04d}", 1) for i in range(k, n_units + 1)] and all(same.values())
          and _tree(r.out / "migrated") == _tree(a.out / "migrated") and not (out / "checkpoints.sqlite").exists(),
          f"resumed with {len(second.calls)} calls (steps {k}..{n_units}); identical: {same}, migrated/ "
          f"{'identical' if _tree(r.out / 'migrated') == _tree(a.out / 'migrated') else 'DIFFERS'}")
    check("resume: run contract", *r.contract_ok())

    # ------------------------------------------------------------------ determinism
    d = Scenario(run, "all-pass-again", bj, src, build=False)
    reqs = lambda sc: {p.relative_to(sc.out).as_posix(): {k: v for k, v in _read(p).items() if k != "run"}  # noqa: E731
                       for p in sorted((sc.out / "units").rglob("request.json"))}
    diffs = [n for n in ("state.json", "translations.jsonl", "plan.json")
             if (d.out / n).read_bytes() != (a.out / n).read_bytes()]
    check("determinism: a second run gives identical state, translations, requests and codebase",
          not diffs and reqs(d) == reqs(a) and _tree(d.out / "migrated") == _tree(a.out / "migrated"),
          f"{len(reqs(d))} requests compared" + (f"; differ: {diffs}" if diffs else ""))

    # ------------------------------------------------------------------ the bundle gate
    bad = run / "tampered-bundle"
    shutil.copytree(bj, bad, ignore=shutil.ignore_patterns("raw"))
    lines = (bad / "fragments.jsonl").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        f = json.loads(line)
        if f["id"] == "pkg.util.fact":
            f["source"] = f["source"].replace("n * fact(n - 1)", "n * fact(n - 2)")
            lines[i] = json.dumps(f)
    (bad / "fragments.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    g_l2 = DummyLayer2(bundle_contract.load(bad))
    g = migrate(bad, src, run / "gate", g_l2, verbose=False)
    ab = g["result"].get("aborted") or []
    check("bundle gate: a bundle whose fragment differs from the source is refused before Layer 2",
          g["result"]["ok"] is False and any("source fidelity" in x for x in ab) and g_l2.calls == [],
          ab[0][:110] if ab else "not refused")

    # ------------------------------------------------------------------ the run validator catches violations
    def planted(name, edit, expect):
        t = run / f"tampered-run-{name}"
        shutil.copytree(s.out, t)
        edit(t)
        failed = [x["check"] for x in contract.validate(t, bj) if not x["ok"]]
        return any(expect in x for x in failed), failed

    def unblock(t):
        st = _read(t / "state.json")
        for u in st["units"]:
            if u["status"] == "blocked":
                u["status"] = "migrated"
                for f in u["fragments"]:
                    st["fragments"][f]["status"] = "migrated"
        write_json(t / "state.json", st)

    def swap_code(t):
        rows = [json.loads(l) for l in (t / "translations.jsonl").read_text(encoding="utf-8").splitlines()]
        rows[0]["pieces"][0]["code"] += "\n// changed after the verdict"
        write_text(t / "translations.jsonl", "".join(json.dumps(r) + "\n" for r in rows))

    def drop_feedback(t):
        p = t / "units" / s.unit_of["pkg.util.fact"] / "attempt-2" / "request.json"
        q = _read(p)
        q["feedback"] = []
        write_json(p, q)

    results = [planted("unblocked", unblock, "blocking is exact"),
               planted("changed-code", swap_code, "committed translations come from"),
               planted("lost-feedback", drop_feedback, "requests carry")]
    check("run contract detects planted violations (unblocked dependent, changed code, lost feedback)",
          all(ok for ok, _ in results), "; ".join(f"{len(f)} failed: {f[:2]}" for _, f in results))

    briefs = [_read(h.out / "units" / h.unit_of[f] / "brief.json") for f in ("pkg.util.double", "pkg.shapes.lookup")]
    check("llm: each escalation gets a brief, shown to the human and saved outside the state",
          all(p.get("brief") == BRIEF for p in payloads) and all(b["source"] == "llm" for b in briefs)
          and "brief" not in (h.out / "state.json").read_text(encoding="utf-8"),
          f"{len(payloads)} prompts carried the brief; units/<unit>/brief.json source: {[b['source'] for b in briefs]}")

    # ------------------------------------------------------------------ the grounding guard (offline)
    val = s.val
    good = (f"Layer 2 was called {s.st['result']['counters']['invocations']} times; `{s.unit_of['pkg.util.double']}` "
            "was escalated. The translations are placeholders.")
    invented = "Layer 2 was called 999 times and `pkg.util.triple` failed."
    fixed = llm_text.run_summary(FakeLLM(invented, good), s.st, val, s.build, "dummy")
    kept = llm_text.run_summary(FakeLLM(invented), s.st, val, s.build, "dummy")
    spaces = llm_text.run_summary(FakeLLM("The build used `javac (OpenJDK\u202f17)`."), s.st, val, s.build, "dummy")
    check("llm: invented numbers and identifiers are rejected, retried once, then replaced by the template",
          fixed["source"] == "llm" and fixed["attempts"] == 2 and fixed["text"] == good
          and kept["source"] == "template" and any("999" in x for x in kept["problems"])
          and any("pkg.util.triple" in x for x in kept["problems"]) and kept["text"].startswith("Layer 1 processed")
          and spaces["source"] == "llm",
          f"retry fixed it: {fixed['source']}; still invented: {kept['source']} ({kept['problems'][:2]}); "
          "a narrow no-break space is normalised")

    class Down:
        model, substitution = "down-model", None

        def __init__(self, provider, error):
            self.provider, self.error, self.calls = provider, error, 0

        def chat(self, system, user, **kw):
            self.calls += 1
            raise llm_text.LLMError(self.error)

    unpaid, busy = Down("unpaid", "unpaid HTTP 402: credits depleted"), Down("busy", "busy HTTP 503: overloaded")
    chain = llm_text.FallbackLLM([unpaid, busy, FakeLLM(good)])
    first = llm_text.run_summary(chain, s.st, val, s.build, "dummy")
    second = llm_text.run_summary(chain, s.st, val, s.build, "dummy")
    check("llm: a failing provider hands over to the next; one with a billing/key error is not asked again",
          first["source"] == second["source"] == "llm" and first["provider"] == "fake"
          and unpaid.calls == 1 and busy.calls == 2 and "unpaid failed" in (first["note"] or ""),
          f"answered by {first['provider']}; calls: unpaid {unpaid.calls} (dropped after HTTP 402), "
          f"busy {busy.calls} (HTTP 503, kept); note: {first['note']}")

    # ------------------------------------------------------------------ live calls (need GROQ_API_KEY / GEMINI_API_KEY)
    live, note = llm_text.from_env("auto")
    if live is None:
        check("llm: live summary (skipped)", True, note)
    else:
        log = run / "llm-live" / "calls.jsonl"
        r = llm_text.run_summary(live, a.st, a.val, a.build, "dummy", log=log)
        write_json(run / "llm-live" / "summary.json", r)
        keys = [os.environ[k] for k in ("GROQ_API_KEY", "GEMINI_API_KEY") if os.environ.get(k)]
        leaked = [str(q) for q in run.rglob("*") if q.is_file() and q.suffix in (".json", ".jsonl", ".md", ".log")
                  and any(k in q.read_text(encoding="utf-8", errors="replace") for k in keys)]
        check("llm: live summary of the all-pass run (grounded or safely replaced), no key ever logged",
              not (r["note"] or "").startswith("LLM call failed") and not leaked,
              f"providers: {note}. Result: {r.get('provider')} {r['model']}, {r['source']} after {r['attempts']} "
              f"call(s); {len(keys)} key(s) scanned for" + (f"; KEY LEAKED in {leaked}" if leaked else ""))

    # ------------------------------------------------------------------ schemas
    current = protocol.schemas()
    stale = [n for n, sch in current.items()
             if not (SCHEMAS / f"{n}.json").exists() or _read(SCHEMAS / f"{n}.json") != sch]
    check("committed JSON schemas match the protocol models", not stale,
          f"{', '.join(f'layer1/schemas/{n}.json' for n in current)}" + (f"; stale: {stale}" if stale else ""))
    return _finish(run, checks, echo)


def _finish(run, checks, echo):
    ok = all(c["ok"] for c in checks)
    write_json(run / "summary.json", {"kind": "verify", "ok": ok, "checks": checks})
    echo(f"\n{sum(c['ok'] for c in checks)}/{len(checks)} known-answer checks passed\nlogs: {run}")
    return ok
