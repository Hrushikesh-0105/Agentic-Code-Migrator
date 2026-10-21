"""A stand-in for Layer 2 that passes every unit, so Layer 1 can be built and tested before the agents exist.

What it returns as the "translation":
  Python target (splice)   the fragment's source, unchanged (so the assembled codebase must equal the original)
  Java target (compose)    a compilable stub per fragment: one Java class per Python class (extending its migrated
                           base); one `<Name>Module` class per Python module (`<Name>Package` for an __init__.py)
                           holding its functions as static methods; methods and constructors that throw
                           UnsupportedOperationException
  other targets (compose)  the generic layout (layer2/layout.py) with a comment in place of each fragment's code
It tests nothing: the verdict's evidence says so.

For testing Layer 1, a `script` makes chosen fragments misbehave: {fragment_id: [action for attempt 1, 2, ...]}
(the last action repeats). Actions: pass, fail, error, needs_human, malformed (a pass verdict missing a
translation), raise (an exception), crash (SimulatedCrash, which is not caught: it stands in for the process
being killed). A request carrying human guidance always passes.
"""
import re
from collections import defaultdict

from layer1.protocol import PROTOCOL

JAVA_KEYWORDS = set("""abstract assert boolean break byte case catch char class const continue default do double
else enum extends final finally float for goto if implements import instanceof int interface long native new package
private protected public return short static strictfp super switch synchronized this throw throws transient try void
volatile while true false null var record yield sealed permits""".split())
ACTIONS = ("pass", "fail", "error", "needs_human", "malformed", "raise", "crash")


class SimulatedCrash(BaseException):
    """Not an Exception, so Layer 1 does not catch it: the run stops as if the process were killed."""


def ident(name):
    s = re.sub(r"\W", "_", name, flags=re.ASCII)
    if not s or s[0].isdigit():
        s = "_" + s
    return s + "_" if s in JAVA_KEYWORDS or s == "_" else s


def camel(name):
    return ident("".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", name) if p) or "Module")


def _key(f):
    return (f["file"], f["segments"][0]["start_line"])


def _unique(taken, name, alt=None):
    for cand in [name] + ([name + alt] if alt else []) + [f"{name}{i}" for i in range(2, 1000)]:
        if cand not in taken:
            taken.add(cand)
            return cand


class JavaNames:
    """Deterministic Python -> Java names for the whole bundle (the dummy's stand-in for a target skeleton)."""

    def __init__(self, bundle):
        man, frs = bundle["manifest"], bundle["fragments"]
        self.frag = {f["id"]: f for f in frs}
        base = (man.get("target") or {}).get("package") or "acm.migrated." + ident(man["repo"]["name"].lower())
        files = {x["module"]: x["file"] for x in man["files"]}
        taken = defaultdict(set)  # Java package -> top-level type names
        self.module = {}
        for mod in sorted(files):
            parts = mod.split(".")
            sub = parts[1:] if files[mod].endswith("__init__.py") else parts[1:-1]
            pkg = base + "".join("." + ident(p.lower()) for p in sub)
            kind = "Package" if files[mod].endswith("__init__.py") else "Module"
            self.module[mod] = (pkg, _unique(taken[pkg], camel(parts[-1]) + kind))
        self.cls = {}

        def resolve(fid):
            if fid not in self.cls:
                f, leaf = self.frag[fid], ident(self.frag[fid]["qualname"].split(".")[-1])
                if f["parent"]:
                    p = resolve(f["parent"])
                    self.cls[fid] = {"pkg": p["pkg"], "name": leaf, "fqn": p["fqn"] + "." + leaf, "file": p["file"]}
                else:
                    pkg = self.module[f["module"]][0]
                    name = _unique(taken[pkg], leaf, camel(f["module"].split(".")[-1]))
                    self.cls[fid] = {"pkg": pkg, "name": name, "fqn": f"{pkg}.{name}", "file": self.path(pkg, name)}
            return self.cls[fid]

        for f in sorted((f for f in frs if f["kind"] == "class"), key=_key):
            resolve(f["id"])
        self.method, members = {}, defaultdict(set)
        for f in sorted((f for f in frs if f["kind"] in ("method", "function")), key=_key):
            leaf = f["qualname"].split(".")[-1]
            owner = f["parent"] if f["kind"] == "method" else f["module"]
            self.method[f["id"]] = None if f["kind"] == "method" and leaf == "__init__" else \
                _unique(members[owner], ident(leaf))

    @staticmethod
    def path(pkg, name):
        return "src/main/java/" + pkg.replace(".", "/") + f"/{name}.java"

    def pieces(self, f, extends=None):
        fid, kind = f["id"], f["kind"]
        throw = f'    throw new UnsupportedOperationException("dummy Layer 2: {fid}");'
        if kind in ("module_header", "function", "module_init"):
            pkg, name = self.module[f["module"]]
            file = self.path(pkg, name)
            body = (f"// {fid} ({f['loc']} LOC)\npublic static Object {self.method[fid]}(Object... args) {{\n{throw}\n}}"
                    if kind == "function" else
                    f"// {fid}: {kind.replace('_', ' ')}, {f['loc']} LOC, not translated by the dummy Layer 2")
            return [{"fragment": fid, "target_file": file, "section": "preamble", "code": f"package {pkg};"},
                    {"fragment": fid, "target_file": file, "scaffold": True,
                     "code": f"public final class {name} {{\n    // @acm:slot {f['module']}\n}}"},
                    {"fragment": fid, "target_file": file, "slot": f["module"], "code": body}]
        if kind == "class":
            c = self.cls[fid]
            decl = ("public static class " if f["parent"] else "public class ") + c["name"] + \
                (f" extends {extends}" if extends else "")
            piece = {"fragment": fid, "target_file": c["file"],
                     "code": f"// {fid} ({f['loc']} LOC)\n{decl} {{\n    // @acm:slot {fid}\n}}"}
            if f["parent"]:
                piece["slot"] = f["parent"]
            return [{"fragment": fid, "target_file": c["file"], "section": "preamble", "code": f"package {c['pkg']};"},
                    piece]
        c, m = self.cls[f["parent"]], self.method[fid]
        head = f"public {c['name']}(Object... args)" if m is None else f"public Object {m}(Object... args)"
        return [{"fragment": fid, "target_file": c["file"], "slot": f["parent"],
                 "code": f"// {fid} ({f['loc']} LOC)\n{head} {{\n{throw}\n}}"}]


class DummyLayer2:
    name = "dummy"

    def __init__(self, bundle, script=None):
        self.frag = {f["id"]: f for f in bundle["fragments"]}
        self.script = script or {}
        bad = {a for acts in self.script.values() for a in acts} - set(ACTIONS)
        if bad or set(self.script) - set(self.frag):
            raise ValueError(f"bad script: unknown actions {sorted(bad)} or fragments "
                             f"{sorted(set(self.script) - set(self.frag))}")
        mig = bundle["manifest"]["migration"]
        self.java = JavaNames(bundle) if mig["target_language"] == "java" else None
        self.layout = None
        if not self.java and not (mig["source_language"].startswith("python") and
                                  mig["target_language"].startswith("python")):
            from layer2.layout import Layout
            self.layout = Layout(bundle)
        self.bases = defaultdict(list)
        for e in bundle["graph"]["edges"]:
            if "inherit" in e["kinds"] and self.frag[e["target"]]["kind"] == "class":
                self.bases[e["source"]].append(e["target"])
        self.calls = []  # (unit, attempt) of every request, in order

    def _action(self, req):
        if req.get("human_guidance"):
            return "pass"
        for f in req["fragments"]:
            acts = self.script.get(f["id"])
            if acts:
                return acts[min(req["attempt"], len(acts)) - 1]
        return "pass"

    def _extends(self, f, available):
        def rank(b):
            leaf = self.frag[b]["qualname"].split(".")[-1]
            texts = [t.split("(")[0].split(".")[-1].strip() for t in f["signature"]["bases"]]
            return (texts.index(leaf) if leaf in texts else len(texts), b)
        bases = sorted((b for b in self.bases.get(f["id"], []) if b in available), key=rank)
        return self.java.cls[bases[0]]["fqn"] if bases else None

    def translate(self, req):
        if req["assembly"] == "splice":
            return [{"fragment": f["id"], "target_file": f["file"], "code": f["source"]} for f in req["fragments"]]
        if self.layout is not None:
            return [p for f in req["fragments"] for p in self.layout.pieces(f, {"code": ""})]
        available = {f["id"] for f in req["fragments"]} | \
            {d["id"] for d in req["dependencies"] if d["status"] == "migrated"}
        return [p for f in req["fragments"]
                for p in self.java.pieces(f, self._extends(f, available) if f["kind"] == "class" else None)]

    def process(self, req):
        self.calls.append((req["unit"], req["attempt"]))
        action = self._action(req)
        where = f"{req['unit']} attempt {req['attempt']}"
        if action == "raise":
            raise RuntimeError(f"scripted exception at {where}")
        if action == "crash":
            raise SimulatedCrash(f"scripted crash at {where}")
        v = {"protocol": dict(PROTOCOL), "unit": req["unit"], "attempt": req["attempt"], "outcome": "pass",
             "reason": "dummy Layer 2: passes without translating or testing",
             "translations": [], "evidence": {"notes": "dummy: no tests generated or run"},
             "trace": [{"agent": "dummy", "status": action}], "usage": {}}
        if action in ("fail", "error", "needs_human"):
            v["outcome"] = action
            v["reason"] = {"fail": "scripted failure: outputs differ on 1 generated test",
                           "error": "scripted error: sandbox unavailable",
                           "needs_human": "scripted ambiguity: the intended behaviour cannot be decided"}[action]
            if action == "fail":
                v["evidence"].update(tests_generated=1, tests_failed=1, failing=["scripted case"])
            return v
        v["translations"] = self.translate(req)
        if action == "malformed":
            v["translations"] = [p for p in v["translations"] if p["fragment"] != req["fragments"][0]["id"]]
        return v
