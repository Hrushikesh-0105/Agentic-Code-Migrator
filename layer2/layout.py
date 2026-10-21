"""The target layout for any target language: where each fragment's translation goes, under which name, and how a
differential test calls it. Deterministic, from the Layer 0 bundle, the language profiles (langs/) and what the
benchmark target's tests expect (layer2/expected.py).

    make_skeleton(bundle)  ->  layer2.skeleton.Skeleton   Java target (class files, its own tested rules), or
                                                          Python -> Python (splice: fragments stay in place)
                           ->  Layout                     every other target

Layout, by the profile's `layout`:
  module_files   (Python, JavaScript, Matlab)  one file per source module (a C / C++ header and its source file
                 form one module); classes hold their methods in a slot; functions and module code at top level
  package_files  (Go, Rust, C++, C)           one file per source module in one package / crate / include dir:
                 Go  `package <pkg>`, structs, methods with receivers at top level, constructors New<Name>
                 Rust `src/<m>.rs` + `src/lib.rs` (`pub mod <m>;`) + Cargo.toml; struct + `impl` slot
                 C++ header-only `include/<m>.hpp`, classes with their methods inside, free functions `inline`
                 C   `src/<m>.c`, structs and functions
  class_files    (C#)                          one file per class in one namespace; module-level code in a static
                 container class <Module>Module
Names: the spelling the target tests use when they use one, else the language's convention where it is forced
(Go exported PascalCase, C# PascalCase), else the source name. Overloads (`~2`) get `<name>_2` in languages
without overloading. The Translator writes only member code; declarations, files and slots come from here.
"""
import re
from collections import defaultdict

from langs import get as get_lang

from .expected import Expectations, find_target_project, norm
from .source import Source, leaf

OVERLOADING = ("java", "csharp", "cpp")
SLOT_METHODS = ("python3", "rust", "cpp", "csharp", "javascript", "matlab")  # methods live inside a class slot


def snake(name):
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return re.sub(r"\W+", "_", s).strip("_").lower() or "m"


def pascal(name):
    parts = [p for p in re.split(r"[^A-Za-z0-9]+|_", name) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "X"


class Layout:
    generic = True

    def __init__(self, bundle, expectations=None):
        man, frs = bundle["manifest"], bundle["fragments"]
        self.frag = {f["id"]: f for f in frs}
        self.src = Source(bundle)
        self.lang = get_lang(man["migration"]["target_language"])
        self.language = self.lang.id
        self.exp = expectations if expectations is not None else \
            Expectations(find_target_project(man), self.language)
        repo = man["repo"]["name"]
        self.crate = self.exp.crate or snake(repo.split("_", 1)[-1] if "_" in repo else repo)
        self.package = re.sub(r"[^a-z0-9]", "", repo.split("_", 1)[-1].lower()) or "acm"
        self.namespace = pascal(repo.split("_", 1)[-1]) if self.language == "csharp" else None
        for m, _ in self.exp.imports:
            if self.language == "csharp" and m.split(".")[0] not in ("System", "Xunit", "Moq", "FluentAssertions",
                                                                      "NUnit", "Microsoft"):
                self.namespace = m
                break
        self.loc, self._modules = {}, {}
        self._place_modules(frs)
        taken = defaultdict(set)
        for f in sorted(frs, key=lambda f: (f["kind"] != "class", f["id"].count("."), f["id"])):
            self.loc[f["id"]] = self._place(f, taken)

    # ------------------------------------------------------------------------------------------------ modules
    def _group(self, f):
        """Source modules that become one target module: a C / C++ header joins its source file."""
        mod = f["module"]
        return mod[:-2] if self.src.id in ("c", "cpp") and mod.endswith("_h") else mod

    def _place_modules(self, frs):
        groups = defaultdict(list)
        for f in frs:
            groups[self._group(f)].append(f)
        used = set()
        for g in sorted(groups):
            names = [leaf(f) for f in groups[g] if f["kind"] in ("class", "function") or
                     (f["kind"] == "method" and self.src.role(f["id"]) == "static")]
            stem = g.split(".")[-1]
            if stem in ("__init__", "index", "mod", "lib") and "." in g:
                stem = g.split(".")[-2]
            m, k = self._module_file(stem, self.exp.module_for(names)), 2
            while (m["file"] or m["stem"]) in used:
                m, k = self._module_file(f"{stem}{k}", None), k + 1
            used.add(m["file"] or m["stem"])
            self._modules[g] = m

    def _module_file(self, stem, imported):
        """{file, stem, call_module} of a target module."""
        L, exp = self.language, self.exp
        if L == "python3":
            if imported and "." in imported and imported.split(".")[0] in ("src", self.crate):
                path = imported.replace(".", "/") + ".py"
            else:
                path = exp.path_for(stem, (".py",)) or f"src/{snake(stem)}.py"
            return {"file": path, "stem": path.rsplit("/", 1)[-1][:-3], "call_module": path[:-3].replace("/", ".")}
        if L == "rust":
            name = imported.split("::")[-1] if imported and imported.split("::")[0] == self.crate and "::" in imported \
                else snake(stem)
            if name in ("lib", "main", "mod", "tests"):
                name += "_"
            return {"file": f"src/{name}.rs", "stem": name, "call_module": name}
        if L == "go":
            return {"file": f"{self.package}/{snake(stem)}.go", "stem": snake(stem), "call_module": self.package}
        if L in ("cpp", "c"):
            hdr = exp.path_for(stem, (".h", ".hpp")) if L == "cpp" else None
            inc = next((m for m, _ in exp.imports if norm(m.rsplit("/", 1)[-1].rsplit(".", 1)[0]) == norm(stem)), None)
            if L == "cpp":
                path = hdr or (f"include/{inc.rsplit('/', 1)[-1]}" if inc else f"include/{snake(stem)}.hpp")
            else:
                path = f"src/{snake(stem)}.c"
            return {"file": path, "stem": snake(stem), "call_module": path}
        if L == "javascript":
            return {"file": f"src/{stem}.js", "stem": stem, "call_module": f"src/{stem}.js"}
        if L == "matlab":
            return {"file": f"{stem}.m", "stem": stem, "call_module": None}
        if L == "csharp":
            return {"file": None, "stem": pascal(stem), "call_module": None}
        raise ValueError(f"no target layout for {self.lang.name}")

    def module_of(self, fid):
        return self._modules[self._group(self.frag[fid])]

    # ------------------------------------------------------------------------------------------------ names
    def _spelled(self, name, forced):
        """The tests' spelling of `name` (never a special __name__), else `forced(name)`."""
        s = self.exp.spelling(name)
        if s and not s.startswith("__") and s not in self.lang.keywords and norm(s) == norm(name):
            return s
        return forced(name)

    def _ident(self, name):
        return name + "_" if name in self.lang.keywords else name

    def class_name(self, cid):
        f = self.frag[cid]
        name = self._spelled(leaf(f), pascal if self.src.id == "c" or self.language in ("go", "rust", "csharp")
                             else (lambda n: n))
        parent = f.get("parent")
        if parent in self.frag and self.frag[parent]["kind"] == "class" and self.language not in \
                ("python3", "cpp", "csharp", "javascript"):
            name = self.class_name(parent) + name  # no nested types: Outer + Inner
        return self._ident(name)

    def _member_name(self, f, role, cls_name):
        name = leaf(f)
        special = self._special(name)
        if role == "constructor":
            return {"python3": "__init__", "go": f"New{cls_name}", "rust": "new", "c": f"{snake(cls_name)}_new",
                    "javascript": "constructor"}.get(self.language, cls_name)
        if special:
            return special
        conv = {"go": pascal, "csharp": pascal}.get(self.language, lambda n: n)
        if self.language == "python3" and name.startswith("__") and name.endswith("__"):
            return name
        out = self._spelled(name, conv)
        k = re.search(r"~(\d+)$", f["id"])
        if k and self.language not in OVERLOADING:
            out += k.group(1) if self.language == "go" else f"_{k.group(1)}"
        if self.language == "go" and role in ("static", "function") and self.src.class_of(f["id"]) and \
                not self.src.python:
            out = cls_name + out  # Go has no static methods: Class.member -> ClassMember()
        if self.language == "c" and self.src.class_of(f["id"]):
            out = f"{snake(cls_name)}_{out}"
        return self._ident(out)

    def _special(self, name):
        """A special method (Python dunder, Java toString, ...) -> the target's name for it, or None."""
        dunder = name if name.startswith("__") and name.endswith("__") else \
            next((d for d, n in self.src.lang.special_methods.items() if n == name and d != "__init__"), None)
        if not dunder or dunder == "__init__":
            return None
        if self.language == "python3":
            return dunder
        t = self.lang.special_methods.get(dunder)
        return t.split(" ")[0] if t and not t.startswith("<") else None

    # ------------------------------------------------------------------------------------------------ placement
    def _place(self, f, taken):
        fid, mod = f["id"], self.module_of(f["id"])
        role = self.src.role(fid)
        if f["kind"] in ("module_header", "module_init"):
            if self.language == "csharp":
                c = self._container(f)
                return {**c, "role": f["kind"], "member": None, "name": c["class"]}
            return {"file": mod["file"], "role": f["kind"], "slot": None, "member": None, "name": None,
                    "class": None, "call_module": mod["call_module"]}
        if f["kind"] == "class":
            name = self.class_name(fid)
            parent = f.get("parent") if self.frag.get(f.get("parent") or "", {}).get("kind") == "class" else None
            nested = bool(parent) and self.language in ("python3", "cpp", "csharp", "javascript")
            file = mod["file"] if self.language != "csharp" else \
                (self.loc[parent]["file"] if nested else f"src/{name}.cs")
            return {"file": file, "role": "class", "name": name, "class": name, "member": None,
                    "slot": parent if nested else ("ns:" + self.namespace if self.language == "csharp" else None),
                    "call_module": mod["call_module"], "qualified": self._qualified(name, parent if nested else None)}
        cls = self.src.class_of(fid)
        if cls:
            c = self.loc.get(cls) or self._place(self.frag[cls], taken)
            member = self._member_name(f, role, c["name"])
            if role != "constructor":
                while member in taken[c["name"]] and not re.search(r"~\d+$", fid) and \
                        self.language not in OVERLOADING:
                    member += "_"
                taken[c["name"]].add(member)
            in_slot = self.language in SLOT_METHODS and not (self.language == "go")
            if self.language == "go" or self.language == "c":
                in_slot = False
            return {"file": c["file"], "role": role, "name": c["name"], "class": c["name"], "member": member,
                    "slot": cls if in_slot else None, "call_module": c["call_module"],
                    "qualified": c.get("qualified")}
        role = "function" if role in ("static", "method") else role
        if self.language == "csharp":
            c = self._container(f)
            return {**c, "role": "static", "member": self._member_name(f, "static", c["class"]),
                    "name": c["class"]}
        member = self._member_name(f, "function", None)
        while member in taken[mod["file"]] and not re.search(r"~\d+$", fid) and self.language not in OVERLOADING:
            member += "_"
        taken[mod["file"]].add(member)
        return {"file": mod["file"], "role": "function", "name": None, "class": None, "member": member,
                "slot": None, "call_module": mod["call_module"]}

    def _container(self, f):
        """C#: the static class that holds a module's functions and constants."""
        mod = self.module_of(f["id"])
        name = mod["stem"] + "Module"
        return {"file": f"src/{name}.cs", "slot": f"container:{self._group(f)}", "class": name,
                "call_module": None, "qualified": self._qualified(name, None), "container": True}

    def _qualified(self, name, parent):
        if self.language == "csharp":
            return (self.loc[parent]["qualified"] + "+" if parent else self.namespace + ".") + name
        return name

    # ------------------------------------------------------------------------------------------------ queries
    def location(self, fid):
        return self.loc[fid]

    def types(self):
        return {}

    def describe(self, fid):
        """One line for prompts: where the fragment's translation goes in the target and what it is called."""
        l = self.loc[fid]
        where = f"in {l['file']}"
        if l["role"] == "class":
            return f"{fid} -> {self.lang.name} type {l['name']} {where}"
        if l["role"] in ("module_header", "module_init"):
            return f"{fid} -> top-level code of {l['file']}" + (f" (static members of {l['class']})"
                                                                 if l.get("container") else "")
        if l["role"] == "constructor":
            what = {"go": f"constructor function {l['member']}", "rust": f"associated fn {l['name']}::new",
                    "python3": f"{l['name']}.__init__"}.get(self.language, f"constructor(s) of {l['name']}")
            return f"{fid} -> {what} {where}"
        if l["role"] == "function":
            return f"{fid} -> function {l['member']} {where}"
        kind = {"static": "static method", "property": "property", "method": "method"}[l["role"]]
        if self.language == "go" and l["role"] == "static":
            return f"{fid} -> function {l['member']} {where}"
        return f"{fid} -> {kind} {l['member']} of {l['name']} {where}"

    def testable(self, fid):
        return self.src.testable(fid) if not self.src.python else \
            self.frag[fid]["kind"] in ("function", "method") and self.src.testable(fid)

    def constructor_params(self, fid):
        cls = self.src.class_of(fid)
        return self.src.constructor(cls) if cls else (None, [])

    def class_constructor(self, class_id, inherited_only=False):
        return self.src.constructor(class_id, inherited_only)

    def python_call(self, fid):
        """Python sources: how runner.py reaches the fragment in the legacy code."""
        from .skeleton import Skeleton
        return Skeleton.python_call(self, fid)

    def target_call(self, fid, args, init_args):
        """The neutral call (sandbox/polyglot.py) that reaches the translation, with type-tagged arguments."""
        l, role = self.loc[fid], self.loc[fid]["role"]
        kind = {"function": "function", "static": "static", "constructor": "constructor",
                "property": "property"}.get(role, "method")
        call = {"kind": kind, "member": l["member"], "args": [] if kind == "property" else list(args)}
        if l.get("call_module"):
            call["module"] = l["call_module"]
        if l.get("class"):
            call["class"] = l.get("qualified") or l["class"]
        if self.language == "go" and kind == "static":
            call = {"kind": "function", "member": l["member"], "args": list(args), "module": l["call_module"]}
        if self.language == "python3" and kind == "static":
            call["kind"] = "static"
        if kind in ("method", "property"):
            call["init"] = list(init_args)
            cls = self.src.class_of(fid)
            ctor, _ = self.src.constructor(cls) if cls else (None, [])
            if self.language == "go":
                call["constructor"] = f"New{l['class']}" if ctor else f"<{l['class']}>"
            elif self.language == "rust":
                call["constructor"] = "new"
        if kind == "constructor":
            call["member"] = None
            if self.language in ("go", "rust"):
                call["constructor"] = l["member"]
        if self.language == "c" and kind in ("method", "static"):
            call = {"kind": "function", "member": l["member"], "args": list(args), "module": l["call_module"]}
        return call

    # ------------------------------------------------------------------------------------------------ pieces
    def pieces(self, f, out):
        from . import layout_code
        return layout_code.pieces(self, f, out)


def make_skeleton(bundle, expectations=None):
    """The target skeleton for a bundle: Skeleton (Java; Python -> Python splice) or Layout (everything else)."""
    from .skeleton import Skeleton
    mig = bundle["manifest"]["migration"]
    t, s = mig["target_language"], mig["source_language"]
    if t == "java" or (t.startswith("python") and s.startswith("python")):
        return Skeleton(bundle)
    return Layout(bundle, expectations)
