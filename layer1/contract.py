"""Re-check a finished Layer 1 run from its files, independently of the orchestrator's code paths.

Inputs: the Layer 0 bundle and the Layer 1 output directory (state.json, translations.jsonl, escalations.json,
units/<unit>/attempt-<n>/{request,verdict,evaluation}.json, migrated/). Returns [{check, ok, detail}].
"""
import json
from collections import defaultdict
from pathlib import Path

from pydantic import ValidationError

from layer0 import contract as bundle_contract

from .protocol import SLOT, Piece, TranslationRequest, Verdict

SATISFIED = ("migrated", "vendored")
FINAL = ("migrated", "vendored", "escalated", "blocked", "out_of_scope")
SKIPPED = ("vendored", "out_of_scope")  # decided without Layer 2
FILES = ("plan.json", "state.json", "translations.jsonl", "escalations.json", "events.jsonl")


def _read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def validate(out, bundle_dir):
    out, results = Path(out), []

    def check(name, problems, detail_ok=""):
        results.append({"check": name, "ok": not problems,
                        "detail": detail_ok if not problems else "; ".join(map(str, problems[:6]))
                        + (f" (+{len(problems) - 6} more)" if len(problems) > 6 else "")})

    missing = [f for f in FILES if not (out / f).exists()]
    if missing:
        check("run output complete", [f"missing {m}" for m in missing])
        return results
    b = bundle_contract.load(bundle_dir)
    frag = {f["id"]: f for f in b["fragments"]}
    st = _read(out / "state.json")
    units, fstat = st["units"], st["fragments"]
    unit_of = {f: u["unit"] for u in units for f in u["fragments"]}
    by_unit = {u["unit"]: u for u in units}
    trans = defaultdict(list)
    for line in (out / "translations.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        trans[r["fragment"]] = r["pieces"]

    # 1. every unit and fragment reached a final status, consistently
    problems = []
    for u in units:
        if u["status"] not in FINAL:
            problems.append(f"{u['unit']}: status {u['status']}")
        for f in u["fragments"]:
            if fstat.get(f, {}).get("status") != u["status"] or fstat[f]["unit"] != u["unit"]:
                problems.append(f"{f}: fragment status disagrees with its unit {u['unit']}")
    if set(fstat) != set(frag):
        problems.append("fragment set differs from the bundle")
    check("run complete: every unit and fragment has a final status", problems,
          f"{len(units)} units, {len(fstat)} fragments: " +
          ", ".join(f"{k} {v}" for k, v in sorted(st["result"]["units_by_status"].items())))

    # 2. units are the bundle's steps, decided one after another in migration order
    problems = []
    if [u["fragments"] for u in units] != [s["fragments"] for s in b["order"]["steps"]]:
        problems.append("units differ from the migration order's steps")
    decided = [u.get("decided_at", -1) for u in units]
    if decided != sorted(decided) or len(set(decided)) != len(decided):
        problems.append("units were not decided one at a time in migration order")
    check("units follow the migration order, one at a time", problems, f"{len(units)} units in step order")

    # 3. a unit is sent to Layer 2 only after all its dependencies were migrated (or are vendored)
    problems, n = [], 0
    ext = {u["unit"]: sorted({d for f in u["fragments"] for d in frag[f]["depends_on"] if d not in u["fragments"]})
           for u in units}
    for u in units:
        if "invoked_at" not in u:
            continue
        for d in ext[u["unit"]]:
            n += 1
            du = by_unit[unit_of[d]]
            if du["status"] not in SATISFIED or du["decided_at"] >= u["scheduled_at"]:
                problems.append(f"{u['unit']} sent before its dependency {d} was migrated")
    check("dependencies are migrated before their dependents are sent", problems,
          f"{n} dependency edges of sent units checked")

    # 4. blocking is exact, and blocked / vendored / out-of-scope units never reach Layer 2
    problems = []
    plan = _read(out / "plan.json")
    scoped = {u["unit"]: u.get("in_scope", True) for u in plan["units"]}
    for u in units:
        bad = [d for d in ext[u["unit"]] if fstat[d]["status"] not in SATISFIED]
        if (u["status"] == "out_of_scope") == scoped.get(u["unit"], True):
            problems.append(f"{u['unit']}: status {u['status']} but in_scope={scoped.get(u['unit'])} in plan.json")
        if u["status"] not in SKIPPED and (u["status"] == "blocked") != bool(bad):
            problems.append(f"{u['unit']}: status {u['status']} but unsatisfied dependencies {bad}")
        if u["status"] == "blocked":
            if u.get("blocked_by") != bad:
                problems.append(f"{u['unit']}: blocked_by {u.get('blocked_by')} != {bad}")
            if any(fstat[r]["status"] != "escalated" for r in u.get("root_causes", [])) or not u.get("root_causes"):
                problems.append(f"{u['unit']}: root causes {u.get('root_causes')} are not escalated fragments")
        if u["status"] == "vendored" and not all("vendored" in frag[f]["flags"] for f in u["fragments"]):
            problems.append(f"{u['unit']}: skipped as vendored but not all fragments are vendored")
        if u["status"] in ("blocked",) + SKIPPED and (out / "units" / u["unit"]).exists():
            problems.append(f"{u['unit']}: {u['status']} but was sent to Layer 2")
    check("blocking is exact; blocked, vendored and out-of-scope units are never sent", problems,
          f"{sum(u['status'] == 'blocked' for u in units)} blocked, "
          f"{sum(u['status'] == 'vendored' for u in units)} vendored, "
          f"{sum(u['status'] == 'out_of_scope' for u in units)} out of scope")

    # 5. retry bound, and one request/verdict/evaluation per attempt
    problems, total = [], 0
    for u in units:
        d = out / "units" / u["unit"]
        dirs = sorted(int(p.name.split("-")[1]) for p in d.glob("attempt-*")) if d.exists() else []
        total += len(dirs)
        if dirs != list(range(1, u["attempts"] + 1)):
            problems.append(f"{u['unit']}: attempt dirs {dirs} for {u['attempts']} attempts")
        if u["attempts"] > st["max_attempts"] * (1 + u.get("human_retries", 0)):
            problems.append(f"{u['unit']}: {u['attempts']} attempts exceed the bound")
        for a in dirs:
            for name in ("request.json", "verdict.json", "evaluation.json"):
                if not (d / f"attempt-{a}" / name).exists():
                    problems.append(f"{u['unit']} attempt {a}: missing {name}")
    if total != st["result"]["counters"]["invocations"]:
        problems.append(f"{total} attempt dirs but {st['result']['counters']['invocations']} invocations counted")
    check(f"at most {st['max_attempts']} attempts per unit (plus human-granted retries)", problems,
          f"{total} Layer 2 invocations, max {max((u['attempts'] for u in units), default=0)} on one unit")
    if problems:
        return results

    # 6 + 7. requests are well-formed and carry the complete context; retries carry the earlier feedback
    proto, ctx = [], []
    for u in units:
        for a in range(1, u["attempts"] + 1):
            d = out / "units" / u["unit"] / f"attempt-{a}"
            req, ver, ev = _read(d / "request.json"), _read(d / "verdict.json"), _read(d / "evaluation.json")
            try:
                TranslationRequest.model_validate(req)
            except ValidationError as e:
                proto.append(f"{u['unit']} attempt {a}: request: {str(e).splitlines()[0]}")
            try:
                v = Verdict.model_validate(ver)
                if ev["outcome"] == "pass" and (v.outcome != "pass" or not ev["valid"]):
                    proto.append(f"{u['unit']} attempt {a}: evaluation pass for a {v.outcome} verdict")
            except ValidationError:
                if ev["valid"]:
                    proto.append(f"{u['unit']} attempt {a}: schema-invalid verdict evaluated as valid")
            if (req["unit"], req["attempt"]) != (u["unit"], a):
                ctx.append(f"{u['unit']} attempt {a}: request is for {req['unit']} attempt {req['attempt']}")
            if req["fragments"] != [frag[f] for f in u["fragments"]]:
                ctx.append(f"{u['unit']} attempt {a}: fragments differ from the bundle")
            deps = {x["id"]: x for x in req["dependencies"]}
            if sorted(deps) != ext[u["unit"]]:
                ctx.append(f"{u['unit']} attempt {a}: dependencies {sorted(deps)} != {ext[u['unit']]}")
            for i, x in deps.items():
                if x["status"] not in SATISFIED or x["translations"] != trans.get(i, []):
                    ctx.append(f"{u['unit']} attempt {a}: dependency {i} lacks its committed translation")
            fb = req["feedback"]
            if [x["attempt"] for x in fb] != list(range(1, a)):
                ctx.append(f"{u['unit']} attempt {a}: feedback covers attempts {[x['attempt'] for x in fb]}")
            for x in fb:
                prev = _read(out / "units" / u["unit"] / f"attempt-{x['attempt']}" / "evaluation.json")
                if (x["outcome"], x["problems"]) != (prev["outcome"], prev["problems"]):
                    ctx.append(f"{u['unit']} attempt {a}: feedback for attempt {x['attempt']} differs from its evaluation")
    check("requests and verdicts match the Layer 1 <-> Layer 2 protocol", proto, "all requests valid; "
          "every verdict judged pass is a schema-valid pass")
    check("requests carry the unit's fragments, its migrated dependencies and earlier feedback", ctx,
          "fragments verbatim, dependencies complete with their committed translations, feedback of every "
          "earlier attempt")

    # 8. what was committed is exactly what a passing verdict (or a human) supplied
    problems = []
    for u in units:
        mine = {f: trans[f] for f in u["fragments"] if f in trans}
        if u["status"] != "migrated":
            if mine:
                problems.append(f"{u['unit']}: {u['status']} but has committed translations")
            continue
        if set(mine) != set(u["fragments"]):
            problems.append(f"{u['unit']}: translations missing for some fragments")
        if u.get("by") == "layer2":
            d = out / "units" / u["unit"] / f"attempt-{u['attempts']}"
            ev, ver = _read(d / "evaluation.json"), _read(d / "verdict.json")
            want = [Piece(**p).model_dump() for p in ver.get("translations", [])]
            got = [p for f in u["fragments"] for p in mine.get(f, [])]
            if ev["outcome"] != "pass" or sorted(map(json.dumps, want)) != sorted(map(json.dumps, got)):
                problems.append(f"{u['unit']}: committed translation is not the passing verdict's")
        elif not u.get("decisions") or u["decisions"][-1]["action"] != "accept":
            problems.append(f"{u['unit']}: migrated by a human without an accept decision")
    check("committed translations come from passing verdicts or human accepts", problems,
          f"{sum(u['status'] == 'migrated' for u in units)} migrated units")

    # 9. the migrated codebase contains every committed piece
    problems, root = [], out / "migrated"
    targets = {p["target_file"] for ps in trans.values() for p in ps}
    files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} if root.exists() else set()
    expected = {x["file"] for x in b["manifest"]["files"]} if st["assembly_mode"] == "splice" else targets
    if files != expected:
        problems.append(f"files {sorted(files ^ expected)[:5]} unexpected or missing")
    for f, ps in trans.items():
        for p in ps:
            if p["target_file"] not in files:
                continue
            have = {l.strip() for l in (root / p["target_file"]).read_text(encoding="utf-8").splitlines()}
            want = [l.strip() for l in p["code"].replace("# ...\n", "\n").splitlines()
                    if l.strip() and not SLOT.match(l)]
            lost = [l for l in want if l not in have]
            if lost:
                problems.append(f"{f}: {len(lost)} line(s) missing from {p['target_file']}, e.g. {lost[0][:60]!r}")
    if st["result"]["assembly"]["problems"]:
        problems += st["result"]["assembly"]["problems"]
    check("migrated codebase contains every committed translation", problems,
          f"{len(files)} files ({st['assembly_mode']} mode)")

    # 10. escalations are recorded with their outcome
    esc = _read(out / "escalations.json")
    problems = [f"{u['unit']}: escalated without a recorded case" for u in units
                if u["status"] == "escalated" and u["unit"] not in {e["unit"] for e in esc}]
    problems += [f"{e['unit']}: recorded final status {e['final_status']} != {by_unit[e['unit']]['status']}"
                 for e in esc if e["final_status"] != by_unit[e["unit"]]["status"]]
    check("escalations recorded with their final outcome", problems, f"{len(esc)} escalation(s)")
    return results
