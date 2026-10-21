"""The Layer 0 -> Layer 1 bundle contract, and a validator that re-checks a bundle against the original sources.

Host side: standard library only (Python 3.8+). A bundle is a directory:
    manifest.json           metadata: repo, languages, files in/out of scope, Java target API, toolchain, stats
    fragments.jsonl         one fragment per line (the unit Layer 1 schedules and Layer 2 migrates)
    dependency_graph.json   fragment -> fragment edges ("source depends on target"), modules, externals, cycles
    migration_order.json    bottom-up steps; each step lists its fragments, its wave and whether it is a cycle
See layer0/README.md for every field.
"""
import json
import re
from pathlib import Path

FORMAT_NAME, FORMAT_VERSION = "acm-layer0-bundle", "1.0"
FILES = ("manifest.json", "fragments.jsonl", "dependency_graph.json", "migration_order.json")
KINDS = {"module_header", "module_init", "class", "function", "method"}
FRAGMENT_KEYS = {"id", "kind", "qualname", "module", "file", "parent", "children", "segments", "loc", "source",
                 "signature", "depends_on", "dependents", "external_apis", "features", "metrics",
                 "branch_conditions", "hazards", "diagnostics", "order", "flags", "target_hints"}
SEPARATOR = "# ...\n"  # joins the text of a fragment's segments in `source`
CONTROL = re.compile(rb"\b(if|elif|elseif|else|while|for|foreach|except|catch|with|assert|match|case|switch|do|"
                     rb"when|until|try)\b|[?]")


def load(bundle):
    bundle = Path(bundle)
    read = lambda name: json.loads((bundle / name).read_text(encoding="utf-8"))  # noqa: E731
    lines = (bundle / "fragments.jsonl").read_text(encoding="utf-8").splitlines()
    return {"manifest": read("manifest.json"), "graph": read("dependency_graph.json"),
            "order": read("migration_order.json"), "fragments": [json.loads(l) for l in lines if l.strip()]}


def _code_line(b):
    s = b.strip()
    return bool(s) and not s.startswith(b"#")


def code_lines(data, language):
    """1-based numbers of the lines of `data` that hold code (not blank, not only a comment), per language:
    `#` for Python; `//` and `/* ... */` for the C family, Java, C#, Go, Rust, JavaScript; `%` and `%{ ... %}`
    for Matlab."""
    lines = data.splitlines()
    if language in (None, "python2", "python3"):
        return {i for i, l in enumerate(lines, 1) if _code_line(l)}
    if language == "matlab":
        out, block = set(), False
        for i, l in enumerate(lines, 1):
            s = l.strip()
            if s in (b"%{", b"%%{"):
                block = True
                continue
            if block:
                block = s not in (b"%}", b"%%}")
                continue
            if s and not s.startswith((b"%", b"...")):
                out.add(i)
        return out
    out, block = set(), False
    for i, l in enumerate(lines, 1):
        s, code = l, False
        while s:
            if block:
                j = s.find(b"*/")
                if j < 0:
                    break
                block, s = False, s[j + 2:]
                continue
            t = s.lstrip()
            if not t or t.startswith(b"//"):
                break
            if t.startswith(b"/*"):
                block, s = True, t[2:]
                continue
            code = True
            j = t.find(b"/*")
            if j < 0:
                break
            k = t.find(b"*/", j + 2)
            if k < 0:
                block = True
                break
            s = t[k + 2:]
        if code:
            out.add(i)
    return out


def validate(bundle, source_root):
    """Returns [{check, ok, detail}]. `source_root` is the directory the bundle's file paths are relative to."""
    results = []

    def check(name, problems, detail_ok=""):
        results.append({"check": name, "ok": not problems,
                        "detail": detail_ok if not problems else "; ".join(problems[:8])
                        + (f" (+{len(problems) - 8} more)" if len(problems) > 8 else "")})

    missing = [f for f in FILES if not (Path(bundle) / f).exists()]
    if missing:
        check("bundle files present", [f"missing {m}" for m in missing])
        return results
    b = load(bundle)
    man, graph, order, frs = b["manifest"], b["graph"], b["order"], b["fragments"]
    fmt = man.get("format", {})
    check("bundle files present and format recognised",
          [] if fmt.get("name") == FORMAT_NAME and fmt.get("version") == FORMAT_VERSION else [f"format {fmt}"],
          f"{FORMAT_NAME} {FORMAT_VERSION}")

    # -- fragment schema
    ids, problems = {}, []
    for r in frs:
        miss = FRAGMENT_KEYS - set(r)
        if miss:
            problems.append(f"{r.get('id')}: missing {sorted(miss)}")
        if r.get("kind") not in KINDS:
            problems.append(f"{r.get('id')}: bad kind {r.get('kind')}")
        if r.get("id") in ids:
            problems.append(f"duplicate id {r['id']}")
        if not r.get("segments"):
            problems.append(f"{r.get('id')}: no segments")
        ids[r.get("id")] = r
    for r in frs:
        if r.get("parent") is not None and (r["parent"] not in ids or r["id"] not in ids[r["parent"]]["children"]):
            problems.append(f"{r['id']}: parent/children mismatch")
    check("fragment schema (keys, kinds, unique ids, parent/children)", problems, f"{len(frs)} fragments")
    if problems:
        return results

    # -- source fidelity: every segment is exactly the original bytes, and `source` is their concatenation
    root, raw, problems = Path(source_root), {}, []
    for f in {r["file"] for r in frs} | {x["file"] for x in man["files"]}:
        raw[f] = (root / f).read_bytes()
    for r in frs:
        data, offs = raw[r["file"]], [0]
        for line in data.splitlines(keepends=True):
            offs.append(offs[-1] + len(line))
        texts, prev_end = [], 0
        for s in r["segments"]:
            if not (1 <= s["start_line"] <= s["end_line"] < len(offs)) or s["start_line"] <= prev_end:
                problems.append(f"{r['id']}: bad/overlapping segment {s}")
                continue
            prev_end = s["end_line"]
            if (s["start_byte"], s["end_byte"]) != (offs[s["start_line"] - 1], offs[s["end_line"]]):
                problems.append(f"{r['id']}: byte span does not match lines {s['start_line']}-{s['end_line']}")
            texts.append(data[s["start_byte"]:s["end_byte"]].decode("utf-8", "replace"))
        if SEPARATOR.join(texts) != r["source"]:
            problems.append(f"{r['id']}: source differs from the original file")
    check("source fidelity (segments are the original bytes)", problems,
          f"{sum(len(r['segments']) for r in frs)} segments match the original files byte for byte")

    # -- annotations point into their own fragment; a branch condition points at a control-flow line
    problems, n = [], 0
    for r in frs:
        inside = lambda l: any(s["start_line"] <= l <= s["end_line"] for s in r["segments"])  # noqa: E731
        lines = [("hazard " + h["rule"], l) for h in r["hazards"] for l in h.get("lines", [])]
        lines += [("diagnostic " + str(d["rule"]), d["line"]) for d in r["diagnostics"]]
        lines += [("branch " + c["condition"][:30], c["line"]) for c in r["branch_conditions"]]
        for what, l in lines:
            n += 1
            if not inside(l):
                problems.append(f"{r['id']}: {what} at line {l} is outside the fragment")
        file_lines = raw[r["file"]].splitlines()
        for c in r["branch_conditions"]:
            # the condition may continue a multi-line `if (` ...: accept the keyword within the 3 lines above
            near = b" ".join(file_lines[max(c["line"] - 4, 0):c["line"]])
            if not CONTROL.search(near):
                problems.append(f"{r['id']}: branch `{c['condition'][:40]}` at line {c['line']} is not a control-flow line")
    check("annotations point inside their fragment; branch conditions at control-flow lines", problems,
          f"{n} hazard/diagnostic/branch lines")

    # -- partition of the code: no line in two fragments, every code line in one
    owner, problems = {}, []
    for r in frs:
        for s in r["segments"]:
            for l in range(s["start_line"], s["end_line"] + 1):
                if (r["file"], l) in owner:
                    problems.append(f"{r['file']}:{l} in {owner[(r['file'], l)]} and {r['id']}")
                owner[(r["file"], l)] = r["id"]
    check("fragments are disjoint", problems, "no line belongs to two fragments")
    total, uncovered = 0, []
    lang = (man.get("migration") or {}).get("source_language")
    for x in man["files"]:
        for i in sorted(code_lines(raw[x["file"]], lang)):
            total += 1
            if (x["file"], i) not in owner:
                uncovered.append(f"{x['file']}:{i}")
    check("every code line is in a fragment", uncovered,
          f"{total} code lines in {len(man['files'])} files, 100% covered")

    # -- graph consistency
    problems = []
    edges = [(e["source"], e["target"]) for e in graph["edges"]]
    for s, t in edges:
        if s not in ids or t not in ids:
            problems.append(f"edge {s} -> {t}: unknown fragment")
    out = {i: sorted(t for s, t in edges if s == i) for i in ids}
    inc = {i: sorted(s for s, t in edges if t == i) for i in ids}
    for i, r in ids.items():
        if r["depends_on"] != out[i] or r["dependents"] != inc[i]:
            problems.append(f"{i}: depends_on/dependents disagree with the graph")
    if sorted(n["id"] for n in graph["nodes"]) != sorted(ids):
        problems.append("graph nodes differ from fragments")
    check("dependency graph consistent with fragments", problems, f"{len(edges)} edges")

    # -- order: a partition into steps that respects every dependency
    problems, step_of, steps = [], {}, order["steps"]
    if [s["step"] for s in steps] != list(range(1, len(steps) + 1)):
        problems.append("steps are not numbered 1..n")
    for s in steps:
        for f in s["fragments"]:
            if f in step_of:
                problems.append(f"{f} in steps {step_of[f]} and {s['step']}")
            step_of[f] = s["step"]
    if set(step_of) != set(ids):
        problems.append(f"{len(set(ids) - set(step_of))} fragments in no step")
    by_step = {s["step"]: s for s in steps}
    for i, r in ids.items():
        st = by_step.get(step_of.get(i), {})
        if r["order"] != {"step": st.get("step"), "wave": st.get("wave"), "cycle": st.get("cycle")}:
            problems.append(f"{i}: order field disagrees with migration_order.json")
    check("migration order partitions the fragments", problems, f"{len(steps)} steps")
    if problems:
        return results

    problems = []
    for s, t in edges:
        if step_of[t] > step_of[s]:
            problems.append(f"{s} (step {step_of[s]}) before its dependency {t} (step {step_of[t]})")
        elif step_of[t] == step_of[s] and not by_step[step_of[s]]["cycle"]:
            problems.append(f"{s} and its dependency {t} share non-cycle step {step_of[s]}")
        elif step_of[t] < step_of[s] and by_step[step_of[t]]["wave"] >= by_step[step_of[s]]["wave"]:
            problems.append(f"{s}: wave not above its dependency {t}")
    check("every dependency is migrated before its dependents", problems,
          f"all {len(edges)} edges point to earlier steps (or within a cycle step)")

    # -- cycles are genuine strongly connected components; waves are longest-path levels
    problems = []
    for st in steps:
        members = set(st["fragments"])
        if st["cycle"]:
            for m in members:
                seen, todo = {m}, [m]
                while todo:
                    for t in out[todo.pop()]:
                        if t in members and t not in seen:
                            seen.add(t)
                            todo.append(t)
                if seen != members:
                    problems.append(f"step {st['step']}: {m} does not reach every member")
                    break
        deps = {step_of[t] for m in members for t in out[m]} - {st["step"]}
        want = 1 + max((by_step[d]["wave"] for d in deps), default=-1)
        if st["wave"] != want:
            problems.append(f"step {st['step']}: wave {st['wave']}, expected {want}")
    cyc_graph = sorted(sorted(c) for c in graph["cycles"])
    if cyc_graph != sorted(sorted(s["fragments"]) for s in steps if s["cycle"]):
        problems.append("graph cycles differ from cycle steps")
    check("cycle steps are strongly connected; waves are minimal", problems,
          f"{sum(s['cycle'] for s in steps)} cycle steps, {order['waves']} waves")
    return results
