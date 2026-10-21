"""The target skeleton: where each Python fragment lives in the migrated project, and how a test case that calls a
Python function or method calls its translation. Deterministic, derived from the Layer 0 bundle alone.

Java target (assembly "compose"):
  Python class          a Java class of the same name (a nested class: a static nested class of its parent)
  module functions,     static members of one container class per module: the class the RepoTransBench target
  constants, init code  tests expect for the module's functions when Layer 0 linked them (EncodingUtils), else
                        <Module>Module (<Package>Package for an __init__.py)
  method                a member of its class, named in camelCase (want_bytes -> wantBytes) unless a target hint names
                        it; __init__ -> constructor, __str__ -> toString, __eq__ -> equals, __hash__ -> hashCode
  package               the expected API's package for hinted classes, else the target package plus the Python
                        package path (pkg/sub/mod.py -> <base>.sub)
Each fragment's code goes into a slot of its class (layer1/protocol.py: compose), so the Translator writes only
members; the class declarations and containers are built here.

Python target (assembly "splice"): every fragment stays in its own file and place.
A source other than Python (C# -> Java) is read through layer2/source.py: constructors, statics and properties
by the source language's own rules. Every other target is laid out by layer2/layout.py.
"""
import ast
import re
from collections import defaultdict

from layer2.dummy import JAVA_KEYWORDS, camel, ident
from layer2.source import Source

DUNDER = {"__str__": "toString", "__repr__": "repr", "__eq__": "equals", "__hash__": "hashCode", "__len__": "len",
          "__iter__": "iterator", "__next__": "next", "__call__": "call", "__contains__": "contains",
          "__getitem__": "getItem", "__setitem__": "setItem", "__delitem__": "delItem", "__lt__": "lessThan",
          "__le__": "lessEqual", "__gt__": "greaterThan", "__ge__": "greaterEqual", "__ne__": "notEquals",
          "__bool__": "isTrue", "__nonzero__": "isTrue", "__enter__": "enter", "__exit__": "exit",
          "__getattr__": "getAttr", "__setattr__": "setAttr"}
STATIC_DECORATORS = ("staticmethod", "classmethod")


def camel_member(name):
    """want_bytes -> wantBytes, _make_keys_list -> makeKeysList, __str__ -> toString."""
    if name in DUNDER:
        return DUNDER[name]
    parts = [p for p in name.strip("_").split("_") if p] or ["call"]
    s = parts[0][:1].lower() + parts[0][1:] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
    return ident(s)


def _decorators(f):
    return [d.lstrip("@").split("(")[0] for d in (f.get("signature") or {}).get("decorators", [])]


def is_property(f):
    return any(d == "property" or d.endswith((".setter", ".getter", ".deleter")) or d.endswith("cached_property")
               for d in _decorators(f))


def is_static(f):
    return any(d.split(".")[-1] in STATIC_DECORATORS for d in _decorators(f))


def is_overload_only(f):
    """Only `@typing.overload` stubs (no implementation): nothing to call."""
    return f["source"].count("def ") > 0 and all("overload" in d for d in _decorators(f)) and _decorators(f)


class Skeleton:
    def __init__(self, bundle):
        man, frs = bundle["manifest"], bundle["fragments"]
        self.frag = {f["id"]: f for f in frs}
        self.language = man["migration"]["target_language"]
        self.src = Source(bundle)
        self.loc = {}
        if self.language != "java":
            for f in frs:
                self.loc[f["id"]] = {"file": f["file"], "role": f["kind"]}
            return
        target = man.get("target") or {}
        self.base = target.get("package") or "acm.migrated." + ident(man["repo"]["name"].lower())
        api = target.get("expected_api") or {}
        files = {x["module"]: x["file"] for x in man["files"]}
        python_classes = defaultdict(set)  # java package -> names of Java classes made from Python classes
        hint = {f["id"]: (f.get("target_hints") or [{}])[0].get("java") for f in frs}

        def package_of(mod):
            parts = mod.split(".")
            sub = parts[1:] if files.get(mod, "").endswith("__init__.py") else parts[1:-1]
            return self.base + "".join("." + ident(p.lower()) for p in sub)

        def api_pkg(java_class, default):
            q = (api.get(java_class) or {}).get("qualified")
            return q.rsplit(".", 1)[0] if q and "." in q else default

        taken = defaultdict(set)  # java package -> taken top-level names
        self.cls = {}
        for f in sorted((f for f in frs if f["kind"] == "class"), key=lambda f: f["id"].count(".")):
            leaf = ident(f["qualname"].split(".")[-1])
            h = hint[f["id"]]
            if f["parent"] and f["parent"] in self.cls:
                p = self.cls[f["parent"]]
                self.cls[f["id"]] = {"package": p["package"], "name": leaf, "fqn": p["fqn"] + "." + leaf,
                                     "binary": p["binary"] + "$" + leaf, "file": p["file"], "slot": f["parent"]}
                continue
            name = h if h and "#" not in h and re.fullmatch(r"[A-Za-z_]\w*", h) else leaf
            pkg = api_pkg(name, package_of(f["module"]))
            if name in taken[pkg]:
                name = leaf if leaf not in taken[pkg] else leaf + camel(f["module"].split(".")[-1])
            taken[pkg].add(name)
            python_classes[pkg].add(name)
            self.cls[f["id"]] = {"package": pkg, "name": name, "fqn": f"{pkg}.{name}", "binary": f"{pkg}.{name}",
                                 "file": self.path(pkg, name), "slot": None}
        self.container = {}
        for mod in sorted(files):
            votes = defaultdict(int)
            for f in frs:
                h = hint[f["id"]]
                if f["module"] == mod and f["kind"] == "function" and h and "#" in h:
                    votes[h.split("#")[0]] += 1
            header = hint.get(f"{mod}::header")
            pkg = package_of(mod)
            name = None
            if votes:
                name = sorted(votes, key=lambda k: (-votes[k], k))[0]
            elif header and "#" not in header and api.get(header) and not api[header].get("constructed"):
                name = header  # a static-only class the tests expect for this module (e.g. Itsdangerous)
            if name:
                pkg = api_pkg(name, pkg)
            if not name or name in python_classes[pkg] or name in taken[pkg]:
                kind = "Package" if files[mod].endswith("__init__.py") else "Module"
                name = camel(mod.split(".")[-1]) + kind
                while name in taken[pkg]:
                    name += "_"
            taken[pkg].add(name)
            self.container[mod] = {"package": pkg, "name": name, "fqn": f"{pkg}.{name}", "binary": f"{pkg}.{name}",
                                   "file": self.path(pkg, name), "slot": mod}
        members = defaultdict(set)
        for f in sorted(frs, key=lambda f: (f["file"], f["segments"][0]["start_line"])):
            fid, kind = f["id"], f["kind"]
            if kind == "class":
                c = self.cls[fid]
                self.loc[fid] = {**c, "role": "class", "member": None}
            elif kind in ("module_header", "module_init"):
                self.loc[fid] = {**self.container[f["module"]], "role": kind, "member": None}
            elif kind == "function" or (kind == "method" and f["parent"] not in self.cls):
                c = self.container[f["module"]]
                self.loc[fid] = {**c, "role": "static", "member": self._member(f, hint[fid], members[c["fqn"]])}
            else:
                c = self.cls[f["parent"]]
                role = self.src.role(fid)
                member = None if role == "constructor" else self._member(f, hint[fid], members[c["fqn"]])
                self.loc[fid] = {**{k: c[k] for k in ("package", "name", "fqn", "binary", "file")}, "slot": f["parent"],
                                 "role": role, "member": member}

    def types(self):
        """Simple name -> fully qualified name of every top-level Java type the skeleton defines (None when two
        packages define the same name)."""
        if not hasattr(self, "_types"):
            self._types = {}
            for t in list(self.cls.values()) + list(self.container.values()):
                if "$" in t["binary"]:
                    continue
                self._types[t["name"]] = None if t["name"] in self._types and self._types[t["name"]] != t["fqn"] \
                    else t["fqn"]
        return self._types

    @staticmethod
    def path(pkg, name):
        return "src/main/java/" + pkg.replace(".", "/") + f"/{name}.java"

    @staticmethod
    def _member(f, h, taken):
        leaf = f["qualname"].split(".")[-1]
        name = h.split("#")[1] if h and "#" in h and h.split("#")[1] != "<init>" else camel_member(leaf)
        if name in taken and not name.startswith("_"):  # e.g. `_sign` next to `sign`
            name = "_" + name
        taken.add(name)
        return name

    def location(self, fid):
        return self.loc[fid]

    def describe(self, fid):
        """One line for prompts: where the fragment lives in Java and how to refer to it."""
        l = self.loc[fid]
        if self.language != "java":
            return f"{fid}: {l['file']}"
        if l["role"] == "class":
            return f"{fid} -> class {l['fqn']} ({l['file']})"
        if l["role"] in ("module_header", "module_init"):
            return f"{fid} -> static members of {l['fqn']}"
        if l["role"] == "constructor":
            return f"{fid} -> constructor(s) of {l['fqn']}"
        what = {"static": "static method", "property": "getter method (no arguments)", "method": "method"}[l["role"]]
        return f"{fid} -> {what} {l['fqn']}.{l['member']}"

    # ------------------------------------------------------------------------------------------- test calls
    def testable(self, fid):
        """Can the fragment be called by a test? (functions and methods with an implementation)"""
        return self.src.testable(fid)

    def python_call(self, fid):
        """How the Python harness reaches the fragment: {module, callable, kind, needs_init, params}."""
        f = self.frag[fid]
        qual = f["qualname"][len(f["module"]) + 1:]
        leaf = qual.split(".")[-1]
        params = list((f.get("signature") or {}).get("params", []))
        if f["kind"] == "function":
            return {"module": f["module"], "callable": qual, "kind": "function", "needs_init": False, "params": params}
        cls = qual.rsplit(".", 1)[0]
        if leaf == "__init__":
            return {"module": f["module"], "callable": cls, "kind": "constructor", "needs_init": False,
                    "params": params[1:]}
        if is_property(f):
            return {"module": f["module"], "callable": qual, "kind": "property", "needs_init": True, "params": [],
                    "attr": True}
        if is_static(f):
            first = params[:1] if params and params[0]["name"] in ("cls",) else []
            return {"module": f["module"], "callable": qual, "kind": "static", "needs_init": False,
                    "params": params[len(first):]}
        return {"module": f["module"], "callable": qual, "kind": "method", "needs_init": True, "params": params[1:]}

    def java_call(self, fid, args, init_args):
        """The Java harness's call for a case: {class, kind, member, args, init}."""
        l = self.loc[fid]
        if l["role"] == "constructor":
            return {"class": l["binary"], "kind": "new", "member": None, "args": args}
        if l["role"] == "static":
            return {"class": l["binary"], "kind": "static", "member": l["member"], "args": args}
        return {"class": l["binary"], "kind": "instance", "member": l["member"],
                "args": [] if l["role"] == "property" else args, "init": init_args}

    def target_call(self, fid, args, init_args):
        return self.java_call(fid, args, init_args)

    def constructor_params(self, fid):
        """Parameters of the constructor of the fragment's class (its own or the nearest base class's __init__)."""
        return self.class_constructor(self.frag[fid]["parent"])

    def class_constructor(self, class_id, inherited_only=False):
        """(constructor fragment id, its parameters without self) of a class: its own, or the nearest base's."""
        return self.src.constructor(class_id, inherited_only)


def literal(text):
    """A parameter default as a value, or raise ValueError."""
    return ast.literal_eval(text)


def positional(params, args, kwargs):
    """Python args/kwargs -> one positional list for a Java call (Java has no keywords). A skipped parameter before
    a given keyword gets its literal default. Returns (list, problem or None)."""
    out, kw = list(args), dict(kwargs)
    names = [p["name"] for p in params if p["kind"] not in ("var_positional", "var_keyword")]
    if any(p["kind"] == "var_positional" for p in params) and kw:
        return out, "keyword arguments with *args"
    for p in params[len(out):]:
        if p["kind"] in ("var_positional", "var_keyword"):
            break
        if not kw:
            break
        if p["name"] in kw:
            out.append(kw.pop(p["name"]))
        elif p.get("default") is not None:
            try:
                literal(p["default"])
            except (ValueError, SyntaxError):
                return out, f"parameter {p['name']} has a non-literal default ({p['default']})"
            out.append({"$literal": p["default"]})
        else:
            return out, f"missing argument {p['name']}"
    if kw:
        return out, f"keyword arguments {sorted(kw)} have no Java position" if set(kw) - set(names) else \
            f"keyword arguments {sorted(kw)} out of order"
    return out, None


__all__ = ["Skeleton", "camel_member", "positional", "literal", "JAVA_KEYWORDS", "is_property", "is_static"]
