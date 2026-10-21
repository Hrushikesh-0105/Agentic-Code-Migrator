"""Translator / Repair output -> Layer 1 pieces (layer1/protocol.py), placed by the skeleton.

The agents write, per fragment, {"id", "imports", "code", "declaration"}: only members (and a class's declaration
line). Packages, containers and slots come from the skeleton, so a model cannot put code in the wrong place:
  class            preamble (package, imports) + "<declaration> {  <fields>  // @acm:slot <class id>  }"
                   (a nested class goes into its parent's slot)
  module code      preamble + the module's container class (scaffold, shared) + the code in the container's slot
  method           preamble (imports into the class's file) + the code in its class's slot
Python target: one piece per fragment, its own file (splice).
Other targets: layer2/layout_code.py.
"""
import re

from layer1.protocol import Piece

TYPE_DECL = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")


def _imports(lines):
    out = []
    for x in lines or []:
        x = str(x).strip().rstrip(";").strip()
        if x.startswith("import "):
            x = x[len("import "):].strip()
        if x and re.fullmatch(r"(static\s+)?[\w$.]+(\.\*)?", x):
            out.append(f"import {x};")
    return out


def split_code(code):
    """Pull `package` / `import` lines a model put in member code out of it. -> (code, imports)."""
    keep, imports = [], []
    for line in (code or "").replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if s.startswith("package ") and s.endswith(";"):
            continue
        if s.startswith("import ") and s.endswith(";"):
            imports.append(s[len("import "):-1])
            continue
        keep.append(line)
    return "\n".join(keep).strip("\n"), imports


def project_imports(sk, loc, code, imports):
    """Imports fixed against the skeleton: an import of a project class that does not exist (a guessed package) is
    dropped, and every project class the code names from another package is imported."""
    types = sk.types()
    known = {fqn for fqn in types.values() if fqn}
    base = sk.base.split(".")[0]
    out = [i for i in imports if not (i[len("import "):-1].startswith(base + ".") and not i.endswith(".*;")
                                      and i[len("import "):-1] not in known)]
    text = re.sub(r'"(?:\\.|[^"\\])*"', '""', code)  # names inside string literals are not references
    for name, fqn in sorted(types.items()):
        if fqn and fqn.rsplit(".", 1)[0] != loc["package"] and re.search(rf"(?<![\w.]){re.escape(name)}\b", text):
            line = f"import {fqn};"
            if line not in out and not any(i.endswith(f".{name};") for i in out):
                out.append(line)
    return out


def unwrap(code, name):
    """Members a model wrapped in a declaration of the target class itself (`public class Name { ... }`): the
    members, and the declaration line it used. Code that is anything else comes back unchanged."""
    m = re.match(r"\s*((?:@\w+\s*)*(?:(?:public|protected|private|abstract|final|static)\s+)*"
                 r"(?:class|interface|enum|record)\s+" + re.escape(name) + r"\b[^{;]*)\{", code)
    if not m or not code.rstrip().endswith("}"):
        return code, None
    body = code[m.end():code.rstrip().rfind("}")]
    lines = body.strip("\n").split("\n")
    ind = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
    return "\n".join(l[ind:] for l in lines), m.group(1).strip()


def declaration(text, name, nested, kind="class"):
    """A class declaration line with the skeleton's name; nested classes are static."""
    d = (text or "").strip().rstrip("{").strip() or f"public {kind} {name}"
    d = re.sub(r"\s+", " ", d)
    m = TYPE_DECL.search(d)
    if not m:
        d, m = f"public class {name} " + d, None
        m = TYPE_DECL.search(d)
    d = d[:m.start(2)] + name + d[m.end(2):]
    if nested and m.group(1) == "class" and not re.search(r"\bstatic\b", d[:m.start()]):
        d = d[:m.start()] + "static " + d[m.start():]
    if not nested:
        d = re.sub(r"\b(private|protected|static)\s+", "", d[:m.start()]) + d[m.start():]
    return d.strip()


def indent(code, n=4):
    return "\n".join((" " * n + l) if l.strip() else "" for l in code.split("\n"))


def build(sk, f, out, java=True):
    """Pieces for fragment `f` from an agent's output {"imports", "code", "declaration"}, in the protocol's
    normal form (every field present), so they compare equal to the same pieces after a round trip via Layer 1.
    A generic layout (layer2/layout.py: every target but Java and Python -> Python) builds its own."""
    if getattr(sk, "generic", False):
        return sk.pieces(f, out)
    return [Piece(**p).model_dump() for p in _build(sk, f, out, java)]


def _build(sk, f, out, java):
    fid = f["id"]
    code, more = split_code(out.get("code", ""))
    if not java:
        return [{"fragment": fid, "target_file": f["file"], "code": (out.get("code") or "").rstrip("\n") + "\n"}]
    loc = sk.location(fid)
    code, wrapped_decl = unwrap(code, loc["name"])
    if wrapped_decl and loc["role"] == "class" and not out.get("declaration"):
        out = {**out, "declaration": wrapped_decl}
    pre = {"fragment": fid, "target_file": loc["file"], "section": "preamble",
           "code": "\n".join([f"package {loc['package']};"] + project_imports(sk, loc, code, _imports(
               list(out.get("imports") or []) + more)))}
    if loc["role"] == "class":
        decl = declaration(out.get("declaration"), loc["name"], nested=bool(loc["slot"]))
        body = (indent(code) + "\n\n" if code.strip() else "") + f"    // @acm:slot {fid}"
        piece = {"fragment": fid, "target_file": loc["file"], "code": f"{decl} {{\n{body}\n}}"}
        if loc["slot"]:
            piece["slot"] = loc["slot"]
        return [pre, piece]
    if loc["slot"] == f["module"]:  # module-level code: the module's container class
        scaffold = {"fragment": fid, "target_file": loc["file"], "scaffold": True,
                    "code": f"public final class {loc['name']} {{\n    // @acm:slot {f['module']}\n}}"}
        return [pre, scaffold, {"fragment": fid, "target_file": loc["file"], "slot": f["module"],
                                "code": code or f"// {fid}: nothing to translate"}]
    return [pre, {"fragment": fid, "target_file": loc["file"], "slot": loc["slot"],
                  "code": code or f"// {fid}: nothing to translate"}]


def apply_edits(code, edits):
    """Surgical patches: each {"find", "replace"} must match the code exactly once (whitespace at line ends and
    indentation are forgiven). Returns (new code, problems)."""
    problems = []
    for e in edits or []:
        find, rep = str(e.get("find", "")), str(e.get("replace", ""))
        if not find:
            problems.append("an edit without `find`")
            continue
        if code.count(find) == 1:
            code = code.replace(find, rep)
            continue
        norm = lambda s: "\n".join(l.strip() for l in s.strip("\n").split("\n"))  # noqa: E731
        lines, want = code.split("\n"), norm(find).split("\n")
        hits = [k for k in range(len(lines) - len(want) + 1)
                if [l.strip() for l in lines[k:k + len(want)]] == want]
        if len(hits) != 1:
            problems.append(f"`find` text {'not found' if not hits else 'found %d times' % len(hits)}: "
                            f"{find.strip()[:80]!r}")
            continue
        k = hits[0]
        ind = lines[k][:len(lines[k]) - len(lines[k].lstrip())]
        new = [ind + l if l.strip() else l for l in rep.strip("\n").split("\n")] if rep.strip() else []
        lines[k:k + len(want)] = new
        code = "\n".join(lines)
    return code, problems
