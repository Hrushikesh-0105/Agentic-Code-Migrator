"""Human-readable report of one Layer 1 run (report.md)."""
import json
from collections import defaultdict
from pathlib import Path


def _cell(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


def _provenance(t):
    if t["source"] == "llm":
        note = f"; {t['note']}" if t.get("note") else ""
        via = {"groq": "Groq", "gemini": "Gemini API"}.get(t.get("provider"), t.get("provider") or "LLM")
        return f"_Written by {t['model']} ({via}) and checked against the run's facts{note}._"
    return f"_Template text: {t['note']}._"


def layer2_evidence(out, st):
    """What Layer 2 (the agents) showed for each unit it was sent: differential tests, repairs, cost."""
    out, rows = Path(out), []
    tokens, calls, mcp = defaultdict(int), defaultdict(int), 0
    for u in st["units"]:
        if not u.get("attempts"):
            continue
        last = None
        for a in range(1, u["attempts"] + 1):
            p = out / "units" / u["unit"] / f"attempt-{a}" / "verdict.json"
            if not p.exists():
                continue
            v = json.loads(p.read_text(encoding="utf-8"))
            for k, n in (v.get("usage") or {}).items():
                if k in ("mcp_calls", "seconds"):
                    continue
                agent, _, what = k.partition("_")
                if what in ("prompt_tokens", "completion_tokens"):
                    tokens[agent] += n
                elif what == "calls":
                    calls[agent] += n
            mcp += int((v.get("usage") or {}).get("mcp_calls", 0))
            last = v
        ev = (last or {}).get("evidence") or {}
        repairs = sum(1 for t in (last or {}).get("trace", []) if t.get("agent") == "repair")
        rows.append({"unit": u["unit"], "fragments": u["fragments"], "status": u["status"], "attempts": u["attempts"],
                     "outcome": (last or {}).get("outcome"), "tests": ev.get("tests_generated", 0),
                     "passed": ev.get("tests_passed", 0), "failed": ev.get("tests_failed", 0),
                     "repair_rounds": repairs, "notes": ev.get("notes", "") or (last or {}).get("reason", "")})
    mig = [r for r in rows if r["status"] == "migrated"]
    return {"units_sent": len(rows),
            "units_verified_by_tests": sum(r["tests"] > 0 and r["passed"] == r["tests"] for r in mig),
            "units_compiled_only": sum(r["tests"] == 0 for r in mig),
            "differential_tests": sum(r["tests"] for r in mig), "tests_equivalent": sum(r["passed"] for r in mig),
            "units_repaired": sum(r["repair_rounds"] > 0 or r["attempts"] > 1 for r in mig),
            "llm_tokens": dict(sorted(tokens.items())), "llm_calls": dict(sorted(calls.items())), "mcp_calls": mcp,
            "rows": rows}


def render(out, validation, build=None, layer2="dummy", bundle=None, summary=None, l2=None):
    out = Path(out)
    st = json.loads((out / "state.json").read_text(encoding="utf-8"))
    if "units" not in st:
        return f"# Layer 1: run aborted\n\nThe bundle was rejected:\n\n" + \
            "".join(f"- {p}\n" for p in st.get("aborted", []))
    res, units = st["result"], st["units"]
    c = res["counters"]
    L = [f"# Layer 1: {st['repo']}", "",
         f"- regime: **{st['migration']['regime']}**, assembly: **{st['assembly_mode']}**, Layer 2: **{layer2}**",
         f"- max attempts per unit: {st['max_attempts']}, human-in-the-loop: {st['hitl']}",
         f"- bundle: `{bundle}`" if bundle else "", ""]
    if summary:
        L += ["## Summary", "", summary["text"], "", _provenance(summary), ""]
    L += ["## Counts", "", "| | count |", "|---|---:|"]
    for k, v in res["units_by_status"].items():
        L.append(f"| units {k} | {v} |")
    for k, v in res["fragments_by_status"].items():
        L.append(f"| fragments {k} | {v} |")
    for k in ("invocations", "retries", "invalid_verdicts", "outcome_fail", "outcome_error", "outcome_needs_human",
              "human_accepted", "human_retries", "human_skipped"):
        if c.get(k) or k in ("invocations", "retries"):
            L.append(f"| {k.replace('_', ' ')} | {c[k]} |")
    L += ["", f"## Contract ({sum(x['ok'] for x in validation)}/{len(validation)})", ""]
    L += [f"- {'PASS' if x['ok'] else '**FAIL**'} {x['check']}: {x['detail']}" for x in validation]
    if build:
        L += ["", "## Build check", "",
              f"{'Builds' if build['ok'] else '**Does not build**'}: {build['tool']}, {build['files']} files"
              + (" (a report on the output, not a Layer 1 criterion)" if not build["ok"] else "")]
        L += [f"- `{e}`" for e in build["errors"][:10]]
    if l2:
        L += ["", "## Layer 2 verification (agents)", "",
              f"- units sent to Layer 2: {l2['units_sent']}; migrated and **verified by differential tests**: "
              f"{l2['units_verified_by_tests']}; migrated on a compile check only (nothing callable, or not testable "
              f"yet): {l2['units_compiled_only']}",
              f"- differential tests on migrated units: {l2['tests_equivalent']}/{l2['differential_tests']} "
              f"equivalent; units that needed repair rounds or retries: {l2['units_repaired']}",
              "- LLM tokens by agent: " + ", ".join(f"{a} {n}" for a, n in l2["llm_tokens"].items())
              + f"; LLM calls: {sum(l2['llm_calls'].values())}; MCP tool calls: {l2['mcp_calls']}", "",
              "| unit | fragments | status | attempts | tests equivalent | repair rounds (last attempt) | evidence |",
              "|---|---|---|---:|---:|---:|---|"]
        for r in l2["rows"]:
            L.append(f"| {r['unit']} | {_cell(', '.join(f.split('.')[-1] for f in r['fragments']))} | {r['status']} | "
                     f"{r['attempts']} | {r['passed']}/{r['tests']} | {r['repair_rounds']} | {_cell(r['notes'][:220])} |")
    waves = defaultdict(lambda: defaultdict(int))
    for u in units:
        waves[u["wave"]][u["status"]] += 1
    statuses = sorted({u["status"] for u in units})
    L += ["", "## Progress by wave", "", "| wave | units | " + " | ".join(statuses) + " |",
          "|---:|---:|" + "---:|" * len(statuses)]
    for w in sorted(waves):
        L.append(f"| {w} | {sum(waves[w].values())} | " + " | ".join(str(waves[w][s]) for s in statuses) + " |")
    notable = [u for u in units if u["status"] in ("escalated", "blocked") or u["attempts"] > 1 or u.get("by") == "human"]
    L += ["", f"## Units needing attention ({len(notable)})", ""]
    if notable:
        L += ["| unit | fragments | status | attempts | detail |", "|---|---|---|---:|---|"]
        for u in notable:
            detail = ("blocked by " + ", ".join(u["blocked_by"]) + "; root cause " + ", ".join(u["root_causes"])
                      if u["status"] == "blocked" else
                      u["escalation"]["reason"] + ": " + u["escalation"]["last_reason"] if u["status"] == "escalated"
                      else "by human" if u.get("by") == "human" else "migrated after retries")
            L.append(f"| {u['unit']} | {_cell(', '.join(u['fragments']))} | {u['status']} | {u['attempts']} | "
                     f"{_cell(detail)} |")
    else:
        L.append("None: every unit was migrated on its first attempt (or is vendored).")
    esc = [u for u in units if (out / "units" / u["unit"] / "brief.json").exists()]
    if esc:
        L += ["", f"## Escalation briefs ({len(esc)})", ""]
        for u in esc:
            b = json.loads((out / "units" / u["unit"] / "brief.json").read_text(encoding="utf-8"))
            L += [f"### {u['unit']}: {', '.join(u['fragments'])} (now {u['status']})", "", b["text"], "",
                  _provenance(b), ""]
    vend = [u for u in units if u["status"] == "vendored"]
    if vend:
        L += ["", f"Vendored (not migrated, not sent to Layer 2): {len(vend)} units, e.g. "
                  + ", ".join(f"`{u['fragments'][0]}`" for u in vend[:3])]
    L += ["", "## Migrated codebase", "", f"`migrated/`, {len(res['assembly']['files'])} files:", ""]
    L += [f"- `{p}` ({n} lines)" for p, n in res["assembly"]["files"].items()]
    L += [f"- **problem**: {p}" for p in res["assembly"]["problems"]]
    return "\n".join(x for x in L if x is not None) + "\n"
