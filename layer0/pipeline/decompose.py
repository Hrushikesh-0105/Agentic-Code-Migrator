# Runs INSIDE acm-sandbox/analysis (Python 3.12). Layer 0, step 2: cut the repo into fragments.
# LibCST reads the Python 3 view of the repo (2to3 edits stay inside lines, so line numbers match), but every
# span indexes the ORIGINAL file: a fragment's source is the legacy code exactly as written, Python 2 included.
#
# Fragment kinds
#   function       a top-level def (nested defs and classes stay inside their enclosing function)
#   method         a def directly in a class body
#   class          the class "shell": header, decorators, class attributes; its methods are their own fragments
#   module_header  module-level imports, constants and other statements that use no project definitions
#   module_init    module-level statements that DO use project definitions (registries, `__main__` blocks, ...)
import ast
from collections import defaultdict

import libcst as cst
from libcst.metadata import (ExpressionContext, ExpressionContextProvider, FullRepoManager,
                             FullyQualifiedNameProvider, PositionProvider)

VENDORED = {"six", "simplejson", "ordereddict", "singledispatch", "typing_extensions", "enum34"}
TEST_NAME = ("test_", "tests_")


def module_name(rel):
    parts = rel[:-3].split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def norm_q(name):
    """LibCST writes nested scopes as f.<locals>.g; pyan3, JARVIS and staticfg write f.g. Use the latter."""
    return name.replace(".<locals>", "")


class Fragment:
    def __init__(self, fid, kind, qualname, module, file, parent=None):
        self.id, self.kind, self.qualname, self.module, self.file = fid, kind, qualname, module, file
        self.parent, self.lines, self.def_lines = parent, set(), []
        self.refs = defaultdict(set)    # fully qualified name -> {call, reference, inherit, decorator}
        self.imports = set()            # fq names imported by module-level import statements
        self.defines = set()            # module-level names bound by this fragment's statements
        self.signature, self.features, self.main_guard = None, {}, False


def scope(ctx):
    """Which analysed files are part of the importable library; the rest are reported, not fragmented."""
    included, excluded = [], []
    for f in ctx.files:
        head, name = f.split("/")[0], f.rsplit("/", 1)[-1]
        if name.startswith(TEST_NAME) or name.endswith("_test.py"):
            excluded.append({"file": f, "reason": "test file"})
        elif f not in ctx.py3_files:
            excluded.append({"file": f, "reason": "not convertible to Python 3 (2to3 failed); cannot be analysed"})
        elif "/" in f and head not in ctx.packages:
            excluded.append({"file": f, "reason": f"script outside the importable packages ({head}/ has no __init__.py)"})
        else:
            included.append(f)
    return included, excluded


# ---------------------------------------------------------------- signatures and language features
def _signature(module, node):
    ps = node.params
    params = [{"name": p.name.value, "default": module.code_for_node(p.default) if p.default else None,
               "annotation": module.code_for_node(p.annotation.annotation) if p.annotation else None,
               "kind": kind}
              for kind, group in (("positional_only", ps.posonly_params), ("positional", ps.params),
                                  ("keyword_only", ps.kwonly_params)) for p in group]
    for kind, p in (("var_positional", ps.star_arg), ("var_keyword", ps.star_kwarg)):
        if isinstance(p, cst.Param):
            params.append({"name": p.name.value, "default": None, "kind": kind,
                           "annotation": module.code_for_node(p.annotation.annotation) if p.annotation else None})
    head = "async def" if node.asynchronous else "def"
    return {"text": f"{head} {node.name.value}({module.code_for_node(ps)})"
                    + (f" -> {module.code_for_node(node.returns.annotation)}" if node.returns else ""),
            "params": params, "returns": module.code_for_node(node.returns.annotation) if node.returns else None,
            "decorators": [module.code_for_node(d.decorator) for d in node.decorators],
            "is_async": node.asynchronous is not None}


def _class_signature(module, node):
    args = ", ".join(module.code_for_node(a) for a in [*node.bases, *node.keywords])
    return {"text": f"class {node.name.value}({args})" if args else f"class {node.name.value}",
            "bases": [module.code_for_node(b.value) for b in node.bases],
            "keywords": [module.code_for_node(k) for k in node.keywords],
            "decorators": [module.code_for_node(d.decorator) for d in node.decorators]}


DYNAMIC = {"getattr": "dynamic_attribute", "setattr": "dynamic_attribute", "hasattr": "dynamic_attribute",
           "delattr": "dynamic_attribute", "eval": "eval_exec", "exec": "eval_exec", "compile": "eval_exec",
           "isinstance": "runtime_type_check", "issubclass": "runtime_type_check", "type": "runtime_type_check",
           "callable": "runtime_type_check", "super": "super_call"}


def _function_features(node):
    """Language features that need deliberate handling when migrating (esp. dynamic Python -> static Java)."""
    f = defaultdict(int)
    for n in ast.walk(node):
        if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            f["nested_definition"] += 1
        elif isinstance(n, (ast.Yield, ast.YieldFrom)):
            f["generator"] += 1
        elif isinstance(n, ast.Lambda):
            f["lambda"] += 1
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            f["comprehension"] += 1
        elif isinstance(n, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            f["exception_handling"] += 1
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            f["context_manager"] += 1
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            f["global_or_nonlocal"] += 1
        elif isinstance(n, ast.Starred):
            f["star_unpacking"] += 1
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in DYNAMIC:
            f[DYNAMIC[n.func.id]] += 1
        elif isinstance(n, ast.Attribute) and n.attr in ("__dict__", "__class__", "__name__"):
            f["introspection"] += 1
        elif isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple):
            f["tuple_return"] += 1
        elif isinstance(n, (ast.Await, ast.AsyncFor)):
            f["await"] += 1
    a = node.args
    if a.vararg:
        f["var_positional"] = 1
    if a.kwarg:
        f["var_keyword"] = 1
    if any(isinstance(d, (ast.List, ast.Dict, ast.Set)) for d in [*a.defaults, *a.kw_defaults] if d is not None):
        f["mutable_default"] = 1
    if a.defaults or any(d is not None for d in a.kw_defaults):
        f["default_arguments"] = 1
    if node.decorator_list:
        f["decorated"] = len(node.decorator_list)
    return dict(sorted(f.items()))


def _class_features(node):
    f = defaultdict(int)
    bases = [b for b in node.bases if not (isinstance(b, ast.Name) and b.id == "object")]
    if len(bases) > 1:
        f["multiple_inheritance"] = len(bases)
    if any(k.arg == "metaclass" for k in node.keywords):
        f["metaclass"] = 1
    for n in node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "") for d in n.decorator_list}
            if "property" in names or any(k in ("setter", "getter", "deleter") for k in names):
                f["property"] += 1
            if names & {"staticmethod", "classmethod"}:
                f["static_or_class_method"] += 1
            if n.name.startswith("__") and n.name.endswith("__") and n.name not in ("__init__", "__new__"):
                f["dunder_method"] += 1
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            f["class_attribute"] += 1
    if node.decorator_list:
        f["decorated"] = len(node.decorator_list)
    return dict(sorted(f.items()))


# ---------------------------------------------------------------- the LibCST pass
class _Collector(cst.CSTVisitor):
    """One pass per file: creates def fragments, attributes every reference to the innermost fragment (or, at
    module level, to the top-level statement it belongs to), and records which names module statements bind."""

    def __init__(self, file, module, mod_node, pos, fqn, ectx, frags):
        self.file, self.mod, self.module = file, module, mod_node
        self.pos, self.fqn, self.ectx, self.frags = pos, fqn, ectx, frags
        self.stack = []       # def fragments; nested (folded) defs push their owner again
        self.in_import = 0
        self.top = {id(s): i for i, s in enumerate(mod_node.body)}
        self.stmts = [{"index": i, "lines": set(), "refs": defaultdict(set), "imports": set(), "defines": set(),
                       "main_guard": False, "imports_only": False} for i in range(len(mod_node.body))]
        self.cur = None

    # -- helpers
    def _lines(self, node):
        r = self.pos[node]
        start = r.start.line
        for d in getattr(node, "decorators", ()):
            start = min(start, self.pos[d].start.line)
        return start, r.end.line

    def _owner(self):
        return self.stack[-1] if self.stack else None

    def _qual(self, node):
        names = sorted(norm_q(q.name) for q in self.fqn.get(node, ()))
        return names[0] if names else f"{self.mod}.{node.name.value}"

    def _sink(self):
        owner = self._owner()
        return owner.refs if owner is not None else (self.stmts[self.cur]["refs"] if self.cur is not None else None)

    def _record(self, node, kind):
        sink = self._sink()
        if sink is not None and not self.in_import:
            for q in self.fqn.get(node, ()):
                sink[norm_q(q.name)].add(kind)

    def on_visit(self, node):
        i = self.top.get(id(node))
        if i is not None:
            self.cur = i
            s, (a, b) = self.stmts[i], self._lines(node)
            s["lines"] = set(range(a, b + 1))
            if isinstance(node, cst.If) and "__name__" in self.module.code_for_node(node.test) \
                    and "__main__" in self.module.code_for_node(node.test):
                s["main_guard"] = True
            if isinstance(node, cst.SimpleStatementLine) and node.body and \
                    all(isinstance(x, (cst.Import, cst.ImportFrom)) for x in node.body):
                s["imports_only"] = True
        return super().on_visit(node)

    # -- definitions
    def _define(self, node, kind):
        owner = self._owner()
        if owner is not None and owner.kind in ("function", "method"):
            self.stack.append(owner)  # nested def/class: stays part of the enclosing function
            return
        qual = self._qual(node)
        if kind == "def":
            kind = "method" if owner is not None and owner.kind == "class" else "function"
        frag = self.frags.get(qual)
        if frag is None:
            frag = self.frags[qual] = Fragment(qual, kind, qual, self.mod, self.file,
                                               parent=owner.id if owner is not None else None)
        elif frag.file != self.file:  # same qualname in two files cannot happen for a well-formed package
            raise ValueError(f"qualname {qual} defined in {frag.file} and {self.file}")
        a, b = self._lines(node)
        frag.lines.update(range(a, b + 1))  # conditional re-definitions (if PY2: def f... else: def f...) merge
        frag.def_lines.append(self.pos[node.name].start.line)
        if frag.signature is None:
            frag.signature = _signature(self.module, node) if kind in ("function", "method") \
                else _class_signature(self.module, node)
        self.stack.append(frag)
        if isinstance(node, cst.ClassDef):
            for arg in node.bases:
                self._collect(arg.value, "inherit")
            for arg in node.keywords:  # metaclass=M: a dependency, but M is not a base (no inherited __init__)
                self._collect(arg.value, "reference")
        for d in node.decorators:
            self._collect(d.decorator, "decorator")

    def _collect(self, expr, kind):
        sink = self._sink()

        class Sub(cst.CSTVisitor):
            def visit_Name(s, n):
                for q in self.fqn.get(n, ()):
                    sink[norm_q(q.name)].add(kind)

            def visit_Attribute(s, n):
                for q in self.fqn.get(n, ()):
                    sink[norm_q(q.name)].add(kind)
        expr.visit(Sub())

    def visit_ClassDef(self, node):
        self._define(node, "class")

    def leave_ClassDef(self, node):
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._define(node, "def")

    def leave_FunctionDef(self, node):
        self.stack.pop()

    # -- references
    def visit_Import(self, node):
        self._imports(node)
        self.in_import += 1

    def leave_Import(self, node):
        self.in_import -= 1

    def visit_ImportFrom(self, node):
        self._imports(node)
        self.in_import += 1

    def leave_ImportFrom(self, node):
        self.in_import -= 1

    def _imports(self, node):
        if self._owner() is None and self.cur is not None and not isinstance(node.names, cst.ImportStar):
            for alias in node.names:
                for q in self.fqn.get(alias.name, ()):
                    self.stmts[self.cur]["imports"].add(norm_q(q.name))

    def visit_Call(self, node):
        self._record(node.func, "call")

    def visit_Name(self, node):
        c = self.ectx.get(node)
        if c in (ExpressionContext.STORE, ExpressionContext.DEL):
            if self._owner() is None and self.cur is not None:
                self.stmts[self.cur]["defines"].add(node.value)
            return
        self._record(node, "reference")

    def visit_Attribute(self, node):
        if self.ectx.get(node) not in (ExpressionContext.STORE, ExpressionContext.DEL):
            self._record(node, "reference")


def decompose(ctx, files):
    """Returns (fragments by id, module records, warnings). Fragments of module-level code are created after
    every def is known, because classifying a statement as header or init needs to know what is a definition."""
    paths = [str(ctx.view / f) for f in files]
    mgr = FullRepoManager(str(ctx.view), paths, {FullyQualifiedNameProvider})
    defs, modules, warnings = {}, {}, []
    for f in files:
        # bytes.splitlines breaks only on \n, \r\n, \r, like LibCST; str.splitlines would also break on \x0c etc.
        orig = (ctx.root / f).read_bytes().splitlines()
        view = (ctx.view / f).read_bytes().splitlines()
        if len(orig) != len(view):
            warnings.append(f"{f}: the 2to3 view changed the line count ({len(orig)} -> {len(view)}); "
                            "spans may be off for this file")
        w = mgr.get_metadata_wrapper_for_path(str(ctx.view / f))
        mod = module_name(f)
        col = _Collector(f, mod, w.module, w.resolve(PositionProvider), w.resolve(FullyQualifiedNameProvider),
                         w.resolve(ExpressionContextProvider), defs)
        w.visit(col)
        modules[mod] = {"file": f, "statements": col.stmts, "lines": len(orig),
                        "vendored": mod.split(".")[-1] in VENDORED}
        # language features from the view's ast, matched by the `def`/`class` line
        tree = ast.parse((ctx.view / f).read_bytes())
        by_line = {(n.lineno, n.name): n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for frag in defs.values():
            if frag.file == f and not frag.features:
                node = by_line.get((frag.def_lines[0], frag.qualname.rsplit(".", 1)[-1]))
                if node is not None:
                    frag.features = _class_features(node) if isinstance(node, ast.ClassDef) else _function_features(node)

    # A class shell owns its span minus its methods' and nested classes' spans.
    for frag in defs.values():
        if frag.parent is not None:
            defs[frag.parent].lines -= frag.lines
    return defs, modules, warnings


def module_fragments(defs, modules, resolve_def):
    """Split each module's top-level code into a header and an init fragment.

    A statement is `init` if it is a `__main__` guard, references a project definition (resolve_def), or uses a
    name bound by an earlier init statement; everything else (imports, constants, compat shims) is `header`.
    Lines of definitions nested in module-level if/try blocks belong to those definitions, not to the module.
    """
    owned = defaultdict(set)
    for frag in defs.values():
        owned[frag.file] |= frag.lines
    frags = {}
    for mod, m in modules.items():
        header = Fragment(f"{mod}::header", "module_header", mod, mod, m["file"])
        init = Fragment(f"{mod}::init", "module_init", mod, mod, m["file"])
        init_names = set()
        for s in m["statements"]:
            lines = s["lines"] - owned[m["file"]]
            if not lines:
                continue
            uses_def = any(resolve_def(q) is not None for q in s["refs"])
            is_init = s["main_guard"] or uses_def or any(q.rsplit(".", 1)[-1] in init_names and
                                                         q.rsplit(".", 1)[0] == mod for q in s["refs"])
            target = init if is_init else header
            if is_init:
                init_names |= s["defines"]
                target.main_guard |= s["main_guard"]
            target.lines |= lines
            target.defines |= s["defines"]
            target.imports |= s["imports"]
            for q, kinds in s["refs"].items():
                target.refs[q] |= kinds
        for frag in (header, init):
            if frag.lines:
                frags[frag.id] = frag
    return frags
