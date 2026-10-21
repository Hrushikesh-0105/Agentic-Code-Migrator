# Runs INSIDE acm-sandbox/analysis (Python 3.12). Static analysis only: nothing under /code is imported or run.
#   python /analysis/run_tools.py             analyse /code (and a Java target at /java if mounted) -> /out
#   python /analysis/run_tools.py --verify    run every tool on a built-in fixture and check its output -> /out
# Each tool writes its raw output to /out/<tool>.json; /out/tools.json records status, version, time and stats.
import ast
import difflib
import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

OUT = Path(os.environ.get("ACM_OUT", "/out"))
SKIP_DIRS = {"tests", "test", "testing", "public_tests", "docs", "doc", "examples", "example", "htmlcov", "build",
             "dist", "venv", "env", "site-packages", "__pycache__", "node_modules", "target"}
SKIP_FILES = {"setup.py", "conftest.py"}
# tool -> distribution whose version is recorded
DISTS = {"libcst": "libcst", "tree_sitter": "tree-sitter", "fissix": "fissix", "modernize": "modernize",
         "pyan3": "pyan3", "jarviscg": "jarviscg", "grimp": "grimp", "networkx": "networkx", "pyright": "pyright",
         "staticfg": "staticfg", "lizard": "lizard", "radon": "radon", "stdlib_list": "stdlib-list"}


def dump(name, obj):
    (OUT / f"{name}.json").write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")


def read(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return text if text.endswith("\n") else text + "\n"


class Ctx:
    """What every tool needs: the repo, its source files, and a Python 3 view of it for Python-3-only tools."""

    def __init__(self, root, java=None):
        self.root, self.java = Path(root), Path(java) if java and Path(java).is_dir() else None
        self.files = []
        for p in sorted(self.root.rglob("*.py")):
            rel = p.relative_to(self.root)
            if not any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]) and rel.name not in SKIP_FILES:
                self.files.append(rel.as_posix())
        self.packages = sorted({f.split("/")[0] for f in self.files
                                if "/" in f and (self.root / f.split("/")[0] / "__init__.py").exists()})
        self.modules = sorted(f[:-3] for f in self.files if "/" not in f)  # top-level single-file modules
        self.py2_files = [f for f in self.files if not self._parses(self.root / f)]
        self.view, self.conversion_log = self._py3_view()
        # Python-3-only tools read these; a file 2to3 could not make valid Python 3 is left out, not fatal
        self.py3_files = [f for f in self.files if f not in self.py2_files or self._parses(self.view / f)]
        self.results = {}  # tool -> raw output, for tools that build on earlier ones

    @staticmethod
    def _parses(path):
        try:
            ast.parse(path.read_bytes())
            return True
        except (SyntaxError, ValueError):
            return False

    def _py3_view(self):
        """Python-3-only tools (LibCST, pyan3, JARVIS, pyright, staticfg, radon) read this copy, in which files
        that are not valid Python 3 were converted by 2to3 (fissix). 2to3 edits inside lines, so line numbers
        keep matching the original."""
        view = Path("/tmp/py3view")
        shutil.rmtree(view, ignore_errors=True)
        shutil.copytree(self.root, view, ignore=shutil.ignore_patterns(".git", "htmlcov", "*.pyc", "__pycache__"))
        for f in self.files:  # pyan3 and radon reject a UTF-8 byte-order mark; it changes no line numbers
            data = (view / f).read_bytes()
            if data.startswith(b"\xef\xbb\xbf"):
                (view / f).write_bytes(data[3:])
        log = ""
        if self.py2_files:
            p = subprocess.run([sys.executable, "-m", "fissix", "-w", "-n", *self.py2_files], cwd=view,
                               capture_output=True, text=True)
            log = p.stdout[-2000:] + p.stderr[-2000:]
        return view, log

    def internal(self, name):
        head = name.split(".")[0]
        return head in self.packages or head in self.modules

    def describe(self):
        return {"files": self.files, "packages": self.packages, "modules": self.modules,
                "py2_files": self.py2_files, "not_convertible": sorted(set(self.files) - set(self.py3_files)),
                "conversion_log": self.conversion_log, "java_target": bool(self.java)}


# ---------------------------------------------------------------- symbols & parsing
def _qual(name):
    """LibCST writes nested defs as f.<locals>.g; pyan3, JARVIS and staticfg write f.g. Use the latter everywhere."""
    return name.replace(".<locals>", "")


def t_libcst(ctx):
    """Symbol table: every class/function/method with fully qualified name, span, parameters, the calls it makes
    (resolved to qualified names) and its local/free variables (def-use at scope level)."""
    import libcst as cst
    from libcst.metadata import FullRepoManager, FullyQualifiedNameProvider, PositionProvider, ScopeProvider

    paths = [str(ctx.view / f) for f in ctx.py3_files]
    mgr = FullRepoManager(str(ctx.view), paths, {FullyQualifiedNameProvider})
    symbols, errors = [], {}

    for f in ctx.py3_files:
        try:
            w = mgr.get_metadata_wrapper_for_path(str(ctx.view / f))
            pos, fq, scopes = w.resolve(PositionProvider), w.resolve(FullyQualifiedNameProvider), w.resolve(ScopeProvider)
        except Exception as e:
            errors[f] = f"{type(e).__name__}: {e}"[:300]
            continue
        module = w.module

        class V(cst.CSTVisitor):
            def __init__(self):
                self.stack = []

            def _enter(self, node, kind):
                sym = {"qualname": sorted(_qual(q.name) for q in fq.get(node, ()))[0] if fq.get(node) else node.name.value,
                       "kind": kind, "file": f, "lines": [pos[node].start.line, pos[node].end.line],
                       "decorators": [module.code_for_node(d.decorator) for d in node.decorators]}
                if isinstance(node, cst.FunctionDef):
                    ps = node.params
                    sym["params"] = [{"name": p.name.value,
                                      "default": module.code_for_node(p.default) if p.default else None,
                                      "annotation": module.code_for_node(p.annotation.annotation) if p.annotation else None}
                                     for p in [*ps.posonly_params, *ps.params, *ps.kwonly_params]]
                    sym["varargs"] = [p.name.value for p in (ps.star_arg, ps.star_kwarg)
                                      if isinstance(p, cst.Param)]
                    sym["returns"] = module.code_for_node(node.returns.annotation) if node.returns else None
                    sym["is_generator"] = False
                    sym["calls"] = set()
                    body = node.body.body[0] if node.body.body else None
                    scope = scopes.get(body) if body is not None else None
                    if scope is not None:
                        local = {a.name for a in scope.assignments}
                        reads = {a.node.value for a in scope.accesses if isinstance(a.node, cst.Name)}
                        sym["locals"], sym["free_vars"] = sorted(local), sorted(reads - local)
                else:
                    sym["bases"] = [module.code_for_node(b.value) for b in node.bases]
                symbols.append(sym)
                self.stack.append(sym)

            def visit_ClassDef(self, node):
                self._enter(node, "class")

            def leave_ClassDef(self, node):
                self.stack.pop()

            def visit_FunctionDef(self, node):
                inside_class = bool(self.stack) and self.stack[-1]["kind"] == "class"
                self._enter(node, "method" if inside_class else "function")

            def leave_FunctionDef(self, node):
                self.stack.pop()

            def _owner(self):
                return next((s for s in reversed(self.stack) if s["kind"] != "class"), None)

            def visit_Call(self, node):
                owner = self._owner()
                if owner is not None:
                    owner["calls"].update(_qual(q.name) for q in fq.get(node.func, ()))

            def visit_Yield(self, node):
                owner = self._owner()
                if owner is not None:
                    owner["is_generator"] = True

        w.visit(V())
    for s in symbols:
        if "calls" in s:
            s["calls"] = sorted(s["calls"])
    ctx.results["libcst"] = symbols
    dump("libcst", {"symbols": symbols, "errors": errors})
    kinds = [s["kind"] for s in symbols]
    internal_calls = sum(1 for s in symbols for c in s.get("calls", ()) if ctx.internal(c))
    return {"classes": kinds.count("class"), "functions": kinds.count("function"), "methods": kinds.count("method"),
            "internal_calls": internal_calls, "files_failed": len(errors)}


def t_tree_sitter(ctx):
    """Error-tolerant parse of the ORIGINAL sources (Python 2 included) and of the Java target: definition spans
    in lines and bytes, used to cut the repo into fragments and to map translated Java fragments back."""
    import tree_sitter_java
    import tree_sitter_python
    from tree_sitter import Language, Parser

    def defs(parser, data, kinds, name_of):
        tree, out = parser.parse(data), []

        def walk(node, prefix):
            name = prefix
            if node.type in kinds:
                n = name_of(node)
                name = f"{prefix}.{n}" if prefix else n
                out.append({"name": name, "type": node.type, "lines": [node.start_point[0] + 1, node.end_point[0] + 1],
                            "bytes": [node.start_byte, node.end_byte]})
            for c in node.children:
                walk(c, name)
        walk(tree.root_node, "")
        return tree.root_node.has_error, out

    py = Parser(Language(tree_sitter_python.language()))
    name = lambda n: n.child_by_field_name("name").text.decode()
    python = {}
    for f in ctx.files:
        err, found = defs(py, (ctx.root / f).read_bytes(), {"function_definition", "class_definition"}, name)
        python[f] = {"has_error": err, "definitions": found}
    java = {}
    if ctx.java:
        jv = Parser(Language(tree_sitter_java.language()))
        kinds = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
                 "method_declaration", "constructor_declaration"}
        for p in sorted(ctx.java.rglob("*.java")):
            err, found = defs(jv, p.read_bytes(), kinds, name)
            java[p.relative_to(ctx.java).as_posix()] = {"has_error": err, "definitions": found}
    dump("tree_sitter", {"python": python, "java": java})
    return {"python_files": len(python), "python_definitions": sum(len(v["definitions"]) for v in python.values()),
            "python_files_with_errors": sum(v["has_error"] for v in python.values()),
            "java_files": len(java), "java_definitions": sum(len(v["definitions"]) for v in java.values())}


# ---------------------------------------------------------------- Python 2 -> 3 hazards
def _changed_lines(old, new):
    lines = set()
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, old.splitlines(), new.splitlines()).get_opcodes():
        if tag != "equal":
            lines.update(range(i1 + 1, max(i2, i1 + 1) + 1))
    return sorted(lines)


def _fixers_fired(fixers, files, root):
    """Run each fixer alone on each file; record the original lines it would change. Output: {file: {fixer: lines}}."""
    from fissix import refactor
    out, errors = {}, {}
    for f in files:
        text, hits = read(root / f), {}
        for fx in fixers:
            new = None
            for opts in ({}, {"print_function": True}):  # retry for py3-style print(..., end=...)
                try:
                    new = str(refactor.RefactoringTool([fx], opts).refactor_string(text, f))
                    break
                except Exception as e:
                    errors[f] = f"{type(e).__name__}: {e}"[:300]
            if new is None:
                break
            errors.pop(f, None)
            if new != text:
                hits[fx.rsplit(".", 1)[-1]] = _changed_lines(text, new)
        out[f] = hits
    return out, errors


def t_fissix(ctx):
    """2to3 (lib2to3 fork): which 2to3 fixers would change which lines. Each hit is a Py2->3 hazard site, and
    applying all of them is the rule-based baseline."""
    from fissix import refactor
    fixers = refactor.get_fixers_from_package("fissix.fixes")
    hits, errors = _fixers_fired(fixers, ctx.files, ctx.root)
    dump("fissix", {"fixers": [x.rsplit(".", 1)[-1] for x in fixers], "hits": hits, "errors": errors})
    fired = sorted({fx for h in hits.values() for fx in h})
    return {"files_with_hits": sum(bool(h) for h in hits.values()), "fixers_fired": fired, "parse_errors": len(errors)}


def t_modernize(ctx):
    """python-modernize fixers (on top of fissix), mainly for hazards 2to3 ignores, e.g. classic division."""
    from fissix import refactor
    fixers = refactor.get_fixers_from_package("libmodernize.fixes")
    hits, errors = _fixers_fired(fixers, ctx.files, ctx.root)
    dump("modernize", {"fixers": [x.rsplit(".", 1)[-1] for x in fixers], "hits": hits, "errors": errors})
    return {"files_with_hits": sum(bool(h) for h in hits.values()),
            "fixers_fired": sorted({fx for h in hits.values() for fx in h}), "parse_errors": len(errors)}


# ---------------------------------------------------------------- dependency structure
def t_pyan3(ctx):
    """Call graph #1 (pyan3): 'uses' and 'defines' edges between modules, classes, functions and methods."""
    from pyan.analyzer import CallGraphVisitor
    v = CallGraphVisitor([str(ctx.view / f) for f in ctx.py3_files], root=str(ctx.view))
    nodes = {}
    for group in v.nodes.values():
        for n in group:
            nodes[n.get_name()] = {"flavor": str(n.flavor), "defined": bool(n.defined),
                                   "file": Path(n.filename).relative_to(ctx.view).as_posix() if n.filename else None,
                                   "line": getattr(n.ast_node, "lineno", None)}
    edges = lambda es: sorted({(a.get_name(), b.get_name()) for a, bs in es.items() for b in bs})
    uses, defines = edges(v.uses_edges), edges(v.defines_edges)
    ctx.results["pyan3"] = uses
    dump("pyan3", {"nodes": nodes, "uses": uses, "defines": defines})
    internal = [(a, b) for a, b in uses if ctx.internal(a) and ctx.internal(b)]
    return {"nodes": len(nodes), "uses_edges": len(uses), "internal_uses_edges": len(internal), "defines_edges": len(defines)}


def t_jarviscg(ctx):
    """Call graph #2 (JARVIS): function-level, flow-insensitive; also resolves calls into builtins and stdlib."""
    tmp = Path("/tmp/jarvis.json")
    p = subprocess.run(["jarviscg", *ctx.py3_files, "--package", ".", "-o", str(tmp)], cwd=ctx.view,
                       capture_output=True, text=True, timeout=900)
    if p.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"jarviscg exit {p.returncode}: {p.stderr[-800:]}")
    graph = {k: sorted(set(v)) for k, v in sorted(json.loads(tmp.read_text()).items())}
    ctx.results["jarviscg"] = graph
    dump("jarviscg", graph)
    edges = [(a, b) for a, bs in graph.items() for b in bs]
    return {"nodes": len(graph), "edges": len(edges),
            "internal_edges": sum(ctx.internal(a) and ctx.internal(b) for a, b in edges),
            "external_targets": len({b for _, b in edges if not ctx.internal(b)})}


def t_grimp(ctx):
    """Module import graph, including imports of external packages."""
    import grimp
    if not ctx.packages:
        dump("grimp", {"skipped": "no packages (grimp needs a package; top-level modules only)"})
        return {"skipped": "no packages"}
    sys.path.insert(0, str(ctx.view))
    try:
        g = grimp.build_graph(*ctx.packages, include_external_packages=True, cache_dir=None)
    finally:
        sys.path.remove(str(ctx.view))
    imports = {m: sorted(g.find_modules_directly_imported_by(m)) for m in sorted(g.modules)
               if m.split(".")[0] in ctx.packages}
    dump("grimp", {"imports": imports})
    internal = [(a, b) for a, bs in imports.items() for b in bs if b.split(".")[0] in ctx.packages]
    return {"modules": len(imports), "internal_imports": len(internal),
            "external_imports": sorted({b.split(".")[0] for bs in imports.values() for b in bs
                                        if b.split(".")[0] not in ctx.packages})}


def t_networkx(ctx):
    """Translation order: union of both call graphs' internal edges, strongly connected components (mutual
    recursion must be translated together), then reverse topological order, callees before callers."""
    import networkx as nx
    defined = {s["qualname"] for s in ctx.results.get("libcst", []) if s["kind"] in ("function", "method")}
    g = nx.DiGraph()
    g.add_nodes_from(defined)
    jarvis = [(a, b) for a, bs in ctx.results.get("jarviscg", {}).items() for b in bs]
    for a, b in [*ctx.results.get("pyan3", []), *jarvis]:
        if a in defined and b in defined and a != b:
            g.add_edge(a, b)
    cond = nx.condensation(g)
    order = [sorted(cond.nodes[c]["members"]) for c in reversed(list(nx.topological_sort(cond)))]
    dump("networkx", {"edges": sorted(g.edges), "order": order,
                      "cycles": [m for m in order if len(m) > 1]})
    return {"units": g.number_of_nodes(), "edges": g.number_of_edges(), "steps": len(order),
            "cycles": sum(len(m) > 1 for m in order)}


# ---------------------------------------------------------------- types, control flow, metrics
def t_pyright(ctx):
    """Type checking diagnostics (JSON) plus inferred return types from `--createstub`."""
    work = Path("/tmp/pyright")
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(ctx.view, work)
    p = subprocess.run(["pyright", "--outputjson", *ctx.py3_files], cwd=work, capture_output=True, text=True, timeout=900)
    report = json.loads(p.stdout)
    diags = [{"file": Path(d["file"]).relative_to(work).as_posix(), "line": d["range"]["start"]["line"] + 1,
              "severity": d["severity"], "rule": d.get("rule"), "message": d["message"]}
             for d in report.get("generalDiagnostics", [])]
    types = {}
    for name in [*ctx.packages, *ctx.modules]:
        subprocess.run(["pyright", "--createstub", name], cwd=work, capture_output=True, text=True, timeout=900)
        for stub in sorted((work / "typings").rglob("*.pyi")) if (work / "typings").exists() else []:
            mod = ".".join(stub.relative_to(work / "typings").with_suffix("").parts).removesuffix(".__init__")
            classes = []  # (indent, name) stack
            for line in stub.read_text(encoding="utf-8").splitlines():
                indent = len(line) - len(line.lstrip())
                classes = [c for c in classes if c[0] < indent]
                if m := re.match(r"\s*class (\w+)", line):
                    classes.append((indent, m.group(1)))
                elif m := re.match(r"\s*(?:async )?def (\w+)\((.*)\).*?# -> (.+):\s*$", line):
                    qual = ".".join([mod, *[c[1] for c in classes], m.group(1)])
                    types[qual] = m.group(3)
    dump("pyright", {"summary": report.get("summary"), "diagnostics": diags, "inferred_returns": types})
    rules = {}
    for d in diags:
        rules[d["rule"] or "other"] = rules.get(d["rule"] or "other", 0) + 1
    return {"diagnostics": len(diags), "by_rule": rules, "inferred_returns": len(types)}


def t_staticfg(ctx):
    """Intra-procedural CFGs: blocks, edges, and branch conditions (the boundaries tests should straddle)."""
    from staticfg import CFGBuilder

    # staticfg 0.9.5 crashes naming calls like super().f() or g()[0].h(). The name is only a label, and staticfg
    # builds nested CFGs with fresh CFGBuilder()s, so the fix has to go on the class itself.
    CFGBuilder.visit_Call = lambda self, node: self.current_block.func_calls.append(ast.unparse(node.func))

    def defs(node, prefix):
        # staticfg keys methods by bare name (four `initialize`s collide), so walk qualified names ourselves
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}.{child.name}"
                if not isinstance(child, ast.ClassDef):
                    yield qual, child
                yield from defs(child, qual)
            else:  # defs inside if/try/with blocks
                yield from defs(child, prefix)

    out, errors = {}, {}
    for f in ctx.py3_files:
        mod = f[:-3].replace("/", ".").removesuffix(".__init__")
        try:
            tree = ast.parse(read(ctx.view / f))
        except SyntaxError as e:
            errors[f] = f"SyntaxError: {e}"[:300]
            continue
        for qual, fn in defs(tree, mod):
            try:
                blocks = list(CFGBuilder().build(qual, ast.Module(body=fn.body, type_ignores=[])))
            except Exception as e:
                errors[qual] = f"{type(e).__name__}: {e}"[:300]
                continue
            # the condition's own line: b.at() is the line where its BLOCK starts, often a docstring or an
            # earlier statement; a negated else-condition built by staticfg may only carry lines on its operands
            def line_of(expr, block):
                return next((n.lineno for n in ast.walk(expr) if getattr(n, "lineno", None)), block.at())
            conds = [{"line": line_of(l.exitcase, b), "condition": ast.unparse(l.exitcase)} for b in blocks
                     for l in b.exits if l.exitcase is not None]
            n_edges = sum(len(b.exits) for b in blocks)
            out[qual] = {"blocks": len(blocks), "edges": n_edges, "branch_conditions": conds,
                         "cyclomatic": n_edges - len(blocks) + 2}
    dump("staticfg", {"functions": out, "errors": errors})
    return {"functions": len(out), "branch_conditions": sum(len(v["branch_conditions"]) for v in out.values()),
            "failed": len(errors)}


def t_lizard(ctx):
    """Per-function size and cyclomatic complexity for BOTH languages (token-based, so Python 2 works too)."""
    import lizard

    def funcs(path, rel):
        info = lizard.analyze_file(str(path))
        return [{"name": fn.long_name, "file": rel, "lines": [fn.start_line, fn.end_line], "nloc": fn.nloc,
                 "ccn": fn.cyclomatic_complexity, "tokens": fn.token_count, "params": len(fn.parameters)}
                for fn in info.function_list]
    python = [x for f in ctx.files for x in funcs(ctx.root / f, f)]
    java = [x for p in sorted(ctx.java.rglob("*.java")) for x in funcs(p, p.relative_to(ctx.java).as_posix())] \
        if ctx.java else []
    dump("lizard", {"python": python, "java": java})
    top = max(python, key=lambda x: x["ccn"], default=None)
    return {"python_functions": len(python), "python_nloc": sum(x["nloc"] for x in python),
            "max_ccn": f"{top['name']}={top['ccn']}" if top else None, "java_functions": len(java)}


def t_radon(ctx):
    """Raw metrics (LOC/SLOC/comments), cyclomatic complexity ranks and maintainability index per file."""
    from radon.complexity import cc_visit
    from radon.metrics import mi_visit
    from radon.raw import analyze
    out = {}
    for f in ctx.py3_files:
        src = read(ctx.view / f)
        out[f] = {"raw": analyze(src)._asdict(), "mi": round(mi_visit(src, multi=True), 2),
                  "cc": [{"name": b.fullname, "line": b.lineno, "complexity": b.complexity} for b in cc_visit(src)]}
    dump("radon", out)
    return {"loc": sum(v["raw"]["loc"] for v in out.values()), "sloc": sum(v["raw"]["sloc"] for v in out.values()),
            "min_mi": min((v["mi"], f) for f, v in out.items()) if out else None}


def t_stdlib_list(ctx):
    """Imports classified as internal / stdlib / third-party, separately for Python 2.7 and 3.12, from the
    ORIGINAL sources: py2-only stdlib modules (e.g. StringIO, urllib2) are migration hazards."""
    import tree_sitter_python
    from stdlib_list import stdlib_list
    from tree_sitter import Language, Parser
    py = Parser(Language(tree_sitter_python.language()))
    std27, std3 = set(stdlib_list("2.7")), set(stdlib_list("3.12"))
    imports = {}
    for f in ctx.files:
        names = set()

        def walk(n):
            if n.type == "import_statement":
                names.update(c.text.decode() for c in n.children if c.type in ("dotted_name", "aliased_import")
                             for c in ([c.child_by_field_name("name")] if c.type == "aliased_import" else [c]))
            elif n.type == "import_from_statement":
                mod = n.child_by_field_name("module_name")
                if mod is not None and not mod.text.decode().startswith("."):
                    names.add(mod.text.decode())
            for c in n.children:
                walk(c)
        walk(py.parse((ctx.root / f).read_bytes()).root_node)
        imports[f] = sorted(names)
    classes = {}
    for m in sorted({m for ms in imports.values() for m in ms}):
        top = m.split(".")[0]
        classes[m] = ("internal" if ctx.internal(m) else "future" if top == "__future__"
                      else "stdlib" if top in std3 and top in std27 else "stdlib_py2_only" if top in std27
                      else "stdlib_py3_only" if top in std3 else "third_party")
    dump("stdlib_list", {"imports": imports, "classification": classes})
    pick = lambda c: sorted({m.split(".")[0] for m, k in classes.items() if k == c})
    return {"third_party": pick("third_party"), "stdlib_py2_only": pick("stdlib_py2_only"),
            "stdlib": len(pick("stdlib"))}


TOOLS = [("libcst", t_libcst), ("tree_sitter", t_tree_sitter), ("fissix", t_fissix), ("modernize", t_modernize),
         ("pyan3", t_pyan3), ("jarviscg", t_jarviscg), ("grimp", t_grimp), ("networkx", t_networkx),
         ("pyright", t_pyright), ("staticfg", t_staticfg), ("lizard", t_lizard), ("radon", t_radon),
         ("stdlib_list", t_stdlib_list)]


def run_all(ctx):
    OUT.mkdir(parents=True, exist_ok=True)
    rows = {"context": ctx.describe(), "tools": {}}
    for name, fn in TOOLS:
        start = time.time()
        try:
            row = {"ok": True, "stats": fn(ctx)}
        except Exception as e:
            row = {"ok": False, "error": f"{type(e).__name__}: {e}"[:500], "traceback": traceback.format_exc()[-2000:]}
        row.update(version=md.version(DISTS[name]), seconds=round(time.time() - start, 2))
        rows["tools"][name] = row
        print(f"{'ok  ' if row['ok'] else 'FAIL'} {name:12} {json.dumps(row.get('stats') or row.get('error'))}",
              flush=True)
    dump("tools", rows)
    return rows


# ---------------------------------------------------------------- --verify: fixture with known answers
FIXTURE = {
    "demo/__init__.py": "",
    "demo/core.py": (
        "from demo import util\n\n\n"
        "class Shape:\n    def area(self):\n        return 0\n\n\n"
        "def total(xs):\n    if not xs:\n        return 0\n    return sum(util.double(x) for x in xs)\n"),
    "demo/util.py": "def double(x):\n    return x * 2\n",
    # a UTF-8 byte-order mark (as in RepoTransBench's json-rpc) must not break any tool
    "demo/bom.py": "﻿def triple(x):\n    return x * 3\n",
    "demo/legacy.py": (
        "import StringIO\n\n\n"
        "def report(d):\n    for k, v in d.iteritems():\n        print \"%s=%d\" % (k, v / 2)\n"
        "    return unicode(len(d))\n"),
    "java/Demo.java": "public class Demo {\n    public int total(int[] xs) {\n        return xs.length;\n    }\n}\n",
}
# tool -> (what must hold on the fixture, check over that tool's raw output)
EXPECT = {
    "libcst": ("qualified name and resolved cross-module call demo.core.total -> demo.util.double",
               lambda o: any(s["qualname"] == "demo.core.total" and "demo.util.double" in s["calls"] for s in o["symbols"])),
    "tree_sitter": ("parses the Python 2 file without errors, and finds Demo.total in Java",
                    lambda o: not o["python"]["demo/legacy.py"]["has_error"]
                    and any(d["name"] == "Demo.total" for d in o["java"]["Demo.java"]["definitions"])),
    "fissix": ("2to3 flags print, dict and unicode in legacy.py",
               lambda o: {"fix_print", "fix_dict", "fix_unicode"} <= set(o["hits"]["demo/legacy.py"])),
    "modernize": ("flags the classic division `v / 2`",
                  lambda o: "fix_classic_division" in o["hits"]["demo/legacy.py"]),
    "pyan3": ("uses edge demo.core.total -> demo.util.double",
              lambda o: ["demo.core.total", "demo.util.double"] in o["uses"]),
    "jarviscg": ("edge demo.core.total -> demo.util.double",
                 lambda o: "demo.util.double" in o.get("demo.core.total", [])),
    "grimp": ("import demo.core -> demo.util", lambda o: "demo.util" in o["imports"]["demo.core"]),
    "networkx": ("demo.util.double is ordered before demo.core.total",
                 lambda o: [i for i, m in enumerate(o["order"]) if "demo.util.double" in m][0]
                 < [i for i, m in enumerate(o["order"]) if "demo.core.total" in m][0]),
    # double(x) returns x * 2 with x untyped, so its type is unknown and rightly left out
    "pyright": ("infers return type `int` for demo.core.total",
                lambda o: o["inferred_returns"].get("demo.core.total") == "int"),
    "staticfg": ("branch condition `not xs` in demo.core.total, at its own line (10)",
                 lambda o: any(c["condition"] == "not xs" and c["line"] == 10
                               for c in o["functions"]["demo.core.total"]["branch_conditions"])),
    # total: 1 + `if` + the generator's `for`
    "lizard": ("CCN 3 for total, and the Java method is measured",
               lambda o: any(f["name"].startswith("total") and f["ccn"] == 3 for f in o["python"]) and o["java"]),
    "radon": ("counts LOC for every file", lambda o: all(v["raw"]["loc"] > 0 for f, v in o.items() if "__init__" not in f)),
    "stdlib_list": ("StringIO classified as a py2-only stdlib module",
                    lambda o: o["classification"].get("StringIO") == "stdlib_py2_only"),
}


def verify():
    root = Path("/tmp/fixture")
    shutil.rmtree(root, ignore_errors=True)
    for rel, text in FIXTURE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    rows = run_all(Ctx(root, root / "java"))
    results = {}
    for name, (claim, check) in EXPECT.items():
        ok = rows["tools"][name]["ok"]
        if ok:
            try:
                ok = bool(check(json.loads((OUT / f"{name}.json").read_text())))
            except Exception:
                ok = False
        results[name] = {"ok": ok, "checks": claim, "version": rows["tools"][name]["version"],
                         "error": rows["tools"][name].get("error")}
    dump("verify", results)
    print("\n" + "\n".join(f"[{'PASS' if r['ok'] else 'FAIL'}] {n} {r['version']}: {r['checks']}"
                           for n, r in results.items()))
    return all(r["ok"] for r in results.values())


if __name__ == "__main__":
    if "--verify" in sys.argv:
        sys.exit(0 if verify() else 1)
    run_all(Ctx("/code", "/java"))
