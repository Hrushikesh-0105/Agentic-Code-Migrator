"""Generated test drivers for languages without runtime reflection (Rust, C++, C).

The function to call is found in the code with tree-sitter; each argument of a case is rendered as a literal of
the declared parameter type; the result is printed by the driver and turned back into the type-tagged format here.
An argument the renderer cannot express in the parameter's type (a user struct, a generic, a raw pointer) makes the
case "untestable" (reported, not a failure of the code under test); a function that is not there is "unresolved".
"""
import base64
import json
import math
import re

from pathlib import Path

from tree_sitter_language_pack import get_parser

HARNESS_DIR = Path(__file__).resolve().parent / "harness"


class Untestable(Exception):
    pass


class Unresolved(Exception):
    pass


def _txt(node, src):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _b64(v):
    return base64.b64decode(v)


def _ints(v):
    if v["t"] == "bytes":
        return list(_b64(v["v"]))
    raise Untestable("expected bytes")


# ================================================================================================ Rust
def rust_signature(text, member, cls=None):
    """{params: [(name, type)], returns, receiver: None | "&self" | "&mut self" | "self", line} of fn `member`
    (inside `impl cls` when cls is given)."""
    src = text.encode("utf-8")
    tree = get_parser("rust").parse(src)
    found = []

    def walk(node, impl_type):
        for ch in node.children:
            if ch.type == "impl_item":
                t = ch.child_by_field_name("type")
                walk(ch.child_by_field_name("body") or ch, _txt(t, src).split("<")[0].strip() if t else None)
            elif ch.type == "mod_item" and ch.child_by_field_name("body") is not None:
                walk(ch.child_by_field_name("body"), impl_type)
            elif ch.type == "function_item":
                name = ch.child_by_field_name("name")
                if name is not None and _txt(name, src) == member and impl_type == cls:
                    found.append(ch)
            elif ch.type == "declaration_list":
                walk(ch, impl_type)
    walk(tree.root_node, None)
    if not found:
        raise Unresolved(f"no fn {member}" + (f" in impl {cls}" if cls else ""))
    fn = found[0]
    params, receiver = [], None
    for p in fn.child_by_field_name("parameters").named_children:
        if p.type == "self_parameter":
            receiver = re.sub(r"\s+", " ", _txt(p, src)).replace("mut self", "mut self")
        elif p.type == "parameter":
            params.append((_txt(p.child_by_field_name("pattern"), src), _txt(p.child_by_field_name("type"), src)))
    rt = fn.child_by_field_name("return_type")
    tps = fn.child_by_field_name("type_parameters")
    return {"params": params, "returns": _txt(rt, src) if rt else "()", "receiver": receiver,
            "generic": _txt(tps, src) if tps else None, "line": fn.start_point[0] + 1}


def _split_args(t):
    """`A, B<C, D>, E` -> [A, B<C, D>, E] (top-level commas only)."""
    out, depth, cur = [], 0, ""
    for ch in t:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _generic(t):
    """`HashMap<String, i64>` -> ("HashMap", ["String", "i64"])."""
    t = t.strip()
    m = re.match(r"^([\w:]+)\s*<(.*)>$", t, re.S)
    if not m:
        return t, []
    return m.group(1).split("::")[-1], _split_args(m.group(2))


RUST_INTS = {"i8", "i16", "i32", "i64", "i128", "isize", "u8", "u16", "u32", "u64", "u128", "usize"}


def rust_str(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o > 0x7e:
            out.append("\\u{%x}" % o)
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def rust_owned(t):
    t = t.strip()
    if t.startswith("&"):
        t = re.sub(r"^&\s*('\w+\s+)?(mut\s+)?", "", t)
    if t == "str":
        return "String"
    m = re.match(r"^\[(.*)\]$", t)
    if m and ";" not in m.group(1):
        return f"Vec<{m.group(1).strip()}>"
    return t


def rust_lit(v, t):
    """A Rust expression of type `t` (owned) for the tagged value `v`."""
    t = t.strip()
    tag = v["t"]
    if t.startswith("&"):
        raise Untestable("reference inside a value")
    if t in RUST_INTS:
        if tag != "int" and not (tag == "bool"):
            raise Untestable(f"{tag} for {t}")
        n = int(v["v"]) if tag == "int" else int(v["v"])
        if t.startswith("u") and n < 0:
            raise Untestable(f"negative value for {t}")
        return f"{n}{t}"
    if t in ("f32", "f64"):
        if tag not in ("float", "int"):
            raise Untestable(f"{tag} for {t}")
        x = float(v["v"]) if not isinstance(v["v"], str) or v["v"] not in ("nan", "inf", "-inf") else v["v"]
        if x == "nan" or (isinstance(x, float) and math.isnan(x)):
            return f"{t}::NAN"
        if x in ("inf", float("inf")):
            return f"{t}::INFINITY"
        if x in ("-inf", float("-inf")):
            return f"{t}::NEG_INFINITY"
        return f"{float(x)!r}{t}"
    if t == "bool":
        if tag != "bool":
            raise Untestable(f"{tag} for bool")
        return "true" if v["v"] else "false"
    if t == "char":
        if tag not in ("text", "str") or len(v["v"]) != 1:
            raise Untestable("char needs a 1-character string")
        return "'" + rust_str(v["v"])[1:-1].replace("'", "\\'") + "'"
    if t in ("String", "str", "&str", "Box<str>"):
        if tag in ("text", "str"):
            return f"String::from({rust_str(v['v'])})"
        raise Untestable(f"{tag} for {t}")
    if tag == "none":
        if t.startswith("Option<"):
            return "None"
        raise Untestable(f"None for {t}")
    head, args = _generic(t)
    if head == "Option":
        return f"Some({rust_lit(v, args[0])})"
    if head == "Box":
        return f"Box::new({rust_lit(v, args[0])})"
    if head in ("Vec", "VecDeque"):
        if args and args[0] == "u8" and tag == "bytes":
            return "vec![" + ", ".join(f"{b}u8" for b in _b64(v["v"])) + "]" + (".into()" if head != "Vec" else "")
        if tag not in ("list", "tuple", "set"):
            raise Untestable(f"{tag} for {t}")
        items = ", ".join(rust_lit(x, args[0]) for x in v["v"])
        return (f"vec![{items}]" if items else f"Vec::<{args[0]}>::new()") + (".into()" if head != "Vec" else "")
    m = re.match(r"^\[(.+);\s*(\d+)\]$", t)
    if m:
        if tag == "bytes" and m.group(1).strip() == "u8":
            return "[" + ", ".join(f"{b}u8" for b in _b64(v["v"])) + "]"
        if tag not in ("list", "tuple"):
            raise Untestable(f"{tag} for {t}")
        return "[" + ", ".join(rust_lit(x, m.group(1)) for x in v["v"]) + "]"
    if head in ("HashMap", "BTreeMap"):
        if tag != "dict":
            raise Untestable(f"{tag} for {t}")
        pairs = ", ".join(f"({rust_lit(k, args[0])}, {rust_lit(x, args[1])})" for k, x in v["v"])
        return f"std::collections::{head}::<{args[0]}, {args[1]}>::from([{pairs}])"
    if head in ("HashSet", "BTreeSet"):
        if tag not in ("set", "list"):
            raise Untestable(f"{tag} for {t}")
        return f"std::collections::{head}::<{args[0]}>::from([{', '.join(rust_lit(x, args[0]) for x in v['v'])}])"
    if t.startswith("(") and t.endswith(")"):
        parts = _split_args(t[1:-1])
        if tag not in ("list", "tuple") or len(parts) != len(v["v"]):
            raise Untestable(f"{tag} for {t}")
        return "(" + ", ".join(rust_lit(x, p) for x, p in zip(v["v"], parts)) + ("," if len(parts) == 1 else "") + ")"
    raise Untestable(f"cannot build a {t} from a test value")


def rust_driver(cases, sigs, crate):
    """The integration test tests/acm_driver.rs: one fn per case; `acm_case` runs the one ACM_CASE selects.
    Returns (source, {case index: (first line, last line)}, {case index: untestable reason})."""
    head = (HARNESS_DIR / "rust" / "prelude.rs").read_text(encoding="utf-8")
    lines = head.split("\n")
    spans, skipped, bodies = {}, {}, []
    for i, c in enumerate(cases):
        call = c["call"]
        try:
            sig = sigs[i]
            if isinstance(sig, Exception):
                raise sig
            if sig.get("generic"):
                raise Untestable(f"generic function {call['member']}{sig['generic']}")
            path = "::".join([crate] + [p for p in (call.get("module") or "").split("::") if p])
            body = [f"fn case_{i}() {{"]
            args, after = [], []
            if len(call.get("args", [])) != len(sig["params"]):
                raise Untestable(f"{len(call.get('args', []))} arguments for {len(sig['params'])} parameters")
            for k, (v, (_, pt)) in enumerate(zip(call.get("args", []), sig["params"])):
                owned = rust_owned(pt)
                body.append(f"    let mut a{k}: {owned} = {rust_lit(v, owned)};")
                after.append(f"acm_dbg!(a{k})")
                ref = pt.strip()
                args.append(f"&mut a{k}" if ref.startswith("&mut") or re.match(r"^&'\w+\s+mut", ref) else
                            f"&a{k}" if ref.startswith("&") else f"a{k}.clone()")
            selfv = "None"
            if call["kind"] in ("method", "property"):
                ctor, csig = call.get("constructor") or "new", sigs.get(("ctor", i))
                if isinstance(csig, Exception):
                    raise csig
                cargs = []
                for k, (v, (_, pt)) in enumerate(zip(call.get("init", []), csig["params"])):
                    owned = rust_owned(pt)
                    body.append(f"    let mut c{k}: {owned} = {rust_lit(v, owned)};")
                    cargs.append(f"&mut c{k}" if pt.strip().startswith("&mut") else f"&c{k}" if pt.strip().startswith("&")
                                 else f"c{k}.clone()")
                if len(cargs) != len(csig["params"]):
                    raise Untestable("constructor arity")
                made = f"{path}::{call['class']}::{ctor}({', '.join(cargs)})"
                if csig["returns"].strip().startswith("Result"):
                    made += ".expect(\"constructor failed\")"
                body.append(f"    let mut obj = {made};")
                target = f"obj.{call['member']}({', '.join(args)})"
                selfv = "Some(acm_dbg!(obj))"
            elif call["kind"] == "constructor":
                target = f"{path}::{call['class']}::{call.get('constructor') or 'new'}({', '.join(args)})"
            elif call["kind"] == "static":
                target = f"{path}::{call['class']}::{call['member']}({', '.join(args)})"
            else:
                target = f"{path}::{call['member']}({', '.join(args)})"
            body += [
                "    std::panic::set_hook(Box::new(|_| {}));",
                "    println!(\"__ACMOUT_BEGIN__\");",
                f"    let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {{ {target} }}));",
                "    let _ = std::io::stdout().flush();",
                f"    let after = vec![{', '.join(after)}];",
                "    match r {",
                f"        Ok(v) => acm_emit(\"ok\", Some(acm_dbg!(v)), None, after, {selfv}),",
                f"        Err(p) => acm_emit(\"panic\", None, Some(acm_panic(p)), after, {selfv}),",
                "    }", "}"]
            spans[i] = (len(lines) + sum(len(b) for b in bodies) + 1,
                        len(lines) + sum(len(b) for b in bodies) + len(body))
            bodies.append(body)
        except (Untestable, Unresolved) as e:
            skipped[i] = e
    dispatch = ["#[test]", "fn acm_case() {", "    let i: usize = std::env::var(\"ACM_CASE\").unwrap().parse().unwrap();",
                "    match i {"] + [f"        {i} => case_{i}()," for i in sorted(spans)] + ["        _ => {}", "    }", "}"]
    return "\n".join(lines + [x for b in bodies for x in b] + dispatch) + "\n", spans, skipped


# ------------------------------------------------------------------------------------------------ Debug text
class _P:
    def __init__(self, s):
        self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \n\t\r":
            self.i += 1

    def peek(self, k=1):
        return self.s[self.i:self.i + k]

    def value(self):
        self.ws()
        c = self.peek()
        if c == '"':
            return ("str", self.string('"'))
        if c == "'":
            return ("char", self.string("'"))
        if c == "[":
            return ("list", self.seq("[", "]"))
        if c == "(":
            items = self.seq("(", ")")
            return ("unit", None) if not items else ("tuple", items)
        if c == "{":
            return self.braces()
        if c == "<":
            j = self.s.find(">", self.i)
            j = len(self.s) - 1 if j < 0 else j
            raw = self.s[self.i:j + 1]
            self.i = j + 1
            return ("opaque", raw)
        m = re.match(r"-?(?:\d+\.\d*(?:e[+-]?\d+)?|\d+e[+-]?\d+|inf|NaN)", self.s[self.i:])
        if m and ("." in m.group(0) or "e" in m.group(0) or m.group(0) in ("inf", "-inf", "NaN")):
            self.i += m.end()
            return ("float", m.group(0))
        m = re.match(r"-?\d+", self.s[self.i:])
        if m:
            self.i += m.end()
            return ("int", m.group(0))
        m = re.match(r"[A-Za-z_][\w:]*", self.s[self.i:])
        if m:
            name = m.group(0)
            self.i += m.end()
            if name in ("true", "false"):
                return ("bool", name == "true")
            self.ws()
            if self.peek() == "(":
                return ("call", name, self.seq("(", ")"))
            if self.peek() == "{":
                kind, fields = self.braces()
                return ("struct", name, fields if kind == "map" else [])
            return ("ident", name)
        raise ValueError(f"cannot parse Debug text at {self.s[self.i:self.i + 40]!r}")

    def string(self, q):
        self.i += 1
        out = []
        while self.i < len(self.s) and self.s[self.i] != q:
            ch = self.s[self.i]
            if ch == "\\":
                nx = self.s[self.i + 1]
                if nx == "u":
                    j = self.s.index("}", self.i)
                    out.append(chr(int(self.s[self.i + 3:j], 16)))
                    self.i = j + 1
                    continue
                out.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(nx, nx))
                self.i += 2
                continue
            out.append(ch)
            self.i += 1
        self.i += 1
        return "".join(out)

    def seq(self, o, c):
        self.i += 1
        items = []
        self.ws()
        while self.peek() != c:
            items.append(self.value())
            self.ws()
            if self.peek() == ",":
                self.i += 1
                self.ws()
        self.i += 1
        return items

    def braces(self):
        """{k: v, ...} (map or struct fields) or {a, b} (set)."""
        self.i += 1
        self.ws()
        pairs, items, is_map = [], [], None
        while self.peek() != "}":
            save = self.i
            m = re.match(r"([A-Za-z_]\w*)\s*:(?!:)", self.s[self.i:])
            if m and is_map is not False:
                self.i += m.end()
                pairs.append((("str", m.group(1)), self.value()))
                is_map = True
            else:
                self.i = save
                k = self.value()
                self.ws()
                if self.peek() == ":" and self.peek(2) != "::":
                    self.i += 1
                    pairs.append((k, self.value()))
                    is_map = True
                else:
                    items.append(k)
                    is_map = False
            self.ws()
            if self.peek() == ",":
                self.i += 1
                self.ws()
        self.i += 1
        return ("map", pairs) if is_map or (not items and not pairs) else ("set", items)


def parse_debug(text):
    p = _P(text.strip())
    return p.value()


ITERATORS = ("IntoIter", "Iter", "IterMut", "Drain", "Rev", "Copied", "Cloned", "Chain", "Take", "Skip")


def _tag_rust(node, t=None):
    """Debug tree -> tagged value; `t` is the declared Rust type when known (it decides bytes vs list, set vs
    map, Result)."""
    t = (t or "").strip()
    head, targs = _generic(t) if t else ("", [])
    k = node[0]
    if head in ("Result",) and k == "call" and node[1] in ("Ok", "Err"):
        return node  # handled by the caller
    if k == "int":
        return {"t": "int", "v": node[1]}
    if k == "float":
        v = node[1]
        return {"t": "float", "v": "nan" if v == "NaN" else v if v in ("inf", "-inf") else float(v)}
    if k == "bool":
        return {"t": "bool", "v": node[1]}
    if k in ("str", "char"):
        return {"t": "text", "v": node[1]}
    if k == "unit":
        return {"t": "none"}
    if k == "list":
        inner = targs[0] if targs else (re.match(r"^\[(.+?)(;.*)?\]$", t).group(1) if re.match(r"^\[", t) else "")
        if inner.strip() == "u8" and all(x[0] == "int" for x in node[1]):
            return {"t": "bytes", "v": base64.b64encode(bytes(int(x[1]) for x in node[1])).decode()}
        return {"t": "list", "v": [_tag_rust(x, inner) for x in node[1]]}
    if k == "tuple":
        parts = _split_args(t[1:-1]) if t.startswith("(") else [None] * len(node[1])
        return {"t": "tuple", "v": [_tag_rust(x, p) for x, p in zip(node[1], parts + [None] * len(node[1]))]}
    if k == "set":
        return {"t": "set", "v": [_tag_rust(x, targs[0] if targs else None) for x in node[1]]}
    if k == "map":
        if head in ("HashSet", "BTreeSet") and not node[1]:
            return {"t": "set", "v": []}
        return {"t": "dict", "v": [[_tag_rust(a, targs[0] if targs else None), _tag_rust(b, targs[1] if len(targs) > 1 else None)]
                                   for a, b in node[1]]}
    if k == "ident":
        if node[1] == "None":
            return {"t": "none"}
        return {"t": "text", "v": node[1].split("::")[-1]} if t and t[0].isupper() else {"t": "repr", "v": node[1]}
    if k == "call":
        name, items = node[1], node[2]
        if name == "Some" and len(items) == 1:
            return _tag_rust(items[0], targs[0] if head == "Option" and targs else None)
        if name in ("Box", "Rc", "Arc", "RefCell", "Cell") and len(items) == 1:
            return _tag_rust(items[0], targs[0] if targs else None)
        if name == "RefCell" or name == "Cell":
            return _tag_rust(items[0])
        if len(items) == 1 and items[0][0] == "list" and name.split("::")[-1] in ITERATORS:
            return {"t": "list", "v": [_tag_rust(x) for x in items[0][1]]}  # vec.into_iter() etc.: its items
        return {"t": "object", "class": name.split("::")[-1],
                "v": {"t": "dict", "v": [[{"t": "text", "v": str(n)}, _tag_rust(x)] for n, x in enumerate(items)]}}
    if k == "struct":
        name, fields = node[1], node[2]
        if name == "RefCell" and fields:
            return _tag_rust(fields[0][1])
        return {"t": "object", "class": name.split("::")[-1],
                "v": {"t": "dict", "v": [[{"t": "text", "v": f[0][1]}, _tag_rust(f[1])] for f in fields]}}
    return {"t": "repr", "v": str(node[1])}


def rust_result(rec, sig):
    """A driver record (debug text) -> a runner.py-style result; `sig` gives the declared types."""
    sig_returns = sig["returns"]
    out = {"status": "ok", "args_after": [], "self_after": None}
    try:
        types = [rust_owned(t) for _, t in sig.get("params", [])]
        out["args_after"] = [_tag_rust(parse_debug(a), types[k] if k < len(types) else None)
                             for k, a in enumerate(rec.get("args_after", []))]
        if rec.get("self_after") is not None:
            out["self_after"] = _tag_rust(parse_debug(rec["self_after"]))
    except (ValueError, IndexError) as e:
        out["args_after"], out["parse_note"] = [], str(e)
    if rec["status"] == "panic":
        out["status"] = "exception"
        out["exception"] = {"type": "panic", "hierarchy": ["panic"], "message": rec.get("message", "")}
        return out
    try:
        node = parse_debug(rec.get("debug", "()"))
    except (ValueError, IndexError) as e:
        return {**out, "value": {"t": "repr", "v": rec.get("debug", "")}, "parse_note": str(e)}
    head, targs = _generic(sig_returns)
    if head == "Result" and node[0] == "call" and node[1] in ("Ok", "Err"):
        inner = node[2][0] if node[2] else ("unit", None)
        if node[1] == "Ok":
            out["value"] = _tag_rust(inner, targs[0] if targs else None)
        else:
            et = inner[1] if inner[0] in ("call", "struct", "ident") else "Err"
            msg = inner[1] if inner[0] == "str" else rec.get("debug", "")
            out["status"] = "exception"
            out["exception"] = {"type": et.split("::")[-1], "hierarchy": [et.split("::")[-1], "Err"], "message": msg}
        return out
    out["value"] = _tag_rust(node, sig_returns)
    return out
