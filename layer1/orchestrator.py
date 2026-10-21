"""Layer 1: the deterministic orchestrator, a LangGraph state machine over Layer 0's migration order.

Nodes (the boxes of the architecture):
  initialize     load the Layer 0 bundle, re-validate it against the sources (a bad bundle is refused), plan units
  schedule       take the next unit in migration order. Vendored code is skipped, and so is code outside the run's
                 scope (if one is set); a unit with a dependency that did not migrate is blocked (never sent)
  invoke_layer2  build the request (fragments + migrated dependencies + feedback of earlier attempts), call Layer 2
  evaluate       check the verdict against the request (protocol.evaluate); an invalid verdict is a failed attempt
  [decide]       routing: pass -> commit; fail/error with attempts left -> retry; else -> escalate
  retry          the next attempt carries the failed one's feedback (max_attempts per unit, default 3)
  commit         store the unit's translations; its dependents can now be scheduled
  escalate       record the case. hitl=defer: mark the unit escalated and go on; hitl=prompt: ask a human
  human_review   pause (LangGraph interrupt) for a decision: accept a translation / retry with guidance / skip
  finalize       assemble the migrated codebase; write state.json, translations.jsonl, escalations.json
Every transition is checkpointed to SQLite, so a crashed run resumes where it stopped and a pause for a human
survives restarts. The state holds only JSON values; nothing in it depends on time, so runs are reproducible.
"""
import copy
import fnmatch
import json
import shutil
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from layer0 import contract as bundle_contract
from sandbox.logs import write_json, write_text

from . import llm as llm_text
from . import protocol
from .assemble import assemble

SATISFIED = ("migrated", "vendored")
FINAL = ("migrated", "vendored", "escalated", "blocked", "out_of_scope")
MAX_HUMAN_RETRIES = 2
COUNTERS = ("invocations", "outcome_pass", "outcome_fail", "outcome_error", "outcome_needs_human", "invalid_verdicts",
            "retries", "migrated_units", "escalated_units", "blocked_units", "vendored_units", "out_of_scope_units",
            "human_accepted",
            "human_retries", "human_skipped", "human_invalid")


def _merge(a, b):
    return {**(a or {}), **(b or {})}


def _add(a, b):
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = out.get(k, 0) + v
    return out


def _extend(a, b):
    return (a or []) + (b or [])


class State(TypedDict, total=False):
    run: str
    aborted: Optional[list]
    units: list                                   # the plan: one entry per migration step (static)
    cursor: int                                   # index of the next unit to consider
    current: Optional[dict]                       # the unit in flight: attempt, budget, feedback, guidance
    verdict: Optional[dict]                       # raw verdict of the current attempt
    evaluation: Optional[dict]                    # Layer 1's check of it
    accepted: Optional[dict]                      # translations about to be committed, and by whom
    review: Optional[dict]                        # an escalation waiting for a human decision
    fragments: Annotated[dict, _merge]            # fragment id -> status, unit, by, blocked_by, root_causes
    units_log: Annotated[dict, _merge]            # unit -> status, attempts, scheduled/invoked/decided seq, ...
    translations: Annotated[dict, _merge]         # fragment id -> committed pieces
    escalations: Annotated[list, _extend]
    counters: Annotated[dict, _add]
    seq: int                                      # transition number: orders every decision in the run
    events: list                                  # what the last node did (logged by the driver)
    result: Optional[dict]


@dataclass
class Context:
    bundle_dir: Path
    source_root: Path
    out: Path
    layer2: Any
    max_attempts: int = 3
    hitl: str = "defer"          # defer | prompt
    vendored: str = "skip"       # skip | migrate
    llm: Any = None              # writes escalation briefs (prose only, never a decision); None: templates
    scope: Optional[list] = None  # fragment patterns to migrate (with everything they depend on); None: all
    bundle: dict = field(default_factory=dict)

    def __post_init__(self):
        self.bundle = self.bundle or bundle_contract.load(self.bundle_dir)
        self.frag = {f["id"]: f for f in self.bundle["fragments"]}
        self.edge_kinds = {(e["source"], e["target"]): e["kinds"] for e in self.bundle["graph"]["edges"]}
        mig = self.bundle["manifest"]["migration"]  # splice only within Python (in place); otherwise compose
        self.mode = "splice" if mig["target_language"].startswith("python") and \
            mig["source_language"].startswith("python") else "compose"
        self.plan_units = [{"unit": f"step-{st['step']:04d}", "fragments": st["fragments"]}
                           for st in self.bundle["order"]["steps"]]


def _unit(state):
    return state["units"][state["current"]["index"]]


def in_scope(frag, patterns):
    """The fragments a scope selects: ids matching a pattern (fnmatch, or a module / class / function prefix), and
    everything they depend on, transitively (a dependency must be migrated before its dependents)."""
    if not patterns:
        return set(frag)
    seeds = {f for f in frag for p in patterns
             if fnmatch.fnmatchcase(f, p) or f == p or f.startswith((p + ".", p + "::"))}
    todo, seen = list(seeds), set()
    while todo:
        f = todo.pop()
        if f not in seen:
            seen.add(f)
            todo += frag[f]["depends_on"]
    return seen


def _attempt_dir(c, unit, attempt):
    return c.out / "units" / unit / f"attempt-{attempt}"


def _ev(seq, node, **kw):
    return {"seq": seq, "node": node, **kw}


# ---------------------------------------------------------------------------------------------------- nodes
def initialize(state: State, runtime: Runtime[Context]):
    c = runtime.context
    failed = [f"{x['check']}: {x['detail']}" for x in bundle_contract.validate(c.bundle_dir, c.source_root)
              if not x["ok"]]
    if failed:
        return {"aborted": failed, "seq": 1, "events": [_ev(1, "initialize", outcome="bundle rejected", detail=failed)]}
    scope = in_scope(c.frag, c.scope)
    units = [{"unit": f"step-{st['step']:04d}", "step": st["step"], "wave": st["wave"], "cycle": st["cycle"],
              "fragments": st["fragments"], "vendored": all("vendored" in c.frag[f]["flags"] for f in st["fragments"]),
              "in_scope": any(f in scope for f in st["fragments"])}
             for st in c.bundle["order"]["steps"]]
    if c.scope and not any(u["in_scope"] for u in units):
        failed = [f"scope {c.scope} matches no fragment"]
        return {"aborted": failed, "seq": 1, "events": [_ev(1, "initialize", outcome="scope matches nothing",
                                                              detail=failed)]}
    man = c.bundle["manifest"]
    write_json(c.out / "plan.json", {"repo": man["repo"]["name"], "migration": man["migration"], "assembly": c.mode,
                                     "max_attempts": c.max_attempts, "hitl": c.hitl, "vendored": c.vendored,
                                     "scope": c.scope, "units": units})
    return {"aborted": None, "units": units, "cursor": 0, "current": None, "verdict": None, "evaluation": None,
            "accepted": None, "review": None, "result": None,
            "fragments": {f: {"status": "pending", "unit": u["unit"]} for u in units for f in u["fragments"]},
            "units_log": {u["unit"]: {"status": "pending", "attempts": 0} for u in units},
            "translations": {}, "escalations": [], "counters": {k: 0 for k in COUNTERS}, "seq": 1,
            "events": [_ev(1, "initialize", outcome="planned", n_units=len(units),
                           n_fragments=sum(len(u["fragments"]) for u in units), assembly=c.mode)]}


def schedule(state: State, runtime: Runtime[Context]):
    c, seq = runtime.context, state["seq"]
    info = {k: dict(v) for k, v in state["fragments"].items()}
    fr_up, un_up, events, counters = {}, {}, [], defaultdict(int)
    units, i = state["units"], state["cursor"]
    while i < len(units):
        u = units[i]
        i += 1
        seq += 1  # each decision gets its own number, also the ones taken without calling Layer 2
        if u["vendored"] and c.vendored == "skip":
            for f in u["fragments"]:
                info[f] = fr_up[f] = {"status": "vendored", "unit": u["unit"], "by": "vendored"}
            un_up[u["unit"]] = {"status": "vendored", "attempts": 0, "decided_at": seq}
            counters["vendored_units"] += 1
            events.append(_ev(seq, "schedule", unit=u["unit"], outcome="vendored", fragments=u["fragments"]))
            continue
        if not u.get("in_scope", True):
            for f in u["fragments"]:
                info[f] = fr_up[f] = {"status": "out_of_scope", "unit": u["unit"], "by": None}
            un_up[u["unit"]] = {"status": "out_of_scope", "attempts": 0, "decided_at": seq}
            counters["out_of_scope_units"] += 1
            events.append(_ev(seq, "schedule", unit=u["unit"], outcome="out_of_scope", fragments=u["fragments"]))
            continue
        deps = sorted({d for f in u["fragments"] for d in c.frag[f]["depends_on"] if d not in u["fragments"]})
        missing = [d for d in deps if info[d]["status"] not in SATISFIED]
        if missing:
            roots = sorted({r for d in missing for r in ([d] if info[d]["status"] == "escalated"
                                                         else info[d].get("root_causes", [d]))})
            for f in u["fragments"]:
                info[f] = fr_up[f] = {"status": "blocked", "unit": u["unit"], "by": None, "blocked_by": missing,
                                      "root_causes": roots}
            un_up[u["unit"]] = {"status": "blocked", "attempts": 0, "blocked_by": missing, "root_causes": roots,
                                "decided_at": seq}
            counters["blocked_units"] += 1
            events.append(_ev(seq, "schedule", unit=u["unit"], outcome="blocked", fragments=u["fragments"],
                              blocked_by=missing))
            continue
        un_up[u["unit"]] = {"status": "in_progress", "attempts": 0, "scheduled_at": seq}
        events.append(_ev(seq, "schedule", unit=u["unit"], outcome="scheduled", wave=u["wave"], cycle=u["cycle"],
                          fragments=u["fragments"]))
        return {"cursor": i, "seq": seq, "fragments": fr_up, "units_log": un_up, "counters": dict(counters),
                "events": events, "current": {"unit": u["unit"], "index": i - 1, "attempt": 1,
                                              "budget": c.max_attempts, "feedback": [], "guidance": None,
                                              "human_retries": 0, "decisions": []}}
    seq += 1
    events.append(_ev(seq, "schedule", outcome="no units left"))
    return {"cursor": i, "seq": seq, "fragments": fr_up, "units_log": un_up, "counters": dict(counters),
            "events": events, "current": None}


def build_request(c, state, u, cur):
    frs = [c.frag[f] for f in u["fragments"]]
    kinds = defaultdict(set)
    for f in u["fragments"]:
        for d in c.frag[f]["depends_on"]:
            if d not in u["fragments"]:
                kinds[d] |= set(c.edge_kinds[(f, d)])
    deps = [{"id": d, "kind": c.frag[d]["kind"], "edge_kinds": sorted(kinds[d]),
             "status": state["fragments"][d]["status"], "file": c.frag[d]["file"],
             "signature": c.frag[d]["signature"], "translations": state["translations"].get(d, [])}
            for d in sorted(kinds)]
    t, target = c.bundle["manifest"].get("target"), None
    if t:
        hinted = {h["java"].split("#")[0] for f in frs for h in f["target_hints"]}
        target = {"language": t["language"], "package": t["package"],
                  "expected_api": {k: t["expected_api"][k] for k in sorted(hinted) if k in t["expected_api"]}}
    return {"protocol": dict(protocol.PROTOCOL), "run": state["run"], "unit": u["unit"], "step": u["step"],
            "wave": u["wave"], "cycle": u["cycle"], "attempt": cur["attempt"], "max_attempts": cur["budget"],
            "migration": c.bundle["manifest"]["migration"], "assembly": c.mode, "target": target, "fragments": frs,
            "dependencies": deps, "feedback": cur["feedback"], "human_guidance": cur["guidance"]}


def invoke_layer2(state: State, runtime: Runtime[Context]):
    c, seq, cur = runtime.context, state["seq"] + 1, state["current"]
    u = _unit(state)
    req = build_request(c, state, u, cur)
    d = _attempt_dir(c, u["unit"], cur["attempt"])
    write_json(d / "request.json", req)
    start = time.time()
    try:
        raw = c.layer2.process(copy.deepcopy(req))  # a copy: Layer 2 cannot change Layer 1's state
    except Exception as e:  # noqa: BLE001  any failure of Layer 2 is a failed attempt, not a failed run
        raw = {"_exception": f"{type(e).__name__}: {e}"}
    raw = json.loads(json.dumps(raw, default=str))
    write_json(d / "verdict.json", raw)
    log = dict(state["units_log"][u["unit"]])
    log.setdefault("invoked_at", seq)
    return {"verdict": raw, "seq": seq, "units_log": {u["unit"]: log}, "counters": {"invocations": 1},
            "events": [_ev(seq, "invoke_layer2", unit=u["unit"], attempt=cur["attempt"],
                           seconds=round(time.time() - start, 3))]}


def _committed(state, c):
    return [p for pieces in state["translations"].values() for p in pieces] if c.mode == "compose" else []


def evaluate(state: State, runtime: Runtime[Context]):
    c, seq, cur = runtime.context, state["seq"] + 1, state["current"]
    u = _unit(state)
    ev = protocol.evaluate(state["verdict"], u["unit"], cur["attempt"], [c.frag[f] for f in u["fragments"]], c.mode,
                           _committed(state, c))
    pieces = ev.pop("pieces")
    write_json(_attempt_dir(c, u["unit"], cur["attempt"]) / "evaluation.json", ev)
    return {"evaluation": ev, "seq": seq,
            "accepted": {"pieces": pieces, "by": "layer2"} if ev["outcome"] == "pass" else None,
            "counters": {f"outcome_{ev['outcome']}": 1, "invalid_verdicts": 0 if ev["valid"] else 1},
            "events": [_ev(seq, "evaluate", unit=u["unit"], attempt=cur["attempt"], outcome=ev["outcome"],
                           valid=ev["valid"], problems=ev["problems"][:3])]}


def decide(state: State):
    ev, cur = state["evaluation"], state["current"]
    if ev["outcome"] == "pass":
        return "commit"
    if ev["outcome"] != "needs_human" and cur["attempt"] < cur["budget"]:
        return "retry"
    return "escalate"


def _feedback(state):
    ev = state["evaluation"]
    return {"attempt": state["current"]["attempt"], "outcome": ev["outcome"], "reason": ev["reason"],
            "problems": ev["problems"], "evidence": ev["evidence"]}


def retry(state: State, runtime: Runtime[Context]):
    seq, cur = state["seq"] + 1, dict(state["current"])
    cur["feedback"] = cur["feedback"] + [_feedback(state)]
    cur["attempt"] += 1
    return {"current": cur, "verdict": None, "evaluation": None, "seq": seq, "counters": {"retries": 1},
            "events": [_ev(seq, "retry", unit=cur["unit"], attempt=cur["attempt"])]}


def commit(state: State, runtime: Runtime[Context]):
    seq, cur, acc = state["seq"] + 1, state["current"], state["accepted"]
    u = _unit(state)
    by_frag = defaultdict(list)
    for p in acc["pieces"]:
        by_frag[p["fragment"]].append(p)
    log = {**state["units_log"][u["unit"]], "status": "migrated", "by": acc["by"], "attempts": cur["attempt"],
           "human_retries": cur["human_retries"], "decisions": cur["decisions"], "decided_at": seq}
    return {"translations": dict(by_frag), "units_log": {u["unit"]: log},
            "fragments": {f: {"status": "migrated", "unit": u["unit"], "by": acc["by"]} for f in u["fragments"]},
            "current": None, "verdict": None, "evaluation": None, "accepted": None, "review": None, "seq": seq,
            "counters": {"migrated_units": 1},
            "events": [_ev(seq, "commit", unit=u["unit"], outcome="migrated", by=acc["by"], attempts=cur["attempt"],
                           fragments=u["fragments"])]}


def _give_up(state, seq, case):
    """The unit stays unmigrated; its dependents will be blocked when they are scheduled."""
    cur, u = state["current"], _unit(state)
    log = {**state["units_log"][u["unit"]], "status": "escalated", "attempts": cur["attempt"],
           "human_retries": cur["human_retries"], "decisions": cur["decisions"], "escalation": case,
           "decided_at": seq}
    return {"units_log": {u["unit"]: log}, "current": None, "verdict": None, "evaluation": None, "accepted": None,
            "review": None, "counters": {"escalated_units": 1},
            "fragments": {f: {"status": "escalated", "unit": u["unit"], "by": None} for f in u["fragments"]}}


def escalate(state: State, runtime: Runtime[Context]):
    c, seq, cur, ev = runtime.context, state["seq"] + 1, state["current"], state["evaluation"]
    u = _unit(state)
    case = {"unit": u["unit"], "fragments": u["fragments"],
            "reason": "needs_human" if ev["outcome"] == "needs_human" else "retries_exhausted",
            "attempts": cur["attempt"], "last_outcome": ev["outcome"], "last_reason": ev["reason"],
            "problems": ev["problems"], "seq": seq}
    event = _ev(seq, "escalate", unit=u["unit"], reason=case["reason"], attempts=cur["attempt"], hitl=c.hitl,
                fragments=u["fragments"])
    _brief(c, u, case)
    if c.hitl == "defer":
        return {**_give_up(state, seq, case), "escalations": [case], "seq": seq, "events": [event]}
    return {"review": {"case": case, "problems": []}, "escalations": [case], "seq": seq, "events": [event]}


def _brief(c, u, case):
    """The escalation brief for the reviewer (units/<unit>/brief.json), written once per escalation. It lives
    outside the state: an LLM's prose must not make the run's decisions or its state non-reproducible."""
    path = c.out / "units" / u["unit"] / "brief.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("attempts") == case["attempts"]:
            return old
    attempts = []
    for a in range(1, case["attempts"] + 1):
        d = _attempt_dir(c, u["unit"], a)
        if (d / "evaluation.json").exists():
            ev = json.loads((d / "evaluation.json").read_text(encoding="utf-8"))
            attempts.append({"attempt": a, "outcome": ev["outcome"], "reason": ev["reason"],
                             "problems": ev["problems"][:3]})
    unit_of = {f: x["unit"] for x in c.plan_units for f in x["fragments"]}
    dependents = {unit_of[d] for f in u["fragments"] for d in c.frag[f]["dependents"] if d not in u["fragments"]}
    brief = llm_text.escalation_brief(c.llm, case, [c.frag[f] for f in u["fragments"]], attempts, len(dependents),
                                      c.bundle["manifest"]["migration"], log=c.out / "llm" / "calls.jsonl")
    brief["attempts"] = case["attempts"]
    write_json(path, brief)
    return brief


def human_review(state: State, runtime: Runtime[Context]):
    c, cur, rv = runtime.context, state["current"], state["review"]
    u = _unit(state)
    frs = [c.frag[f] for f in u["fragments"]]
    last = _attempt_dir(c, u["unit"], cur["attempt"])
    decision = interrupt({
        "unit": u["unit"], "case": rv["case"], "problems_with_last_decision": rv["problems"],
        "fragments": [{"id": f["id"], "kind": f["kind"], "file": f["file"], "source": f["source"]} for f in frs],
        "brief": _brief(c, u, rv["case"])["text"],
        "assembly": c.mode, "last_request": str(last / "request.json"), "last_verdict": str(last / "verdict.json"),
        "human_retries_left": MAX_HUMAN_RETRIES - cur["human_retries"],
        "actions": {"accept": "{action, translations: [piece, ...], note}",
                    "retry": "{action, guidance}  (a fresh budget of attempts)", "skip": "{action, note}"}})
    seq = state["seq"] + 1
    decision = decision if isinstance(decision, dict) else {}
    action = decision.get("action")
    record = {"seq": seq, "action": action, "note": decision.get("note") or decision.get("guidance") or ""}
    cur = {**cur, "decisions": cur["decisions"] + [record]}
    event = _ev(seq, "human_review", unit=u["unit"], action=action)
    if action == "accept":
        try:
            pieces = [protocol.Piece.model_validate(p) for p in decision.get("translations") or []]
            problems = protocol.check_pieces(pieces, frs, c.mode,
                                             [protocol.Piece(**p) for p in _committed(state, c)])
        except (ValidationError, TypeError) as e:
            problems = [f"translations do not match the schema: {str(e).splitlines()[0]}"]
        if not problems:
            return {"current": cur, "review": None, "seq": seq, "counters": {"human_accepted": 1},
                    "accepted": {"pieces": [p.model_dump() for p in pieces], "by": "human"}, "events": [event]}
    elif action == "retry" and cur["human_retries"] < MAX_HUMAN_RETRIES:
        cur.update(feedback=cur["feedback"] + [_feedback(state)], attempt=cur["attempt"] + 1,
                   budget=cur["attempt"] + c.max_attempts, guidance=record["note"] or None,
                   human_retries=cur["human_retries"] + 1)
        return {"current": cur, "review": None, "verdict": None, "evaluation": None, "seq": seq,
                "counters": {"human_retries": 1}, "events": [event]}
    elif action == "skip":
        st = {**state, "current": cur}
        return {**_give_up(st, seq, rv["case"]), "seq": seq, "counters": {"escalated_units": 1, "human_skipped": 1},
                "events": [event]}
    else:
        problems = ["no human retries left" if action == "retry" else f"unknown action {action!r}"]
    return {"current": cur, "review": {**rv, "problems": problems}, "seq": seq, "counters": {"human_invalid": 1},
            "events": [{**event, "problems": problems}]}


def after_review(state: State):
    if state.get("accepted"):
        return "commit"
    if state.get("review"):
        return "human_review"
    return "invoke_layer2" if state.get("current") else "schedule"


def finalize(state: State, runtime: Runtime[Context]):
    c, seq = runtime.context, state["seq"] + 1
    if state.get("aborted"):
        result = {"ok": False, "aborted": state["aborted"]}
        write_json(c.out / "state.json", result)
        return {"result": result, "seq": seq, "events": [_ev(seq, "finalize", outcome="aborted")]}
    status = {f: x["status"] for f, x in state["fragments"].items()}
    shutil.rmtree(c.out / "migrated", ignore_errors=True)
    asm = assemble(c.mode, c.bundle, status, state["translations"], c.source_root, c.out / "migrated")
    write_text(c.out / "translations.jsonl", "".join(
        json.dumps({"fragment": f, "unit": u["unit"], "by": state["units_log"][u["unit"]].get("by"),
                    "pieces": state["translations"][f]}) + "\n"
        for u in state["units"] for f in u["fragments"] if f in state["translations"]))
    final = {u: x["status"] for u, x in state["units_log"].items()}
    write_json(c.out / "escalations.json", [{**e, "final_status": final[e["unit"]],
                                             "brief": f"units/{e['unit']}/brief.json"} for e in state["escalations"]])
    units = [{**u, **state["units_log"][u["unit"]]} for u in state["units"]]
    by_status = defaultdict(int)
    for u in units:
        by_status[u["status"]] += 1
    frag_status = defaultdict(int)
    for x in state["fragments"].values():
        frag_status[x["status"]] += 1
    result = {"ok": all(u["status"] in FINAL for u in units) and not asm["problems"],
              "units": len(units), "units_by_status": dict(sorted(by_status.items())),
              "fragments": len(status), "fragments_by_status": dict(sorted(frag_status.items())),
              "counters": state["counters"], "assembly": asm}
    man = c.bundle["manifest"]
    write_json(c.out / "state.json", {"repo": man["repo"]["name"], "migration": man["migration"],
                                      "assembly_mode": c.mode, "max_attempts": c.max_attempts, "hitl": c.hitl,
                                      "result": result, "units": units, "fragments": state["fragments"]})
    return {"result": result, "seq": seq, "events": [_ev(seq, "finalize", outcome="done", **{
        k: result[k] for k in ("units_by_status", "fragments_by_status")})]}


# ---------------------------------------------------------------------------------------------------- graph
def build_graph():
    g = StateGraph(State, context_schema=Context)
    for name, fn in [("initialize", initialize), ("schedule", schedule), ("invoke_layer2", invoke_layer2),
                     ("evaluate", evaluate), ("retry", retry), ("commit", commit), ("escalate", escalate),
                     ("human_review", human_review), ("finalize", finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "initialize")
    g.add_conditional_edges("initialize", lambda s: "finalize" if s.get("aborted") else "schedule",
                            ["schedule", "finalize"])
    g.add_conditional_edges("schedule", lambda s: "invoke_layer2" if s.get("current") else "finalize",
                            ["invoke_layer2", "finalize"])
    g.add_edge("invoke_layer2", "evaluate")
    g.add_conditional_edges("evaluate", decide, ["commit", "retry", "escalate"])
    g.add_edge("retry", "invoke_layer2")
    g.add_edge("commit", "schedule")
    g.add_conditional_edges("escalate", lambda s: "human_review" if s.get("review") else "schedule",
                            ["human_review", "schedule"])
    g.add_conditional_edges("human_review", after_review, ["commit", "human_review", "invoke_layer2", "schedule"])
    g.add_edge("finalize", END)
    return g


def mermaid():
    return build_graph().compile().get_graph().draw_mermaid()


def _progress(e, total):
    node, unit = e["node"], e.get("unit", "")
    frs = e.get("fragments") or []
    name = frs[0] + (f" (+{len(frs) - 1} in cycle)" if len(frs) > 1 else "") if frs else ""
    if node == "commit":
        return f"   {unit}  migrated   {name}" + (f"  [{e['attempts']} attempts]" if e["attempts"] > 1 else "") + \
            ("  [by human]" if e["by"] == "human" else "")
    if node == "schedule" and e["outcome"] == "blocked":
        return f"   {unit}  BLOCKED    {name}  <- {', '.join(e['blocked_by'][:3])}"
    if node == "escalate":
        return f"   {unit}  ESCALATED  {name}  ({e['reason']} after {e['attempts']} attempt(s); hitl={e['hitl']})"
    if node == "evaluate" and e["outcome"] != "pass":
        return f"   {unit}  attempt {e['attempt']}: {e['outcome']}" + \
            (f" - {e['problems'][0]}" if e["problems"] else "")
    if node == "human_review":
        return f"   {unit}  human decision: {e['action']}" + (f" - {e['problems'][0]}" if e.get("problems") else "")
    if node == "initialize":
        return f"   initialize: {e['outcome']}" + (f" - {e['detail'][0]}" if e.get("detail") else
                                                   f", {e['n_units']} units / {e['n_fragments']} fragments, "
                                                   f"assembly={e['assembly']}")
    return None


def migrate(bundle_dir, source_root, out, layer2, max_attempts=3, hitl="defer", vendored="skip", human=None,
            resume=False, echo=print, verbose=True, llm=None, scope=None):
    """Run (or resume) Layer 1 on one bundle; everything is written to `out`. `human(payload) -> decision` answers
    escalations when hitl="prompt"; without it (or if it returns None) the run pauses there, to be resumed later.
    Returns {"status": "finished" | "paused", "result" | "pending"}."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "checkpoints.sqlite"
    if not resume and db.exists():
        raise FileExistsError(f"{db} exists: resume that run, or use a fresh directory")
    ctx = Context(Path(bundle_dir), Path(source_root), out, layer2, max_attempts, hitl, vendored, llm, scope)
    conn = sqlite3.connect(db, check_same_thread=False)
    # WAL + NORMAL: a commit survives the process crashing (what resume is for) without an fsync per transition
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        app = build_graph().compile(checkpointer=SqliteSaver(conn))
        n_units = len(ctx.bundle["order"]["steps"])
        cfg = {"configurable": {"thread_id": "layer1"},
               "recursion_limit": n_units * (4 + 3 * max_attempts) * (1 + MAX_HUMAN_RETRIES) + 100}
        if resume and not app.get_state(cfg).values:
            raise RuntimeError(f"nothing to resume in {db}")
        inp = None if resume else {"run": out.as_posix().split("/logs/")[-1], "seq": 0}
        with open(out / "events.jsonl", "a", encoding="utf-8") as log:
            while True:
                for update in app.stream(inp, cfg, context=ctx, stream_mode="updates"):
                    for node, val in update.items():
                        for e in (val or {}).get("events", []) if node != "__interrupt__" else []:
                            log.write(json.dumps(e) + "\n")
                            line = _progress(e, n_units)
                            if line and verbose:
                                echo(line)
                    log.flush()
                snap = app.get_state(cfg)
                if not snap.interrupts:
                    break
                payload = snap.interrupts[0].value
                decision = human(payload) if human else None
                if decision is None:  # nobody to ask: the run waits, checkpointed, for --resume
                    return {"status": "paused", "pending": payload}
                inp = Command(resume=decision)
        result = app.get_state(cfg).values["result"]
    finally:
        conn.close()
    db.unlink()  # finished: the checkpoints are only needed to resume an unfinished run
    return {"status": "finished", "result": result}
