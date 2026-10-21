"""The source side of a fragment, for any source language of langs/: what it is (constructor, property, static or
instance method, function), which class it belongs to, its parameters, and the call a differential test makes to
reach it in the legacy code.

Layer 0 records fragments the same way for every language (kind, qualname, parent, signature {text, params,
decorators}); what differs is how a language marks a constructor or a static member:
  constructor   Python `__init__`; JavaScript `constructor`; Java / C# / C++ / Matlab a method named like its class
  static        Python @staticmethod / @classmethod; a `static` modifier (Java, C#, C++, JavaScript); every member
                of a C# `static class`
  property      Python @property; a C# property (Layer 0 marks it)
The legacy call is sandbox/polyglot.py's neutral form ({kind, module, class, member, constructor, args, init});
a Python source keeps sandbox/harness/runner.py's own form, which also traces executed lines.
"""
import re

STATIC_DECORATORS = ("staticmethod", "classmethod")
CALLABLE = ("function", "method")


def leaf(f):
    return f["qualname"].split(".")[-1].split("::")[-1]


def decorators(f):
    return [d.lstrip("@").split("(")[0] for d in (f.get("signature") or {}).get("decorators") or []]


def _modifiers(f):
    """Modifier words before the name in the signature text (Java / C# / C++ / JavaScript)."""
    sig = (f.get("signature") or {}).get("text") or ""
    head = sig.split("(")[0]
    return set(re.findall(r"\b(static|abstract|public|private|protected|internal|virtual|override|async)\b",
                          head)) | {w for d in decorators(f) for w in d.split()}


class Source:
    """Source-language facts about a bundle's fragments."""

    def __init__(self, bundle):
        from langs import get
        mig = bundle["manifest"]["migration"]
        self.lang = get(mig["source_language"])
        self.id = self.lang.id
        self.python = self.id in ("python2", "python3")
        self.frag = {f["id"]: f for f in bundle["fragments"]}

    # ------------------------------------------------------------------------------------------------ roles
    def class_of(self, fid):
        """The class fragment a method belongs to (None for a function, or a method whose class Layer 0 did not
        find, e.g. a C++ method of a class declared outside the project)."""
        p = self.frag[fid].get("parent")
        return p if p and self.frag.get(p, {}).get("kind") == "class" else None

    def is_constructor(self, fid):
        f = self.frag[fid]
        if f["kind"] != "method":
            return False
        name = leaf(f)
        if self.python:
            return name == "__init__"
        if self.id == "javascript":
            return name == "constructor"
        cls = self.class_of(fid)
        owner = leaf(self.frag[cls]) if cls else f["qualname"].split(".")[-2] if "." in f["qualname"] else None
        return bool(owner) and name == owner and self.id in ("java", "csharp", "cpp", "matlab")

    def is_property(self, fid):
        f = self.frag[fid]
        if self.python:
            return any(d == "property" or d.endswith((".setter", ".getter", ".deleter")) or d.endswith("cached_property")
                       for d in decorators(f))
        return bool((f.get("signature") or {}).get("property"))

    def is_static(self, fid):
        f = self.frag[fid]
        if self.python:
            return any(d.split(".")[-1] in STATIC_DECORATORS for d in decorators(f))
        if "static" in _modifiers(f):
            return True
        cls = self.class_of(fid)
        return bool(cls) and "static" in _modifiers(self.frag[cls])  # a C# static class

    def role(self, fid):
        """class | module_header | module_init | function | constructor | property | static | method."""
        f = self.frag[fid]
        if f["kind"] != "method":
            return f["kind"]
        if self.is_constructor(fid):
            return "constructor"
        if self.is_property(fid):
            return "property"
        if self.is_static(fid) or not self.class_of(fid) and not self.python:
            return "static"
        return "method"

    def params(self, fid):
        """Parameters a caller passes (Python's self / cls dropped)."""
        f = self.frag[fid]
        ps = list((f.get("signature") or {}).get("params") or [])
        if self.python and f["kind"] == "method" and ps and ps[0]["name"] in ("self", "cls"):
            if self.role(fid) != "static" or ps[0]["name"] == "cls":
                ps = ps[1:]
        return ps

    def constructor(self, class_id, inherited_only=False):
        """(constructor fragment id, its parameters) of a class: its own, or the nearest base class's."""
        seen, todo = set(), [class_id]
        while todo:
            c = todo.pop(0)
            if c in seen or c not in self.frag:
                continue
            seen.add(c)
            ctors = sorted(k for k in self.frag if self.class_of(k) == c and self.is_constructor(k))
            if ctors and not (inherited_only and c == class_id):
                best = min(ctors, key=lambda k: len(self.params(k)))  # the simplest overload
                return best, self.params(best)
            todo += [d for d in self.frag[c]["depends_on"] if self.frag.get(d, {}).get("kind") == "class"]
        return None, []

    def testable(self, fid):
        """Can a differential test call it in the legacy code?"""
        f = self.frag[fid]
        if f["kind"] not in CALLABLE:
            return False
        if self.python:
            ds = decorators(f)
            return not (ds and all("overload" in d for d in ds))
        if self.id == "matlab":  # only a file's primary function is on the path
            return f["kind"] == "function" and leaf(f) == f["file"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if self.id == "javascript" and self.frag.get(f.get("parent") or "", {}).get("kind") == "module_header":
            return False
        return "abstract" not in _modifiers(f)

    # ------------------------------------------------------------------------------------------------ calls
    def java_class(self, class_id):
        """Binary name of a Java class: package.Outer$Inner."""
        f = self.frag[class_id]
        pkg = f["module"].rsplit(".", 1)[0] if "." in f["module"] else ""
        return (pkg + "." if pkg else "") + f["qualname"].replace(".", "$")

    def call(self, fid):
        """The neutral call (without arguments) that reaches the fragment in the legacy code: {kind, module,
        class, member, constructor}. Not used for Python sources (see layer2.skeleton.Skeleton.python_call)."""
        f, role = self.frag[fid], self.role(fid)
        cls = self.class_of(fid)
        kind = {"function": "function", "constructor": "constructor", "property": "property",
                "static": "static"}.get(role, "method")
        if role == "static" and not cls:
            kind = "function"
        call = {"kind": kind, "member": leaf(f)}
        if self.id == "java":
            call["class"] = self.java_class(cls) if cls else f["module"]
        elif cls:
            call["class"] = leaf(self.frag[cls]) if self.id != "csharp" else self.frag[cls]["qualname"]
        elif "." in f["qualname"] and self.id in ("cpp", "javascript"):
            call["class"] = f["qualname"].rsplit(".", 1)[0].replace(".", "::" if self.id == "cpp" else ".")
        if self.id in ("cpp", "c", "javascript"):
            call["module"] = f["file"]
        if self.id == "javascript" and kind == "function" and "class" in call:
            call["kind"] = "static"  # a function of an object-literal module: obj.member
        return call
