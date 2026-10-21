"""Generated test drivers for C and C++ (see sandbox/drivers.py for the idea and the Rust driver).

C++: arguments are rendered as typed literals of the parsed parameter types; the call runs in a lambda (so void and
non-void results are handled alike) inside try/catch; results are encoded by the overloads of
sandbox/harness/cpp/acm_enc.hpp. C: the same, with plain encoder functions (sandbox/harness/c/acm_enc.h) chosen
by the declared return type; there are no exceptions, so a crash (signal) is the only failure a C run reports.
A non-const reference or a pointer-to-scalar parameter becomes a local variable whose value afterwards is the
case's args_after (output parameters)."""
import re

from tree_sitter_language_pack import get_parser

from .drivers import Unresolved, Untestable, _b64, _generic, _txt

C_INTS = {"int", "long", "long int", "long long", "long long int", "short", "short int", "signed", "signed int",
          "unsigned", "unsigned int", "unsigned long", "unsigned long long", "unsigned short", "size_t", "ssize_t",
          "ptrdiff_t", "intptr_t", "uintptr_t", "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t", "uint16_t",
          "uint32_t", "uint64_t", "unsigned char", "signed char", "long unsigned int", "std::size_t",
          "std::int64_t", "std::int32_t", "std::uint8_t", "std::uint32_t", "std::uint64_t", "std::int8_t",
          "std::int16_t", "std::uint16_t", "char16_t", "char32_t"}
C_FLOATS = {"float", "double", "long double"}
UNSIGNED = ("unsigned", "uint", "std::uint", "size_t", "std::size_t", "uintptr", "long unsigned")


def _chain_name(node, src):
    """(name, qualifier) at the end of a declarator chain."""
    while node is not None and node.type in ("pointer_declarator", "reference_declarator", "function_declarator",
                                             "parenthesized_declarator", "attributed_declarator"):
        nxt = node.child_by_field_name("declarator")
        node = nxt if nxt is not None else (node.named_children[-1] if node.named_children else None)
    if node is None:
        return None, ""
    t = _txt(node, src)
    if "::" in t:
        q, _, n = t.rpartition("::")
        return n, q
    return t, ""


def _fn_declarator(node):
    marks = ""
    while node is not None and node.type != "function_declarator":
        if node.type == "pointer_declarator":
            marks += "*"
        elif node.type == "reference_declarator":
            marks += "&"
        nxt = node.child_by_field_name("declarator")
        node = nxt if nxt is not None else (node.named_children[-1] if node.named_children else None)
    return node, marks


def _param(p, src):
    """`const std::string& name` -> ("name", "const std::string&")."""
    ty = p.child_by_field_name("type")
    base = _txt(ty, src) if ty is not None else ""
    quals = " ".join(_txt(c, src) for c in p.children if c.type == "type_qualifier")
    d = p.child_by_field_name("declarator")
    name, marks, arr = "", "", ""
    while d is not None:
        if d.type == "pointer_declarator":
            marks += "*"
        elif d.type == "reference_declarator":
            marks += "&"
        elif d.type == "array_declarator":
            arr += "*"
        elif d.type in ("identifier", "field_identifier"):
            name = _txt(d, src)
            break
        nxt = d.child_by_field_name("declarator")
        if nxt is None:
            kids = [c for c in d.named_children if c.type in ("identifier", "pointer_declarator",
                                                               "reference_declarator", "array_declarator")]
            nxt = kids[0] if kids else None
        d = nxt
    t = re.sub(r"\s+", " ", (quals + " " + base).strip()) + marks + arr
    return name, t


def signature(text, member, cls=None, lang="cpp"):
    """{params, returns, qualifier, namespace, static, template, in_class, line} of function / method `member`
    (of class `cls`; `member == cls` finds a constructor)."""
    src = text.encode("utf-8")
    tree = get_parser(lang).parse(src)
    hits = []

    def consider(node, ns, klass, template=False):
        fd, marks = _fn_declarator(node.child_by_field_name("declarator"))
        if fd is None:
            return
        name, qual = _chain_name(fd.child_by_field_name("declarator"), src)
        owner = qual.split("::")[-1] if qual else klass
        if name != member or (cls or None) != (owner or None):
            return
        params, variadic = [], False
        plist = fd.child_by_field_name("parameters")
        for p in plist.named_children if plist is not None else []:
            if p.type in ("parameter_declaration", "optional_parameter_declaration"):
                params.append(_param(p, src))
            elif p.type == "variadic_parameter":
                variadic = True
        params = [p for p in params if p[1] != "void"]
        rt = node.child_by_field_name("type")
        quals = " ".join(_txt(c, src) for c in node.children if c.type == "type_qualifier")
        storage = " ".join(_txt(c, src) for c in node.children if c.type == "storage_class_specifier")
        hits.append({"params": params, "returns": (quals + " " + (_txt(rt, src) if rt is not None else "")).strip()
                     + marks, "template": template, "namespace": "::".join(ns),
                     "qualifier": "::".join(ns + ([owner] if owner else [])), "static": "static" in storage,
                     "variadic": variadic, "definition": node.type == "function_definition",
                     "in_class": owner is not None, "line": node.start_point[0] + 1})

    def walk(node, ns, klass):
        for ch in node.children:
            t = ch.type
            if t == "namespace_definition":
                nm = ch.child_by_field_name("name")
                body = ch.child_by_field_name("body")
                if body is not None:
                    walk(body, ns + ([_txt(nm, src)] if nm is not None else []), klass)
            elif t in ("class_specifier", "struct_specifier") and ch.child_by_field_name("body") is not None:
                nm = ch.child_by_field_name("name")
                walk(ch.child_by_field_name("body"), ns, _txt(nm, src) if nm is not None else klass)
            elif t in ("linkage_specification", "declaration_list", "field_declaration_list", "preproc_ifdef",
                       "preproc_if", "preproc_else", "preproc_elif", "access_specifier"):
                body = ch.child_by_field_name("body")
                walk(body if body is not None else ch, ns, klass)
            elif t == "template_declaration":
                for c in ch.named_children:
                    if c.type in ("function_definition", "declaration", "field_declaration"):
                        consider(c, ns, klass, template=True)
                    elif c.type in ("class_specifier", "struct_specifier"):
                        walk(c, ns, klass)
            elif t in ("function_definition", "declaration", "field_declaration"):
                consider(ch, ns, klass)

    walk(tree.root_node, [], None)
    if not hits:
        raise Unresolved(f"no function {member}" + (f" in {cls}" if cls else ""))
    hits.sort(key=lambda h: not h["definition"])
    return hits[0]


def _base(t):
    t = re.sub(r"\b(const|volatile|struct|class|typename|enum)\b", "", t)
    return re.sub(r"\s+", " ", t.replace("&", "")).strip()


def c_str(s):
    """A C string literal for the UTF-8 bytes of `s` (octal escapes: safe before any following character)."""
    out = []
    for b in (s.encode("utf-8") if isinstance(s, str) else s):
        c = chr(b)
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif 0x20 <= b < 0x7f:
            out.append(c)
        else:
            out.append("\\%03o" % b)
    return '"' + "".join(out) + '"'


def _int_lit(v, b):
    if v["t"] != "int":
        raise Untestable(f"{v['t']} for {b}")
    n = int(v["v"])
    if b.startswith(UNSIGNED) and n < 0:
        raise Untestable(f"negative value for {b}")
    return f"(({b}){n}{'ULL' if n > 2 ** 63 - 1 else 'LL'})"


def _float_lit(v, b):
    if v["t"] not in ("float", "int"):
        raise Untestable(f"{v['t']} for {b}")
    x = v["v"]
    if x in ("nan", "inf", "-inf"):
        return {"nan": "NAN", "inf": "INFINITY", "-inf": "(-INFINITY)"}[x]
    return f"(({b}){float(x)!r})"


def _char_lit(v):
    if v["t"] not in ("text", "str") or len(v["v"].encode("utf-8")) != 1:
        raise Untestable("char needs a 1-byte string")
    return "'" + c_str(v["v"])[1:-1].replace("'", "\\'") + "'"


def cpp_lit(v, t):
    """A C++ expression of value type `t` for the tagged value `v`."""
    b = _base(t)
    tag = v["t"]
    if b in C_INTS:
        return _int_lit(v, b)
    if b in C_FLOATS:
        return _float_lit(v, b)
    if b == "bool":
        if tag != "bool":
            raise Untestable(f"{tag} for bool")
        return "true" if v["v"] else "false"
    if b == "char":
        return _char_lit(v)
    if b in ("std::string", "string", "std::string_view", "string_view"):
        if tag in ("text", "str"):
            return f"std::string({c_str(v['v'])}, {len(v['v'].encode('utf-8'))})"
        if tag == "bytes":
            raw = _b64(v["v"])
            return f"std::string({c_str(raw)}, {len(raw)})"
        raise Untestable(f"{tag} for {b}")
    if tag == "none":
        if b.startswith(("std::optional", "optional")):
            return "std::nullopt"
        raise Untestable(f"None for {b}")
    head, args = _generic(b)
    head = head.replace("std::", "")
    if head == "optional" and args:
        return f"{b}{{{cpp_lit(v, args[0])}}}"
    if head in ("vector", "deque", "list") and args:
        if _base(args[0]) in ("uint8_t", "unsigned char", "std::uint8_t", "char") and tag == "bytes":
            return f"{b}{{" + ", ".join(str(x) for x in _b64(v["v"])) + "}"
        if tag not in ("list", "tuple", "set"):
            raise Untestable(f"{tag} for {b}")
        return f"{b}{{" + ", ".join(cpp_lit(x, args[0]) for x in v["v"]) + "}"
    if head in ("set", "unordered_set") and args:
        if tag not in ("list", "set"):
            raise Untestable(f"{tag} for {b}")
        return f"{b}{{" + ", ".join(cpp_lit(x, args[0]) for x in v["v"]) + "}"
    if head in ("map", "unordered_map") and len(args) >= 2:
        if tag != "dict":
            raise Untestable(f"{tag} for {b}")
        return f"{b}{{" + ", ".join(f"{{{cpp_lit(k, args[0])}, {cpp_lit(x, args[1])}}}" for k, x in v["v"]) + "}"
    if head in ("pair", "tuple") and args:
        if tag not in ("list", "tuple") or len(v["v"]) != len(args):
            raise Untestable(f"{tag} for {b}")
        return f"{b}{{" + ", ".join(cpp_lit(x, a) for x, a in zip(v["v"], args)) + "}"
    if head == "array" and len(args) == 2:
        if tag not in ("list", "tuple"):
            raise Untestable(f"{tag} for {b}")
        return f"{b}{{" + ", ".join(cpp_lit(x, args[0]) for x in v["v"]) + "}"
    raise Untestable(f"cannot build a {b} from a test value")


def c_lit(v, t):
    b = _base(t)
    if b in C_INTS:
        return _int_lit(v, b)
    if b in C_FLOATS:
        return _float_lit(v, b)
    if b in ("bool", "_Bool"):
        if v["t"] != "bool":
            raise Untestable(f"{v['t']} for bool")
        return "1" if v["v"] else "0"
    if b == "char":
        return _char_lit(v)
    raise Untestable(f"cannot build a {b} from a test value")


SCALARS = C_INTS | C_FLOATS | {"bool", "_Bool", "char"}


def _arg(k, v, t, lines, after, prefix, c_mode):
    """Declare argument k; return the expression passed. Mutable strings, references and pointers to scalars
    become locals whose final value is reported (output parameters)."""
    raw = t.replace(" ", "")
    b = _base(t)
    if raw in ("char*", "unsignedchar*", "signedchar*"):
        if v["t"] == "none":
            after.append(("const char*", "NULL" if c_mode else "(const char*)nullptr"))
            return "NULL" if c_mode else "nullptr"
        if v["t"] not in ("text", "str"):
            raise Untestable(f"{v['t']} for {t}")
        size = max(len(v["v"].encode("utf-8")) + 1, 256)
        lines.append(f"    char {prefix}{k}[{size}] = {c_str(v['v'])};")
        after.append(("char*", f"{prefix}{k}"))
        return f"{prefix}{k}" if raw == "char*" else f"({raw[:-1].replace('char', ' char')} *){prefix}{k}"
    if raw in ("constchar*", "charconst*", "constunsignedchar*", "unsignedcharconst*", "constsignedchar*"):
        if v["t"] == "none":
            after.append(("const char*", "NULL" if c_mode else "(const char*)nullptr"))
            return "NULL" if c_mode else "nullptr"
        if v["t"] not in ("text", "str"):
            raise Untestable(f"{v['t']} for {t}")
        cast = "" if "unsigned" not in raw and "signed" not in raw else \
            f"(const {'unsigned' if 'unsigned' in raw else 'signed'} char *)"
        after.append(("const char*", c_str(v["v"])))  # unchanged, but every argument has an entry (aligned)
        return cast + c_str(v["v"])
    if raw.endswith("*") and raw.count("*") == 1 and _base(t.replace("*", "")) in SCALARS:
        inner = _base(t.replace("*", ""))
        lit = c_lit(v, inner) if c_mode else cpp_lit(v, inner)
        lines.append(f"    {inner} {prefix}{k} = {lit};")
        after.append((inner, f"{prefix}{k}"))
        return f"&{prefix}{k}"
    if raw.endswith("*"):
        raise Untestable(f"pointer parameter {t}")
    lit = c_lit(v, b) if c_mode else cpp_lit(v, b)
    lines.append(f"    {b} {prefix}{k} = {lit};")
    after.append((b, f"{prefix}{k}"))
    return f"{prefix}{k}"


def cpp_driver(cases, sigs, includes):
    head = ["#include \"acm_enc.hpp\"", "#include <cstdlib>", "#include <cmath>"] + \
        [f"#include \"{h}\"" for h in includes] + [""]
    bodies, spans, skipped = [], {}, {}
    for i, c in enumerate(cases):
        call = c["call"]
        try:
            sig = sigs[i]
            if isinstance(sig, Exception):
                raise sig
            if sig.get("template"):
                raise Untestable(f"template function {call['member']}")
            args_in = call.get("args", [])
            if len(args_in) != len(sig["params"]):
                raise Untestable(f"{len(args_in)} arguments for {len(sig['params'])} parameters")
            lines, after, passed = [f"static void case_{i}() {{"], [], []
            for k, (v, (_, pt)) in enumerate(zip(args_in, sig["params"])):
                passed.append(_arg(k, v, pt, lines, after, "a", False))
            selfx = '""'
            if call["kind"] in ("method", "property"):
                csig = sigs.get(("ctor", i))
                if isinstance(csig, Exception):
                    raise csig
                cargs = []
                for k, (v, (_, pt)) in enumerate(zip(call.get("init", []), csig["params"])):
                    cargs.append(_arg(k, v, pt, lines, [], "c", False))
                if len(cargs) != len(csig["params"]):
                    raise Untestable("constructor arity")
                cls_q = csig["qualifier"] or call["class"]
                lines.append(f"    {cls_q} obj{{{', '.join(cargs)}}};" if cargs else f"    {cls_q} obj;")
                target = f"obj.{call['member']}({', '.join(passed)})"
                selfx = "acm::enc(obj)"
            elif call["kind"] == "constructor":
                target = f"{sig['qualifier'] or call['class']}{{{', '.join(passed)}}}"
            else:
                q = sig["qualifier"] + "::" if sig["qualifier"] else ""
                target = f"{q}{call['member']}({', '.join(passed)})"
            aft = "{" + ", ".join(f"acm::enc({x})" for _, x in after) + "}"
            lines += [
                "    std::cout << \"__ACMOUT_BEGIN__\" << std::endl;",
                "    try {",
                f"        acm::run([&]() -> decltype(auto) {{ return {target}; }},",
                f"                 [&]() {{ return std::vector<std::string>{aft}; }},",
                f"                 [&]() {{ return std::string({selfx}); }});",
                "    } catch (const std::exception& e) {",
                f"        acm::emit(\"exception\", \"\", acm::exc(acm::demangle(typeid(e).name()), e.what()), {aft}, {selfx});",
                "    } catch (...) {",
                f"        acm::emit(\"exception\", \"\", acm::exc(\"unknown\", \"\"), {aft}, {selfx});",
                "    }", "}", ""]
            start = len(head) + sum(len(b) for b in bodies) + 1
            spans[i] = (start, start + len(lines) - 1)
            bodies.append(lines)
        except (Untestable, Unresolved) as e:
            skipped[i] = e
    main = ["int main() {", "    int i = std::atoi(std::getenv(\"ACM_CASE\"));", "    switch (i) {"] + \
        [f"        case {i}: case_{i}(); break;" for i in sorted(spans)] + \
        ["        default: break;", "    }", "    return 0;", "}"]
    return "\n".join(head + [x for b in bodies for x in b] + main) + "\n", spans, skipped


def _c_encoder(t, expr):
    """A C statement printing the tagged value of `expr` of type `t`, or None."""
    raw = t.replace(" ", "")
    b = _base(t)
    if raw in ("constchar*", "char*", "charconst*"):
        return f"acm_enc_str({expr})"
    if raw.endswith("*"):
        return None
    if b in ("bool", "_Bool"):
        return f"acm_enc_bool({expr})"
    if b == "char":
        return f"acm_enc_char({expr})"
    if b in C_INTS:
        return f"acm_enc_{'u' if b.startswith(UNSIGNED) else ''}int({expr})"
    if b in C_FLOATS:
        return f"acm_enc_float((double)({expr}))"
    return None


def c_driver(cases, sigs, includes):
    head = ["#include \"acm_enc.h\"", "#include <stdlib.h>", "#include <math.h>"] + \
        [f"#include \"{h}\"" for h in includes] + [""]
    bodies, spans, skipped = [], {}, {}
    for i, c in enumerate(cases):
        call = c["call"]
        try:
            sig = sigs[i]
            if isinstance(sig, Exception):
                raise sig
            args_in = call.get("args", [])
            if len(args_in) != len(sig["params"]):
                raise Untestable(f"{len(args_in)} arguments for {len(sig['params'])} parameters")
            lines, after, passed = [f"static void case_{i}(void) {{"], [], []
            for k, (v, (_, pt)) in enumerate(zip(args_in, sig["params"])):
                passed.append(_arg(k, v, pt, lines, after, "a", True))
            rt = sig["returns"]
            target = f"{call['member']}({', '.join(passed)})"
            lines.append("    acm_begin();")
            if _base(rt) == "void" and "*" not in rt:
                lines += [f"    {target};", "    acm_result_begin(); acm_enc_none();"]
            else:
                enc = _c_encoder(rt, "acm_v")
                lines += [f"    {rt} acm_v = {target};", "    acm_result_begin();",
                          f"    {enc};" if enc else f"    acm_enc_repr(\"{_base(rt)}\");"]
            lines.append("    acm_after_begin();")
            for n, (ty, x) in enumerate(after):
                e = _c_encoder(ty, x)
                lines.append(("    acm_sep();" if n else "") + (f"    {e};" if e else "    acm_enc_repr(\"?\");"))
            lines += ["    acm_end();", "}", ""]
            start = len(head) + sum(len(b) for b in bodies) + 1
            spans[i] = (start, start + len(lines) - 1)
            bodies.append(lines)
        except (Untestable, Unresolved) as e:
            skipped[i] = e
    main = ["int main(void) {", "    int i = atoi(getenv(\"ACM_CASE\"));", "    switch (i) {"] + \
        [f"        case {i}: case_{i}(); break;" for i in sorted(spans)] + \
        ["        default: break;", "    }", "    return 0;", "}"]
    return "\n".join(head + [x for b in bodies for x in b] + main) + "\n", spans, skipped
