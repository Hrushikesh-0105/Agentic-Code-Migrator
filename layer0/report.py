"""report.md: a human-readable view of one Layer 0 bundle (host side, standard library only)."""
from collections import Counter

from .contract import load


def _cell(x):
    return str(x).replace("|", "\\|")


def render(bundle, validation, quality=None):
    b = load(bundle)
    man, graph, order = b["manifest"], b["graph"], b["order"]
    frs = {r["id"]: r for r in b["fragments"]}
    st, mig = man["stats"], man["migration"]
    out = [f"# Layer 0: {man['repo']['name']}", "",
           f"`{mig['regime']}` · source root `{man['repo']['source_root']}` · created {man['created']}", ""]

    passed = sum(v["ok"] for v in validation)
    out += ["## Summary", "", "| | |", "|---|---|",
            f"| files in scope / excluded | {len(man['files'])} / {len(man['excluded_files'])} |",
            f"| fragments | {st['fragments']} ({', '.join(f'{v} {k}' for k, v in st['by_kind'].items())}) |",
            f"| dependency edges | {st['edges']} (found by: "
            + ", ".join(f"{k} {v}" for k, v in graph["edge_sources"].items()) + ") |",
            f"| migration steps / waves | {st['steps']} / {st['waves']} (steps per wave: {order['steps_per_wave']}) |",
            f"| cycle steps (largest) | {st['cycles']} ({st['largest_cycle']} fragments) |",
            f"| lines of code | {st['loc']} |",
            f"| fragments with hazards / Java target hints | {st['fragments_with_hazards']} / "
            f"{st['fragments_with_target_hints']} |",
            f"| flags | {', '.join(f'{k} {v}' for k, v in sorted(st['flags'].items())) or 'none'} |",
            f"| contract validation | {passed}/{len(validation)} checks passed |", ""]

    out += ["## Contract validation", ""] + \
           [f"- {'PASS' if v['ok'] else '**FAIL**'} {v['check']}: {v['detail']}" for v in validation] + [""]

    if quality:
        dropped = graph.get("dropped_unconfirmed", [])
        out += ["## Graph quality", "",
                "Measured from the source text, independently of the tools that built the graph (see layer0/audit.py).",
                "", f"- **Edge evidence** {quality['edge_evidence']}: call/reference/inheritance edges whose target is "
                    "named in the source fragment's code.",
                f"- **Call recall** {quality['call_recall']}: plain calls `name(...)` to a uniquely named project "
                "function or class that have an edge.",
                f"- **Dropped** {len(dropped)} pyan3/JARVIS edge(s) that neither LibCST nor the code supports, e.g. "
                + ", ".join(f"`{d[0]}` -> `{d[1]}` ({d[3]})" for d in dropped[:3]) if dropped else
                "- **Dropped** no pyan3/JARVIS edges."]
        out += [f"  - not evidenced: `{u[0]}` -> `{u[1]}` ({', '.join(u[2])})" for u in quality["unevidenced_edges"][:10]]
        out += [f"  - missed call: `{m[0]}` -> `{m[1]}`" for m in quality["missed_calls"][:10]] + [""]

    out += ["## Migration order", "",
            "Bottom-up: each step is migrated after every step it depends on. A cycle step is migrated as one unit.",
            "", "| step | wave | fragment(s) | kind | LOC | depends on | hazards | flags |",
            "|---:|---:|---|---|---:|---:|---:|---|"]
    for s in order["steps"]:
        for i, fid in enumerate(s["fragments"]):
            r = frs[fid]
            out.append(f"| {s['step'] if i == 0 else ''} | {s['wave'] if i == 0 else ''} | "
                       f"`{_cell(fid)}`{' (cycle)' if s['cycle'] and i == 0 else ''} | {r['kind']} | {r['loc']} | "
                       f"{len(r['depends_on'])} | {sum(len(h.get('lines', [1])) for h in r['hazards'])} | "
                       f"{', '.join(r['flags'])} |")
    out.append("")

    if graph["cycles"]:
        out += ["## Cycles", ""] + [f"- {', '.join(f'`{c}`' for c in cyc)}" for cyc in graph["cycles"]] + [""]

    rules = Counter()
    for r in frs.values():
        for h in r["hazards"]:
            rules[f"{h['tool']}: {h['rule']}"] += len(h.get("lines", [1]))
    if rules:
        out += ["## Migration hazards", "", "| tool: rule | sites |", "|---|---:|"] + \
               [f"| {k} | {v} |" for k, v in rules.most_common()] + [""]

    feats = Counter()
    examples = {}
    for r in frs.values():
        for k in r["features"]:
            feats[k] += 1
            examples.setdefault(k, r["id"])
    if feats:
        out += ["## Language features that need deliberate handling", "",
                "Fragments using each feature (dynamic Python constructs have no direct Java equivalent).", "",
                "| feature | fragments | e.g. |", "|---|---:|---|"] + \
               [f"| {k} | {v} | `{examples[k]}` |" for k, v in feats.most_common()] + [""]

    ext = graph["external_apis"]
    if ext:
        out += ["## External APIs used", ""]
        for cat, names in ext.items():
            top = list(names.items())[:15]
            out.append(f"- **{cat}** ({len(names)}): " + ", ".join(f"`{n}`" for n, _ in top)
                       + (" ..." if len(names) > 15 else ""))
        out.append("")

    tgt = man.get("target")
    if tgt:
        linked = {h["java"].split("#")[0] for r in frs.values() for h in r["target_hints"]}
        out += ["## Java target", "",
                f"Package `{tgt['package']}`, {len(tgt['test_files'])} test files, {tgt['test_methods']} `@Test` methods. "
                "The tests expect these project classes (the translation must provide them):", "",
                "| class | constructed | methods | static methods | linked fragment(s) |", "|---|---:|---|---|---|"]
        for cls, u in tgt["expected_api"].items():
            frags = sorted({fid for fid, r in frs.items() for h in r["target_hints"] if h["java"].split("#")[0] == cls})
            out.append(f"| `{cls}` | {u['constructed']} | {', '.join(u['methods'])} | {', '.join(u['static_methods'])} | "
                       f"{len(frags)}{'' if cls in linked else ' (no match: name differs from the Python code)'} |")
        if tgt["third_party_imports"]:
            out += ["", "Third-party Java imports in the tests: " + ", ".join(f"`{x}`" for x in tgt["third_party_imports"])]
        out.append("")

    if man["excluded_files"]:
        out += ["## Excluded files", ""] + [f"- `{x['file']}`: {x['reason']}" for x in man["excluded_files"]] + [""]
    if man["warnings"]:
        out += ["## Warnings", ""] + [f"- {w}" for w in man["warnings"]] + [""]
    return "\n".join(out)
