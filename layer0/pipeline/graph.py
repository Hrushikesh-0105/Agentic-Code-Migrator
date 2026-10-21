# Runs INSIDE acm-sandbox/analysis (Python 3.12). Layer 0, steps 1 and 3: the fragment dependency graph and the
# bottom-up migration order.
#
# An edge A -> B means "A depends on B, so B is migrated first". Edge kinds:
#   call          A calls B                              reference   A reads B (a class, constant, function object)
#   inherit       class A derives from class B           decorator   A is decorated by B
#   constructor   A instantiates a class whose __init__ is B (looked up through internal base classes)
#   use           pyan3's "uses" relation                member      method/nested class A lives in class B
#   module        A needs its module's header (imports and constants)
#   init          (Java targets) class A is declared together with its own constructor B: with B's `member` edge
#                 back to A they form one step, since a Java class does not compile without its constructors
# Each edge records which source found it (`via`: libcst, pyan3, jarviscg, structure).
import re
from collections import defaultdict

import networkx as nx


class Resolver:
    """Maps a fully qualified name to the fragment that defines it."""

    def __init__(self, defs, modules):
        self.defs, self.modules = defs, set(modules)
        self.frags = dict(defs)
        self.names = defaultdict(dict)  # module -> {module-level name: fragment id that binds it}

    def add_module_fragments(self, modfrags):
        self.frags.update(modfrags)
        for f in modfrags.values():
            for name in f.defines:
                # a name bound in both header and init (rebinding) resolves to init, which runs later
                if f.kind == "module_init" or name not in self.names[f.module]:
                    self.names[f.module][name] = f.id

    def resolve_def(self, fq):
        """The function/method/class fragment `fq` names or lies inside, else None (module-level or external)."""
        parts = fq.split(".")
        for i in range(len(parts), 0, -1):
            p = ".".join(parts[:i])
            if p in self.defs:
                return p
            if p in self.modules:
                return None
        return None

    def resolve(self, fq):
        """(fragment id or None, internal?). Module-level names resolve to the header/init fragment binding them;
        an internal name with no fragment (e.g. an empty __init__.py) gives (None, True)."""
        parts = fq.split(".")
        for i in range(len(parts), 0, -1):
            p = ".".join(parts[:i])
            if p in self.defs:
                return p, True
            if p in self.modules:
                header = f"{p}::header"
                fid = self.names[p].get(parts[i]) if i < len(parts) else None
                fid = fid or (header if header in self.frags else None)
                return fid, True
        return None, False

    def is_descendant(self, child, ancestor):
        p = self.frags[child].parent if child in self.frags else None
        while p is not None:
            if p == ancestor:
                return True
            p = self.frags[p].parent
        return False


JARVIS_TYPES = {"map": "dict"}  # JARVIS writes methods of builtin types as <map>.get, <str>.format, <list>.append


def _jarvis_name(name):
    name = name.replace("<builtin>.", "builtins.")
    m = re.match(r"<(\w+)>(\..+)?$", name)
    return f"builtins.{JARVIS_TYPES.get(m.group(1), m.group(1))}{m.group(2) or ''}" if m else name


def build_edges(resolver, pyan_uses, jarvis, code_of):
    """`code_of(fid)` is the fragment's code: a pyan3-only edge is kept only if the target's name occurs in it as
    an identifier (pyan3 sometimes maps a field such as `self._data` onto a property `data`)."""
    frags, defs = resolver.frags, resolver.defs
    edges = {}
    external = defaultdict(set)
    recursive = set()
    dropped = []

    def add(src, dst, kind, via):
        if dst is None or src is None or src == dst:  # names inside a fragment (locals, nested defs) fold onto it
            return
        if resolver.is_descendant(dst, src):  # a class shell does not depend on its own methods
            return
        e = edges.setdefault((src, dst), {"kinds": set(), "via": set()})
        e["kinds"].add(kind)
        e["via"].add(via)

    # internal base classes per class, for constructor lookup through inheritance
    bases = defaultdict(list)
    for f in defs.values():
        if f.kind == "class":
            for q, kinds in f.refs.items():
                fid, _ = resolver.resolve(q)
                if "inherit" in kinds and fid in defs and defs[fid].kind == "class" and fid != f.id:
                    bases[f.id].append(fid)

    def constructor(cls):
        seen, queue = set(), [cls]
        while queue:
            c = queue.pop(0)
            if c in seen:
                continue
            seen.add(c)
            if f"{c}.__init__" in defs:
                return f"{c}.__init__"
            queue.extend(bases[c])
        return None

    for f in frags.values():  # structure
        if f.parent is not None:
            add(f.id, f.parent, "member", "structure")
        if f.kind != "module_header":
            add(f.id, f"{f.module}::header" if f"{f.module}::header" in frags else None, "module", "structure")

    # recursion = an exact self-CALL (LibCST or JARVIS). pyan3's "uses" also links `@name.setter` to `name`.
    for f in defs.values():
        if "call" in f.refs.get(f.qualname, ()):
            recursive.add(f.id)
    for a, bs in jarvis.items():
        if a in defs and a in bs:
            recursive.add(a)

    for f in frags.values():  # LibCST references
        for q, kinds in f.refs.items():
            fid, internal = resolver.resolve(q)
            if not internal:
                external[f.id].add(q)
                continue
            for k in kinds:
                add(f.id, fid, k, "libcst")
            if "call" in kinds and fid in defs and defs[fid].kind == "class" \
                    and not resolver.is_descendant(f.id, fid):
                add(f.id, constructor(fid), "constructor", "libcst")

    # pyan3 and JARVIS resolve what LibCST cannot (self.method(), super() calls), but both also guess: pyan3 maps
    # the field `self._data` onto a property `data`; JARVIS links `self.serialize(...)` to unrelated functions
    # named *serialize*. An edge only they report is kept if the target's name occurs in the source fragment's
    # code (`\b`: `data` does not match `self._data`; for `C.__init__` the name sought is `C`).
    def evidenced(src, fid, name):
        parts = name.rsplit(".", 2)
        leaf = parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]
        return (src, fid) in edges or re.search(rf"\b{re.escape(leaf)}\b", code_of(src))

    for a, bs in jarvis.items():  # JARVIS: also names the builtin/stdlib/third-party callee
        src = resolver.resolve_def(a)
        if src is None:
            continue
        for b in bs:
            b = _jarvis_name(b)
            fid, internal = resolver.resolve(b)
            if not internal:
                external[src].add(b)
            elif fid is not None and fid != src and not resolver.is_descendant(fid, src):
                if evidenced(src, fid, b):
                    add(src, fid, "call", "jarviscg")
                else:
                    dropped.append([src, fid, b, "jarviscg"])

    for a, b in pyan_uses:
        if a.startswith("*") or b.startswith("*") or "^" in a or "^" in b:
            continue
        src, (fid, internal) = resolver.resolve_def(a), resolver.resolve(b)
        if not internal or src is None or fid is None or src == fid or resolver.is_descendant(fid, src):
            continue
        if evidenced(src, fid, b):
            add(src, fid, "use", "pyan3")
        else:
            dropped.append([src, fid, b, "pyan3"])
    return edges, external, recursive, dropped


def migration_order(frag_ids, edges, sort_key):
    """Steps in bottom-up order. Strongly connected components (mutual recursion, or classes that use each
    other) become ONE step, since neither side can be migrated first. `wave` is the length of the longest
    dependency chain below a step: steps in the same wave are independent and could run in parallel."""
    g = nx.DiGraph()
    g.add_nodes_from(frag_ids)
    g.add_edges_from(edges)
    cond = nx.condensation(g)
    members = {c: sorted(cond.nodes[c]["members"], key=sort_key) for c in cond}
    steps = []
    for wave, generation in enumerate(nx.topological_generations(cond.reverse(copy=True))):
        for c in sorted(generation, key=lambda c: sort_key(members[c][0])):
            steps.append({"step": len(steps) + 1, "wave": wave, "fragments": members[c],
                          "cycle": len(members[c]) > 1})
    return steps
