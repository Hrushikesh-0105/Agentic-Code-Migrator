# Runs INSIDE acm-sandbox/analysis (Python 3.12). Builds the Layer 0 -> Layer 1 bundle for each repo in a job:
#   python /layer0/build.py /out/job.json   -> /out/<name>/{manifest.json, fragments.jsonl, dependency_graph.json,
#                                                         migration_order.json, raw/}
#   python /layer0/build.py --verify        -> fixture with known answers -> /out/verify/
# Nothing under the analysed repo is imported or executed.
import datetime
import json
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, "/analysis")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_tools  # noqa: E402  the Layer 0 toolchain (analysis/tools/run_tools.py)
from decompose import decompose, module_fragments, scope  # noqa: E402
from graph import Resolver, build_edges, migration_order  # noqa: E402
from target import java_target, link  # noqa: E402

FORMAT = {"name": "acm-layer0-bundle", "version": "1.0"}
OVERSIZED_LOC = 150  # a fragment this large probably needs splitting before it fits one LLM call


def dump(path, obj):
    Path(path).write_text(json.dumps(obj, indent=1, default=sorted) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- spans
def _blank(b):
    return not b.strip()


def _comment(b):
    return b.strip().startswith(b"#")


def segments(lines, raw_lines, offsets, owned):
    """Line set -> contiguous segments in the ORIGINAL file. Blank lines are dropped; a gap of blank/comment
    lines no other fragment owns is bridged; a segment holding only comments is dropped."""
    keep = sorted(l for l in lines if not _blank(raw_lines[l - 1]))
    ranges = []
    for l in keep:
        if ranges and all((_blank(raw_lines[g - 1]) or _comment(raw_lines[g - 1])) and g not in owned
                          for g in range(ranges[-1][1] + 1, l)):
            ranges[-1][1] = l
        else:
            ranges.append([l, l])
    out = []
    for a, b in ranges:
        if all(_comment(raw_lines[l - 1]) or _blank(raw_lines[l - 1]) for l in range(a, b + 1)):
            continue
        out.append({"start_line": a, "end_line": b, "start_byte": offsets[a - 1], "end_byte": offsets[b]})
    return out


def _offsets(raw):
    offs, pos = [0], 0
    for line in raw.splitlines(keepends=True):
        pos += len(line)
        offs.append(pos)
    return offs


# ---------------------------------------------------------------- one repo
def build_bundle(name, code, java, out, source_root=".", target_language=None):
    out, raw_dir = Path(out), Path(out) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    run_tools.OUT = raw_dir
    started = time.time()

    # Step 1a: parse and analyse with the pinned toolchain (LibCST, tree-sitter, pyan3, JARVIS, pyright, ...)
    ctx = run_tools.Ctx(code, java)
    tools = run_tools.run_all(ctx)

    def load(tool):
        p = raw_dir / f"{tool}.json"
        return json.loads(p.read_text(encoding="utf-8")) if tools["tools"][tool]["ok"] and p.exists() else None

    # Step 2: decompose into fragments
    files, excluded = scope(ctx)
    defs, modules, warnings = decompose(ctx, files)
    resolver = Resolver(defs, modules)
    resolver.add_module_fragments(module_fragments(defs, modules, resolver.resolve_def))
    frags = resolver.frags

    # Step 1b: dependency and call graph over fragments (LibCST references + pyan3 + JARVIS + structure)
    raw_text = {f: (ctx.root / f).read_bytes() for f in files}
    raw_lines = {f: raw_text[f].splitlines() for f in files}

    def code_of(fid):
        fr = frags[fid]
        return b"\n".join(raw_lines[fr.file][l - 1] for l in sorted(fr.lines)).decode("utf-8", "replace")
    edges, external, recursive, dropped = build_edges(
        resolver, (load("pyan3") or {}).get("uses", []), load("jarviscg") or {}, code_of)
    target = java_target(java) if java else None
    target_language = target_language or ("java" if target else "python3")
    if target_language == "java":  # a Java class compiles only with its constructors: class + own __init__ = 1 step
        for fid, fr in frags.items():
            if fr.kind == "class" and f"{fid}.__init__" in frags:
                e = edges.setdefault((fid, f"{fid}.__init__"), {"kinds": set(), "via": set()})
                e["kinds"].add("init")
                e["via"].add("structure")

    # Step 3: bottom-up migration order
    file_rank = {f: i for i, f in enumerate(files)}
    key = lambda fid: (file_rank[frags[fid].file], min(frags[fid].lines), fid)  # noqa: E731
    steps = migration_order(list(frags), list(edges), key)
    where = {fid: s for s in steps for fid in s["fragments"]}

    # ---- spans and source text, from the ORIGINAL files
    offsets = {f: _offsets(raw_text[f]) for f in files}
    owned_by_file = defaultdict(set)
    for fr in frags.values():
        owned_by_file[fr.file] |= fr.lines
    segs = {fid: segments(fr.lines, raw_lines[fr.file], offsets[fr.file], owned_by_file[fr.file] - fr.lines)
            for fid, fr in frags.items()}
    line_owner = {}
    for fid, ss in segs.items():
        for s in ss:
            for l in range(s["start_line"], s["end_line"] + 1):
                line_owner[(frags[fid].file, l)] = fid

    def owner_of(file, line):
        return line_owner.get((file, line)) or (f"{module_of[file]}::header"
                                                if f"{module_of[file]}::header" in frags else None)
    module_of = {m["file"]: mod for mod, m in modules.items()}

    # ---- annotations from the toolchain, attached by line or by qualified name
    hazards, diagnostics = defaultdict(list), defaultdict(list)
    for tool in ("fissix", "modernize"):
        for f, fixers in ((load(tool) or {}).get("hits") or {}).items():
            for fixer, lines in fixers.items():
                by_frag = defaultdict(list)
                for l in lines:
                    if f in file_rank and owner_of(f, l):
                        by_frag[owner_of(f, l)].append(l)
                for fid, ls in by_frag.items():
                    hazards[fid].append({"tool": tool, "rule": fixer, "lines": sorted(ls)})
    stdlib = load("stdlib_list") or {}
    classification = stdlib.get("classification", {})
    for f, mods in (stdlib.get("imports") or {}).items():
        py2_only = [m for m in mods if classification.get(m) == "stdlib_py2_only"]
        if py2_only and f in file_rank and f"{module_of[f]}::header" in frags:
            hazards[f"{module_of[f]}::header"].append({"tool": "stdlib_list", "rule": "py2_only_stdlib_import",
                                                        "modules": py2_only})
    pyright = load("pyright") or {}
    for d in pyright.get("diagnostics", []):
        fid = owner_of(d["file"], d["line"]) if d["file"] in file_rank else None
        if fid:
            diagnostics[fid].append({k: d[k] for k in ("line", "severity", "rule", "message")})
    inferred = pyright.get("inferred_returns", {})
    cfgs = (load("staticfg") or {}).get("functions", {})
    lizard = {(x["file"], x["lines"][0]): x for x in (load("lizard") or {}).get("python", [])}

    std27, std3 = _stdlib_sets()

    def classify(fq):
        top = fq.split(".")[0]
        if top == "builtins":
            return "builtin"
        if "." not in fq and fq not in std27 | std3:
            return "unresolved"  # a bare method name whose receiver no tool could type (e.g. JARVIS `append`)
        if top in std27 and top in std3:
            return "stdlib"
        return "stdlib_py2_only" if top in std27 else "stdlib_py3_only" if top in std3 else "third_party"

    py2_files = set(ctx.py2_files)
    source_language = "python2" if py2_files & set(files) else "python3"

    records = []
    for fid in sorted(frags, key=key):
        fr = frags[fid]
        ss = segs[fid]
        text = [raw_text[fr.file][s["start_byte"]:s["end_byte"]].decode("utf-8", "replace") for s in ss]
        sig = dict(fr.signature) if fr.signature else None
        if sig is not None and fr.kind in ("function", "method"):
            sig["inferred_return"] = inferred.get(fr.qualname)
        metrics = {}
        for dl in fr.def_lines:
            if (fr.file, dl) in lizard:
                x = lizard[(fr.file, dl)]
                metrics = {"nloc": x["nloc"], "ccn": x["ccn"], "tokens": x["tokens"]}
                break
        ext = defaultdict(set)
        for q in external.get(fid, ()):
            ext[classify(q)].add(q)
        rec = {
            "id": fid, "kind": fr.kind, "qualname": fr.qualname, "module": fr.module, "file": fr.file,
            "parent": fr.parent, "children": sorted(c.id for c in frags.values() if c.parent == fid),
            "segments": ss, "loc": sum(not _blank(raw_lines[fr.file][l - 1]) for s in ss
                                       for l in range(s["start_line"], s["end_line"] + 1)),
            "source": "# ...\n".join(text), "signature": sig,
            "depends_on": sorted(d for (s, d) in edges if s == fid),
            "dependents": sorted(s for (s, d) in edges if d == fid),
            "external_apis": {k: sorted(v) for k, v in sorted(ext.items())},
            "features": fr.features, "metrics": metrics,
            "branch_conditions": cfgs.get(fr.qualname, {}).get("branch_conditions", [])
            if fr.kind in ("function", "method") else [],
            "hazards": hazards.get(fid, []), "diagnostics": diagnostics.get(fid, []),
            "order": {"step": where[fid]["step"], "wave": where[fid]["wave"], "cycle": where[fid]["cycle"]},
        }
        if fr.kind in ("module_header", "module_init"):
            internal_imports = sorted({resolver.frags[r].module if r else q for q in fr.imports
                                       for r, i in [resolver.resolve(q)] if i})
            ext_imports = defaultdict(set)
            for q in fr.imports:
                if not resolver.resolve(q)[1]:
                    ext_imports[classify(q)].add(q.split(".")[0])
            rec["imports"] = {"internal_modules": internal_imports,
                              "external": {k: sorted(v) for k, v in sorted(ext_imports.items())}}
            rec["defines"] = sorted(fr.defines)
        flags = []
        if fid in recursive:
            flags.append("recursive")
        if where[fid]["cycle"]:
            flags.append("in_cycle")
        if rec["loc"] > OVERSIZED_LOC:
            flags.append("oversized")
        if modules[fr.module]["vendored"]:
            flags.append("vendored")
        if fr.file in py2_files:
            flags.append("python2_source")
        if fr.main_guard:
            flags.append("entrypoint")
        rec["flags"] = flags
        records.append(rec)

    hints = link(records, target) if target else {}
    for rec in records:
        rec["target_hints"] = hints.get(rec["id"], [])
    if target:
        py_names = {r["qualname"].rsplit(".", 1)[-1].lower().replace("_", "") for r in records}
        stubbed = sorted(c for c in target["stubbed_in_tests"] if c.lower().replace("_", "") in py_names)
        if stubbed:
            warnings.append(f"the Java tests declare their own stand-ins for {len(stubbed)} project class(es) "
                            f"({', '.join(stubbed)}): those tests exercise the stubs, not the translation")

    # ---- write the bundle
    (out / "fragments.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    via = Counter(v for e in edges.values() for v in e["via"])
    ext_count = defaultdict(Counter)
    for r in records:
        for cat, names in r["external_apis"].items():
            ext_count[cat].update(names)
    module_imports = {}
    for mod, m in sorted(modules.items()):
        h = next((r for r in records if r["id"] == f"{mod}::header"), None)
        module_imports[mod] = {"file": m["file"], "vendored": m["vendored"], "python2_source": m["file"] in py2_files,
                               "imports_internal": h["imports"]["internal_modules"] if h else [],
                               "imports_external": h["imports"]["external"] if h else {}}
    dump(out / "dependency_graph.json", {
        "format": FORMAT,
        "edge_semantics": "source depends on target: target is migrated first",
        "nodes": [{"id": r["id"], "kind": r["kind"], "module": r["module"], "file": r["file"]} for r in records],
        "edges": [{"source": s, "target": d, "kinds": sorted(e["kinds"]), "via": sorted(e["via"])}
                  for (s, d), e in sorted(edges.items())],
        "modules": module_imports,
        "cycles": [s["fragments"] for s in steps if s["cycle"]],
        "recursive": sorted(recursive),
        # pyan3/JARVIS edges with no support from LibCST or the code: [source, target, tool's name, tool]
        "dropped_unconfirmed": sorted({tuple(d) for d in dropped}),
        "external_apis": {cat: dict(c.most_common()) for cat, c in sorted(ext_count.items())},
        "edge_sources": dict(sorted(via.items())),
    })
    waves = Counter(s["wave"] for s in steps)
    loc = {r["id"]: r["loc"] for r in records}
    dump(out / "migration_order.json", {
        "format": FORMAT,
        "strategy": "bottom-up: strongly connected components of the dependency graph in reverse topological "
                    "order; a step is migrated only after every step it depends on; steps in one wave are "
                    "independent",
        "steps": [{**s, "loc": sum(loc[f] for f in s["fragments"]),
                   "kinds": sorted({frags[f].kind for f in s["fragments"]})} for s in steps],
        "waves": len(waves), "steps_per_wave": [waves[w] for w in sorted(waves)],
    })
    kinds = Counter(r["kind"] for r in records)
    manifest = {
        "format": FORMAT,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "repo": {"name": name, "source_root": source_root, "packages": ctx.packages, "modules": ctx.modules},
        "migration": {"source_language": source_language, "target_language": target_language,
                      "regime": f"{source_language}->{target_language}"},
        "files": [{"file": f, "module": module_of[f], "lines": len(raw_lines[f]),
                   "python2_source": f in py2_files, "vendored": modules[module_of[f]]["vendored"],
                   "maintainability_index": ((load("radon") or {}).get(f) or {}).get("mi")} for f in files],
        "excluded_files": excluded,
        "target": target,
        "toolchain": {"image": "acm-sandbox/analysis",
                      "tools": {t: {"ok": r["ok"], "version": r["version"], "seconds": r["seconds"],
                                    **({"error": r["error"]} if not r["ok"] else {})}
                                for t, r in tools["tools"].items()}},
        "stats": {"fragments": len(records), "by_kind": dict(sorted(kinds.items())), "edges": len(edges),
                  "steps": len(steps), "waves": len(waves), "cycles": sum(s["cycle"] for s in steps),
                  "largest_cycle": max((len(s["fragments"]) for s in steps), default=0),
                  "loc": sum(loc.values()),
                  "fragments_with_hazards": sum(bool(r["hazards"]) for r in records),
                  "fragments_with_target_hints": sum(bool(r["target_hints"]) for r in records),
                  "unconfirmed_edges_dropped": len({tuple(d[:2]) for d in dropped}),
                  "flags": dict(Counter(f for r in records for f in r["flags"])),
                  "seconds": round(time.time() - started, 1)},
        "outputs": {"fragments": "fragments.jsonl", "dependency_graph": "dependency_graph.json",
                    "migration_order": "migration_order.json", "raw_tool_outputs": "raw/"},
        "warnings": warnings + [f"tool {t} failed: {r['error']}" for t, r in tools["tools"].items() if not r["ok"]],
    }
    dump(out / "manifest.json", manifest)
    return manifest


def _stdlib_sets():
    from stdlib_list import stdlib_list
    return set(stdlib_list("2.7")), set(stdlib_list("3.12"))


# ---------------------------------------------------------------- --verify: fixture with known answers
FIXTURE = {
    "pkg/__init__.py": '"""Demo package."""\nfrom pkg.shapes import Square\n\n__all__ = ["Square"]\n',
    # Python 2 source: must survive verbatim in the fragments
    "pkg/util.py": (
        "import StringIO\n\nSCALE = 2\n\n\n"
        "def double(x):\n    return x * SCALE\n\n\n"
        "def is_even(n):\n    if n == 0:\n        return True\n    return is_odd(n - 1)\n\n\n"
        "def is_odd(n):\n    if n == 0:\n        return False\n    return is_even(n - 1)\n\n\n"
        "def fact(n):\n    return 1 if n <= 1 else n * fact(n - 1)\n\n\n"
        "def report(d):\n    if not d:\n        raise ValueError, \"empty\"\n"  # Python-2-only syntax
        "    out = StringIO.StringIO()\n    for k, v in d.iteritems():\n"
        "        print >>out, \"%s=%d\" % (k, v / 2)\n    return out.getvalue()\n"),
    "pkg/shapes.py": (
        "from pkg import util\n\n\n"
        "class Shape(object):\n    sides = 0\n\n    def __init__(self, size):\n        self.size = size\n\n"
        "    def area(self):\n        raise NotImplementedError\n\n\n"
        "class Square(Shape):\n    sides = 4\n\n    # geometry\n    def area(self):\n"
        "        return util.double(self.size) * self.size / 2\n\n"
        "    def describe(self):\n        return \"square %d\" % self.area()\n\n\n"
        "class Cube(Square):\n    def __init__(self, size):\n        super(Cube, self).__init__(size)\n\n\n"
        "def make(size):\n    return Square(size)\n\n\n"
        "def lookup(name):\n    return REGISTRY[name](1)\n\n\n"
        "REGISTRY = {\"square\": make}\n\n"
        "if __name__ == \"__main__\":\n    print(make(3).describe())\n"),
    "tests/test_shapes.py": "from pkg.shapes import make\n\n\ndef test_make():\n    assert make(2).size == 2\n",
    "scripts/bench.py": "from pkg.util import double\nprint(double(2))\n",
}
JAVA_FIXTURE = {
    "src/test/java/com/demo/original/ShapesTest.java": (
        "package com.demo.original;\n\nimport com.demo.Square;\nimport com.demo.Util;\n"
        "import org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "class ShapesTest {\n    @Test\n    void area() {\n        Square sq = new Square(3);\n"
        "        assertEquals(9, sq.area());\n        assertEquals(true, Util.isEven(4));\n    }\n}\n"),
}
EXPECTED_IDS = {
    "pkg::header", "pkg.util::header", "pkg.util.double", "pkg.util.is_even", "pkg.util.is_odd", "pkg.util.report",
    "pkg.shapes::header", "pkg.shapes.Shape", "pkg.shapes.Shape.__init__", "pkg.shapes.Shape.area",
    "pkg.shapes.Square", "pkg.shapes.Square.area", "pkg.shapes.Square.describe", "pkg.shapes.make",
    "pkg.shapes.lookup", "pkg.shapes::init", "pkg.util.fact", "pkg.shapes.Cube", "pkg.shapes.Cube.__init__"}


def _checks(b):
    fr, step = b["fragments"], {r["id"]: r["order"]["step"] for r in b["fragments"].values()}
    edge = {(e["source"], e["target"]): e for e in b["graph"]["edges"]}

    def has(s, d, kind):
        return (s, d) in edge and kind in edge[(s, d)]["kinds"]
    return [
        ("decompose: exactly the expected fragments (tests/ and scripts/ excluded, one header/init per module)",
         set(fr) == EXPECTED_IDS),
        ("decompose: class shell keeps attributes but not methods",
         "sides = 4" in fr["pkg.shapes.Square"]["source"] and "def area" not in fr["pkg.shapes.Square"]["source"]),
        ("decompose: Python 2 source preserved verbatim (raise E, msg / print >> / iteritems)",
         all(s in fr["pkg.util.report"]["source"] for s in ('raise ValueError, "empty"', "print >>out", "iteritems"))
         and "python2_source" in fr["pkg.util.report"]["flags"]),
        ("decompose: REGISTRY and the __main__ guard are module init, constants stay in the header",
         "REGISTRY" in fr["pkg.shapes::init"]["defines"] and "entrypoint" in fr["pkg.shapes::init"]["flags"]
         and "SCALE" in fr["pkg.util::header"]["defines"]),
        ("graph: cross-module call Square.area -> util.double", has("pkg.shapes.Square.area", "pkg.util.double", "call")),
        ("graph: inheritance Square -> Shape", has("pkg.shapes.Square", "pkg.shapes.Shape", "inherit")),
        ("graph: self.area() resolved (Square.describe -> Square.area)",
         ("pkg.shapes.Square.describe", "pkg.shapes.Square.area") in edge),
        ("graph: constructor found through inheritance (make -> Shape.__init__)",
         has("pkg.shapes.make", "pkg.shapes.Shape.__init__", "constructor")),
        ("graph: module constant (double -> util header via SCALE)", has("pkg.util.double", "pkg.util::header", "reference")),
        ("graph: name bound in module init (lookup -> shapes init via REGISTRY)",
         has("pkg.shapes.lookup", "pkg.shapes::init", "reference")),
        ("graph: recursion flagged only for exact self-calls (fact yes; super().__init__, is_even no)",
         "recursive" in fr["pkg.util.fact"]["flags"] and "recursive" not in fr["pkg.shapes.Cube.__init__"]["flags"]
         and "recursive" not in fr["pkg.util.is_even"]["flags"]),
        ("order: mutual recursion is_even/is_odd is one cycle step",
         step["pkg.util.is_even"] == step["pkg.util.is_odd"] and fr["pkg.util.is_even"]["order"]["cycle"]),
        ("order: callee before caller, base before subclass, class before its methods",
         step["pkg.util.double"] < step["pkg.shapes.Square.area"]
         and step["pkg.shapes.Shape"] < step["pkg.shapes.Square"] < step["pkg.shapes.Square.area"]),
        ("order: make before module init before lookup",
         step["pkg.shapes.make"] < step["pkg.shapes::init"] < step["pkg.shapes.lookup"]),
        ("metadata: 2to3/modernize hazards and py2-only import attached to the right fragments",
         {"fix_dict", "fix_print"} <= {h["rule"] for h in fr["pkg.util.report"]["hazards"]}
         and any(h["rule"] == "py2_only_stdlib_import" for h in fr["pkg.util::header"]["hazards"])),
        ("metadata: Java API expected by the tests, linked to fragments",
         {"java": "Square#area", "match": "name"} in fr["pkg.shapes.Square.area"]["target_hints"]
         and {"java": "Util#isEven", "match": "name"} in fr["pkg.util.is_even"]["target_hints"]
         and b["manifest"]["target"]["expected_api"]["Square"]["constructed"] == 1),
        ("metadata: source language detected as python2", b["manifest"]["migration"]["source_language"] == "python2"),
    ]


def load_bundle(d):
    d = Path(d)
    return {"manifest": json.loads((d / "manifest.json").read_text()),
            "graph": json.loads((d / "dependency_graph.json").read_text()),
            "order": json.loads((d / "migration_order.json").read_text()),
            "fragments": {r["id"]: r for r in map(json.loads, (d / "fragments.jsonl").read_text().splitlines())}}


def verify(out):
    src, java = out / "fixture" / "src", out / "fixture" / "java"
    for root, files in ((src, FIXTURE), (java, JAVA_FIXTURE)):
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
    build_bundle("fixture", src, java, out / "bundle")
    results = []
    try:
        checks = _checks(load_bundle(out / "bundle"))
    except Exception:
        checks = [("checks ran", False)]
        traceback.print_exc()
    for claim, ok in checks:
        results.append({"check": claim, "ok": bool(ok)})
        print(f"[{'PASS' if ok else 'FAIL'}] {claim}", flush=True)
    dump(out / "verify.json", results)
    return all(r["ok"] for r in results)


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(0 if verify(Path("/out/verify")) else 1)
    job = json.loads(Path(sys.argv[1]).read_text())
    failed = False
    for r in job["repos"]:
        print(f"== {r['name']}", flush=True)
        try:
            if r.get("source_language") not in (None, "python2", "python3"):
                from generic import build_generic  # noqa: E402  tree-sitter pipeline for the other languages
                target = java_target(r["java"]) if r.get("java") and r.get("target_language") == "java" else None
                m = build_generic(r["name"], r["code"], r.get("java"), r["out"], r.get("source_root", "."),
                                  r["source_language"], r.get("target_language") or "python3", target)
            else:
                m = build_bundle(r["name"], r["code"], r.get("java"), r["out"], r.get("source_root", "."),
                                 r.get("target_language"))
            print(f"== {r['name']}: {json.dumps(m['stats'])}", flush=True)
        except Exception as e:
            failed = True
            Path(r["out"]).mkdir(parents=True, exist_ok=True)
            dump(Path(r["out"]) / "error.json", {"error": f"{type(e).__name__}: {e}",
                                                 "traceback": traceback.format_exc()})
            print(f"== {r['name']}: FAILED {type(e).__name__}: {e}", flush=True)
    sys.exit(1 if failed else 0)
