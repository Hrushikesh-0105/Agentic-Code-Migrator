"""Translator / Repair output -> Layer 1 pieces (layer1/protocol.py, compose mode) for a generic Layout.

The agents write, per fragment, {"id", "imports", "code", "declaration"}. Files, declarations' names, slots and
project scaffolding come from the layout, so a model cannot put code in the wrong place:
  every file        preamble: its header line (Go `package`, C++ `#pragma once`) + imports (those a model put
                    in its code are moved here; project modules the code names are imported automatically)
  class             "<declaration> { <fields> <slot> }" in the language's form (Python `class X:` with an
                    indented slot, Rust `pub struct` + an `impl` block slot, C++ with `public:`, Go / C a plain
                    struct whose methods stay at top level)
  method            the code in its class's slot (Python, Rust, C++, C#, JavaScript) or at top level (Go, C)
  function, module  top-level code of the module's file (C#: the module's static container class)
  scaffolding       shared, identical copies kept once: Cargo.toml + src/lib.rs (`pub mod m;`), go.mod,
                    Python package __init__.py files, the C# namespace block
"""
import re
import textwrap

from layer1.protocol import Piece

DECL = re.compile(r"\b(class|struct|interface|enum|record|trait|type|classdef)\s+([A-Za-z_]\w*)")
DEFAULT_DECL = {"python3": "class {n}", "cpp": "class {n}", "csharp": "public class {n}",
                "rust": "#[derive(Debug, Clone, PartialEq)]\npub struct {n}", "go": "type {n} struct",
                "javascript": "class {n}", "c": "typedef struct {n}", "matlab": "classdef {n}"}


def comment(lang, text):
    return {"python3": "#", "matlab": "%"}.get(lang, "//") + " " + text


def slot_line(lang, name):
    return comment(lang, f"@acm:slot {name}")


def indent(code, n=4):
    return "\n".join((" " * n + l) if l.strip() else "" for l in code.split("\n"))


# ------------------------------------------------------------------------------------------------ imports
def split_code(code, lang):
    """Pull file-level lines (imports, package / namespace lines) a model put in its code out of it.
    -> (code, imports, dropped header lines)."""
    keep, imports = [], []
    lines = (code or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        top = line == line.lstrip()
        if lang == "python3" and top and re.match(r"(import\s+[\w.]|from\s+[\w.]+\s+import\s)", s):
            if s.startswith("from") and s.endswith("(") or s.endswith("\\"):
                while i + 1 < len(lines) and not (lines[i].rstrip().endswith(")") and "(" in s):
                    i += 1
                    s += " " + lines[i].strip()
                    if lines[i].rstrip().endswith(")"):
                        break
            imports.append(s.replace("\\", " "))
        elif lang == "go" and top and s.startswith("package "):
            pass
        elif lang == "go" and top and s.startswith("import ("):
            while i + 1 < len(lines) and lines[i + 1].strip() != ")":
                i += 1
                if lines[i].strip():
                    imports.append(lines[i].strip())
            i += 1
        elif lang == "go" and top and s.startswith("import "):
            imports.append(s[len("import "):].strip())
        elif lang == "rust" and top and re.match(r"(pub\s+)?use\s", s) and s.endswith(";"):
            imports.append(s)
        elif lang in ("cpp", "c") and top and (s.startswith("#include") or s == "#pragma once"
                                               or re.match(r"using\s+namespace\s+[\w:]+\s*;", s)):
            if s != "#pragma once":
                imports.append(s)
        elif lang == "csharp" and top and re.match(r"using\s+[\w.]+\s*;", s):
            imports.append(s)
        elif lang == "csharp" and top and re.match(r"namespace\s+[\w.]+\s*;", s):
            pass
        elif lang == "javascript" and top and re.match(r"(const|let|var)\s+.*=\s*require\(", s):
            imports.append(s)
        else:
            keep.append(line)
        i += 1
    return "\n".join(keep).strip("\n"), imports


def import_line(lang, x):
    x = str(x).strip()
    if not x:
        return None
    if lang == "python3":
        return x if x.startswith(("import ", "from ")) else f"import {x}"
    if lang == "go":
        x = x[len("import "):].strip() if x.startswith("import ") else x
        alias, _, path = x.rpartition(" ")
        return f"import {alias + ' ' if alias else ''}\"{path.strip(chr(34))}\""
    if lang == "rust":
        x = re.sub(r"^(pub\s+)?use\s+", "", x).rstrip(";").strip()
        return f"use {x};"
    if lang in ("cpp", "c"):
        if x.startswith(("#include", "using ")):
            return x
        x = x.strip("<>\"")
        return f"#include \"{x}\"" if x.endswith((".h", ".hpp")) and "/" in x or x.startswith(".") else f"#include <{x}>"
    if lang == "csharp":
        x = re.sub(r"^using\s+", "", x).rstrip(";").strip()
        return f"using {x};"
    if lang == "javascript":
        return x if "require" in x or x.startswith("import ") else None
    return None


def project_imports(L, loc, code):
    """Imports of the project's own modules that `code` names (other files of the target)."""
    lang, out = L.language, []
    if lang not in ("python3", "rust", "cpp"):
        return out
    text = re.sub(r'"(?:\\.|[^"\\])*"' + r"|'(?:\\.|[^'\\])*'", '""', code)
    seen = set()
    for fid, l in L.loc.items():
        if l["file"] == loc["file"] or l["role"] not in ("class", "function"):
            continue
        name = l["name"] if l["role"] == "class" else l["member"]
        if not name or (name, l["file"]) in seen or not re.search(rf"(?<![\w.]){re.escape(name)}\b", text):
            continue
        if lang == "python3" and L.frag[fid].get("parent") and L.frag[L.frag[fid]["parent"]]["kind"] == "class":
            continue  # a nested class is reached through its outer class
        seen.add((name, l["file"]))
        if lang == "python3":
            out.append(f"from {l['call_module']} import {name}")
        elif lang == "rust":
            out.append(f"use crate::{l['call_module']}::*;")
        else:
            out.append(f"#include \"{l['file'].rsplit('/', 1)[-1]}\"")
    return out


# ------------------------------------------------------------------------------------------------ declarations
def unwrap(code, name, lang):
    """Members a model wrapped in a declaration of their own class (`class Name { ... }`, `impl Name { ... }`,
    Python `class Name:` + an indented body): the members and the declaration it used; else unchanged."""
    if lang == "python3":
        m = re.match(r"\s*(class\s+" + re.escape(name) + r"\b[^\n:]*):[ \t]*\n", code)
        if not m:
            return code, None
        body = code[m.end():].split("\n")
        if any(l.strip() and not l.startswith((" ", "\t")) for l in body):
            return code, None
        ind = min((len(l) - len(l.lstrip()) for l in body if l.strip()), default=0)
        return "\n".join(l[ind:] for l in body).strip("\n"), m.group(1)
    m = re.match(r"\s*((?:#\[[^\]]*\]\s*)*(?:@\w+\s*)*(?:(?:pub|public|protected|private|internal|abstract|final|"
                 r"static|sealed|partial|export)\s+)*(?:class|struct|impl|interface|record)\s+" + re.escape(name)
                 + r"\b[^{;]*)\{", code)
    if not m or not code.rstrip().rstrip(";").endswith("}"):
        return code, None
    end = code.rstrip().rstrip(";").rstrip()
    body = code[m.end():len(end) - 1]
    lines = body.strip("\n").split("\n")
    ind = min((len(l) - len(l.lstrip()) for l in lines if l.strip()), default=0)
    decl = m.group(1).strip()
    return "\n".join(l[ind:] for l in lines), (None if decl.startswith("impl") else decl)


def _header(text, lang):
    """Only the declaration itself: a model may write `class X:\\n    pass` or `struct X { ... }` as the
    declaration. Keeps what precedes the keyword (attributes, annotations, modifiers), cuts at the body."""
    m = DECL.search(text)
    if not m:
        return text.split("\n")[0].strip().rstrip("{").rstrip(":").strip()
    if lang == "python3":
        depth, end = 0, len(text)
        for i in range(m.end(), len(text)):
            c = text[i]
            depth += c in "([" and 1 or c in ")]" and -1 or 0
            if (c == ":" and depth == 0) or (c == "\n" and depth == 0):
                end = i
                break
    else:
        end = text.find("{", m.end())
        end = len(text) if end < 0 else end
        if lang == "go":  # `type X struct` ends at its line
            nl = text.find("\n", m.end())
            end = min(end, nl) if nl >= 0 else end
    return text[:end].strip()


def declaration(L, text, name):
    lang = L.language
    d = _header((text or "").strip(), lang) or DEFAULT_DECL[lang].format(n=name)
    m = DECL.search(d)
    if not m:
        d = DEFAULT_DECL[lang].format(n=name) + (d if lang in ("python3",) and d.startswith("(") else "")
        m = DECL.search(d)
    d = d[:m.start(2)] + name + d[m.end(2):]
    kind = m.group(1)
    if lang == "rust" and kind in ("struct", "enum", "trait") and not re.search(r"\bpub\b", d[:m.start()]):
        d = d[:m.start()] + "pub " + d[m.start():]
    if lang == "rust" and "derive(" not in d and kind in ("struct", "enum"):
        d = "#[derive(Debug, Clone, PartialEq)]\n" + d
    return d, kind


def class_code(L, fid, decl, kind, fields, name):
    lang, slot = L.language, slot_line(L.language, fid)
    if lang == "python3":
        return f"{decl}:\n{indent(fields or 'pass')}\n    {slot}"
    if lang == "cpp":
        return f"{decl} {{\npublic:\n" + (indent(fields) + "\n" if fields else "") + f"    {slot}\n}};"
    if lang == "csharp":
        return f"{decl}\n{{\n" + (indent(fields) + "\n\n" if fields else "") + f"    {slot}\n}}"
    if lang == "javascript":
        return f"{decl} {{\n" + (indent(fields) + "\n\n" if fields else "") + f"    {slot}\n}}"
    if lang == "rust":
        if kind == "trait":
            return f"{decl} {{\n" + (indent(fields) + "\n" if fields else "") + f"    {slot}\n}}"
        body = f"{decl} {{\n" + (indent(fields) + "\n" if fields else "") + "}"
        return body + f"\n\nimpl {name} {{\n    {slot}\n}}"
    if lang == "go":
        return f"{decl} {{\n" + (indent(fields) + "\n" if fields else "") + "}"
    if lang == "c":
        return f"{decl} {{\n" + (indent(fields) + "\n" if fields else "    char _unused;\n") + f"}} {name};"
    if lang == "matlab":
        return f"{decl}\n    properties\n{indent(fields, 8)}\n    end\n    methods\n        {slot}\n    end\nend"
    raise ValueError(lang)


# ------------------------------------------------------------------------------------------------ pieces
def scaffolds(L, f, loc):
    lang, out = L.language, []
    if lang == "rust":
        out.append({"target_file": "Cargo.toml", "code": f"[package]\nname = \"{L.crate}\"\nversion = \"0.1.0\"\n"
                                                         f"edition = \"2021\"\n\n[lib]\npath = \"src/lib.rs\"\n\n"
                                                         f"[dependencies]"})
        out.append({"target_file": "src/lib.rs", "code": f"pub mod {loc['call_module']};"
                    if loc.get("call_module") else ""})
    elif lang == "go":
        out.append({"target_file": "go.mod", "code": f"module {L.package}\n\ngo 1.21"})
    elif lang == "python3":
        parts = loc["file"].split("/")[:-1]
        for k in range(1, len(parts) + 1):
            out.append({"target_file": "/".join(parts[:k]) + "/__init__.py", "code": ""})
    elif lang == "csharp":
        out.append({"target_file": loc["file"],
                    "code": f"namespace {L.namespace}\n{{\n    {slot_line(lang, 'ns:' + L.namespace)}\n}}"})
    return [{**p, "fragment": f["id"], "scaffold": True} for p in out]


def pieces(L, f, out):
    """Pieces for fragment `f` from an agent's output {"imports", "code", "declaration"}, in the protocol's normal
    form (every field present)."""
    return [Piece(**p).model_dump() for p in _pieces(L, f, out)]


def _pieces(L, f, out):
    fid, lang, loc = f["id"], L.language, L.location(f["id"])
    # member code comes at column 0 whatever the model indented it to (the slot indents it; Python needs it so)
    code, moved = split_code(textwrap.dedent((out.get("code") or "").replace("\r\n", "\n")).strip("\n"), lang)
    if loc["role"] in ("method", "static", "property", "constructor") and loc.get("slot"):
        code, _ = unwrap(code, loc["name"], lang)
    wrapped_decl = None
    if loc["role"] == "class":
        code, wrapped_decl = unwrap(code, loc["name"], lang)
    imports = [x for x in (import_line(lang, i) for i in list(out.get("imports") or []) + moved) if x]
    head = {"go": [f"package {L.package}"], "cpp": ["#pragma once"]}.get(lang, [])
    pre_lines = head + [i for i in dict.fromkeys(imports + project_imports(L, loc, code))]
    res = []
    file = loc["file"]
    if pre_lines:
        res.append({"fragment": fid, "target_file": file, "section": "preamble", "code": "\n".join(pre_lines)})
    res += scaffolds(L, f, loc)
    placeholder = comment(lang, f"{fid}: nothing to translate")
    if loc["role"] == "class":
        decl, kind = declaration(L, out.get("declaration") or wrapped_decl, loc["name"])
        piece = {"fragment": fid, "target_file": file, "code": class_code(L, fid, decl, kind, code.strip("\n"),
                                                                           loc["name"])}
        if loc.get("slot"):
            piece["slot"] = loc["slot"]
        return res + [piece]
    if loc.get("container"):  # C#: module code and functions in the module's static class
        res.append({"fragment": fid, "target_file": file, "scaffold": True, "slot": "ns:" + L.namespace,
                    "code": f"public static class {loc['class']}\n{{\n    {slot_line(lang, loc['slot'])}\n}}"})
        return res + [{"fragment": fid, "target_file": file, "slot": loc["slot"], "code": code or placeholder}]
    piece = {"fragment": fid, "target_file": file, "code": code or placeholder}
    if loc.get("slot"):
        piece["slot"] = loc["slot"]
    return res + [piece]
