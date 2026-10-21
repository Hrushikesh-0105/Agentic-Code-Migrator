# Runs INSIDE acm-sandbox/analysis. Layer 0 for sources that are not Python (Java, C#, C, C++, JavaScript, Matlab;
# any language of langs/ with a tree-sitter grammar and an entry in SPECS): the same four steps and the same bundle
# as the Python pipeline (build.py), from tree-sitter parse trees instead of LibCST + pyan3 + JARVIS.
#
#   fragments   function | method | class (its shell: header, fields, nested types, with methods split out) |
#               module_header (imports / includes / types / constants / prototypes) | module_init (top-level code
#               that uses the project's own definitions, e.g. `module.exports = {...}`, a function table)
#               Overloads (same name) get ids with a `~N` suffix. Out-of-line C++ methods (`Cls::m`) belong to Cls.
#   edges       call / reference / constructor (name resolution against the project's definitions, preferring the
#               same class, then the same module, then a unique project-wide match), inherit, member, module,
#               init (Java / C# / C++ targets: a class and its constructors form one step)
#   order       graph.migration_order (strongly connected components, bottom-up, waves)
#   metadata    signatures (parameters with types, return type), branch conditions, per-language hazard patterns,
#               lizard metrics, external APIs (imports / includes and calls the project does not define)
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import tree_sitter_language_pack as tslp

from graph import migration_order

SPECS = {
    "java": {
        "class": {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration",
                  "annotation_type_declaration"},
        "body": {"class_body", "interface_body", "enum_body", "enum_body_declarations", "record_body",
                 "annotation_type_body"},
        "method": {"method_declaration", "constructor_declaration", "compact_constructor_declaration"},
        "function": set(), "container": set(),
        "header": {"package_declaration", "import_declaration"},
        "call": {"method_invocation": "name", "object_creation_expression": "type"},
        "branch": {"if_statement": "condition", "while_statement": "condition", "for_statement": "condition",
                   "do_statement": "condition", "switch_expression": "condition", "catch_clause": None},
        "super": {"superclass", "super_interfaces", "extends_interfaces"},
        "test": re.compile(r"(^|/)(src/test/|test/|tests/)|Tests?\.java$|^Test\w*\.java$|(^|/)Test\w*\.java$"),
    },
    "csharp": {
        "class": {"class_declaration", "interface_declaration", "struct_declaration", "enum_declaration",
                  "record_declaration", "record_struct_declaration"},
        "body": {"declaration_list", "enum_member_declaration_list"},
        "method": {"method_declaration", "constructor_declaration", "destructor_declaration",
                   "operator_declaration", "conversion_operator_declaration"},
        "function": {"local_function_statement"},
        "container": {"namespace_declaration", "file_scoped_namespace_declaration"},
        "header": {"using_directive", "extern_alias_directive", "global_attribute"},
        "call": {"invocation_expression": "function", "object_creation_expression": "type"},
        "branch": {"if_statement": "condition", "while_statement": "condition", "for_statement": "condition",
                   "do_statement": "condition", "switch_statement": "value",
                   "catch_clause": None},
        "super": {"base_list"},
        "test": re.compile(r"(^|/)[^/]*Tests?[^/]*/|Tests?\.cs$|(^|/)TestResults/"),
    },
    "c": {
        "class": set(), "body": set(), "method": set(),
        "function": {"function_definition"}, "container": {"linkage_specification"},
        "header": {"preproc_include", "preproc_def", "preproc_function_def", "type_definition", "struct_specifier",
                   "enum_specifier", "union_specifier", "preproc_ifdef", "preproc_if", "preproc_call"},
        "call": {"call_expression": "function"},
        "branch": {"if_statement": "condition", "while_statement": "condition", "for_statement": "condition",
                   "do_statement": "condition", "switch_statement": "condition"},
        "super": set(),
        "test": re.compile(r"(^|/)(tests?|testing|unittests?|check)/|(^|/)test_[^/]*\.c$|_test\.c$|_tests?\.c$"),
    },
    "cpp": {
        "class": {"class_specifier", "struct_specifier", "union_specifier"},
        "body": {"field_declaration_list"},
        "method": {"function_definition"},
        "function": {"function_definition"},
        "container": {"namespace_definition", "linkage_specification", "template_declaration"},
        "header": {"preproc_include", "preproc_def", "preproc_function_def", "using_declaration", "alias_declaration",
                   "type_definition", "enum_specifier", "namespace_alias_definition", "preproc_ifdef", "preproc_if",
                   "preproc_call", "static_assert_declaration"},
        "call": {"call_expression": "function", "new_expression": "type"},
        "branch": {"if_statement": "condition", "while_statement": "condition", "for_statement": "condition",
                   "do_statement": "condition", "switch_statement": "condition",
                   "catch_clause": None},
        "super": {"base_class_clause"},
        "test": re.compile(r"(^|/)(tests?|testing|unittests?|cpp_tests|gtest|googletest|benchmarks?)/|"
                           r"(^|/)test_[^/]*\.(cc|cpp|cxx)$|_test\.(cc|cpp|cxx)$|_unittest\.(cc|cpp)$"),
    },
    "javascript": {
        "class": {"class_declaration", "class"},
        "body": {"class_body"},
        "method": {"method_definition", "field_definition"},
        "function": {"function_declaration", "generator_function_declaration"},
        "container": {"export_statement"},
        "header": {"import_statement"},
        "call": {"call_expression": "function", "new_expression": "constructor"},
        "branch": {"if_statement": "condition", "while_statement": "condition", "for_statement": "condition",
                   "do_statement": "condition", "switch_statement": "value",
                   "catch_clause": None},
        "super": {"class_heritage"},
        "test": re.compile(r"(^|/)(tests?|__tests__|specs?|coverage|node_modules|dist|build)/|"
                           r"\.(test|spec)\.[cm]?js$|\.min\.js$|(^|/)(karma|jest|webpack|rollup|babel)\.conf"),
    },
    "matlab": {
        "class": {"class_definition"},
        "body": {"methods"},
        "method": {"function_definition"},
        "function": {"function_definition"},
        "container": set(),
        "header": set(),
        "call": {"function_call": "name"},
        "branch": {"if_statement": "condition", "elseif_clause": "condition", "while_statement": "condition",
                   "switch_statement": "condition"},
        "super": {"superclasses"},
        "test": re.compile(r"(^|/)tests?/|Tests?\.m$|(^|/)test_[^/]*\.m$"),
    },
}
EXTENSIONS = {"java": (".java",), "csharp": (".cs",), "c": (".c", ".h"),
              "cpp": (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h", ".ipp", ".inl"),
              "javascript": (".js", ".mjs", ".cjs"), "matlab": (".m",)}
SKIP_DIRS = {".git", "node_modules", "coverage", "build", "dist", "out", "bin", "obj", "target", "CMakeFiles",
             "TestResults", ".vs", ".idea", "__pycache__", ".tox", "venv", "packages"}
VENDORED = re.compile(r"(^|/)(vendor|third_party|thirdparty|3rdparty|external|deps|lib/[^/]+-\d)|\.min\.js$")

# Per-language hazard patterns: semantics that silently change in translation (cf. langs/catalog.py source_notes)
HAZARDS = {
    "java": [("integer_division", r"\b\w+\s*/\s*\w+"), ("string_identity", r"[\w\)\]\"]\s*==\s*\""),
             ("char_arithmetic", r"'.'\s*[-+]"), ("integer_overflow", r"\b(int|long)\b[^;=]*=[^;]*\*"),
             ("checked_exception", r"\bthrows\b"), ("static_state", r"\bstatic\b(?!\s+final)[^(]*=")],
    "csharp": [("integer_division", r"\b\w+\s*/\s*\w+"), ("out_ref_parameter", r"\b(out|ref)\s+\w"),
               ("nullable", r"\?\.|\?\?"), ("linq", r"\.(Where|Select|OrderBy|GroupBy|Aggregate)\("),
               ("property", r"\{\s*get\s*;"), ("unchecked_overflow", r"\bunchecked\b")],
    "c": [("pointer_arithmetic", r"\*\s*\(\s*\w+\s*[+-]|\w+\s*\+\+\s*\)|\[\s*-"),
          ("manual_memory", r"\b(malloc|calloc|realloc|free)\s*\("), ("nul_terminated", r"'\\0'|\bstrlen\("),
          ("errno_return", r"\berrno\b|return\s+-1\s*;"), ("macro", r"^\s*#\s*define"),
          ("fixed_width_overflow", r"\b(unsigned|uint\d+_t|int\d+_t)\b"), ("goto", r"\bgoto\b")],
    "cpp": [("pointer_arithmetic", r"\*\s*\(\s*\w+\s*[+-]"), ("manual_memory", r"\bnew\s+\w|\bdelete\b"),
            ("template", r"\btemplate\s*<"), ("operator_overload", r"\boperator\s*[^\w\s(]"),
            ("integer_overflow", r"\b(unsigned|uint\d+_t|int\d+_t|size_t)\b"), ("reference_param", r"\w&\s+\w"),
            ("ordered_map", r"std::map\b|std::set\b")],
    "javascript": [("loose_equality", r"[^=!]==[^=]|!=[^=]"), ("undefined_vs_null", r"\bundefined\b"),
                   ("float_numbers", r"\bparseInt\b|\bparseFloat\b|Math\.(floor|round|trunc)|\b\d+\s*/\s*\d+"),
                   ("this_binding", r"\bthis\b"), ("prototype", r"\.prototype\b"),
                   ("async", r"\b(async|await|Promise|then)\b|callback"), ("default_sort", r"\.sort\(\s*\)")],
    "matlab": [("one_based_index", r"\(\s*1\s*[,)]|\bend\b\s*[-+)]"), ("elementwise_op", r"\.\*|\./|\.\^"),
               ("matrix_op", r"[\w)\]]\s*\*\s*[\w(\[]"), ("nargin", r"\bnarg(in|out)\b"),
               ("inclusive_range", r"\w+\s*:\s*\w+"), ("column_major", r"\breshape\b|\(:\)")],
}
STD = {
    "java": ("java.", "javax."), "csharp": ("System",), "javascript": (),
    "c": (), "cpp": ("std::",), "matlab": (),
}
C_STD_HEADERS = set("""assert.h ctype.h errno.h float.h limits.h locale.h math.h setjmp.h signal.h stdarg.h stddef.h
stdio.h stdlib.h string.h time.h stdbool.h stdint.h inttypes.h complex.h wchar.h wctype.h unistd.h fcntl.h
sys/types.h sys/stat.h pthread.h iso646.h tgmath.h stdalign.h stdatomic.h stdnoreturn.h threads.h uchar.h""".split())
NODE_BUILTINS = set("""fs path os util events stream http https url querystring crypto zlib child_process assert
buffer net dns readline tty vm cluster worker_threads perf_hooks string_decoder timers""".split())


def _txt(n, src):
    return src[n.start_byte:n.end_byte].decode("utf-8", "replace")


def _name(node, src, lang):
    """The declared name of a class / function / method node."""
    n = node.child_by_field_name("name")
    if n is not None:
        return _txt(n, src)
    if lang in ("c", "cpp"):
        d = node.child_by_field_name("declarator")
        while d is not None and d.type not in ("identifier", "field_identifier", "qualified_identifier",
                                               "destructor_name", "operator_name", "type_identifier"):
            nxt = d.child_by_field_name("declarator")
            d = nxt if nxt is not None else (d.named_children[0] if d.named_children else None)
        return _txt(d, src) if d is not None else None
    return None


def _decl_type(node):
    """For `const f = () => ...` / `var X = class {...}`: the declarator's value node."""
    for d in node.named_children:
        if d.type == "variable_declarator":
            v = d.child_by_field_name("value")
            if v is not None and v.type in ("arrow_function", "function_expression", "function", "class",
                                            "generator_function"):
                return d, v
    return None, None


class Def:
    def __init__(self, fid, kind, qualname, module, file, parent, node, lines, name):
        self.id, self.kind, self.qualname, self.module, self.file = fid, kind, qualname, module, file
        self.parent, self.node, self.lines, self.name = parent, node, set(lines), name
        self.nodes = [node] if node is not None else []
        self.refs = defaultdict(set)  # name -> kinds (call, reference, constructor)
        self.bases = []
        self.signature = None
        self.this_calls = set()


def _lines_of(n):
    return range(n.start_point[0] + 1, n.end_point[0] + 2 if n.end_point[1] > 0 else n.end_point[0] + 1)


def module_name(rel, lang, src_text):
    stem = rel.rsplit(".", 1)[0]
    if lang == "java":
        m = re.search(r"^\s*package\s+([\w.]+)\s*;", src_text, re.M)
        return (m.group(1) + "." if m else "") + stem.split("/")[-1]
    mod = stem.replace("/", ".").replace("-", "_").replace(" ", "_")
    if lang in ("c", "cpp") and rel.endswith((".h", ".hpp", ".hh", ".hxx")):
        mod += "_h"
    return mod


def collect(code, lang):
    """Source files in scope, and excluded ones with a reason."""
    root = Path(code)
    spec = SPECS[lang]
    files, excluded = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in EXTENSIONS[lang]:
            continue
        rel = p.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in rel.split("/")[:-1]):
            continue
        if spec["test"].search(rel):
            excluded.append({"file": rel, "reason": "test, build output or configuration"})
            continue
        if lang == "c" and rel.endswith(".h") and False:
            continue
        files.append(rel)
    return files, excluded


def decompose_file(rel, data, lang, module, defs, rest_nodes):
    """Fragments of one file: Def objects into `defs`; top-level nodes outside any definition into `rest_nodes`."""
    spec = SPECS[lang]
    tree = tslp.get_parser(lang).parse(data)
    src = data

    def add(kind, node, qual, parent, name):
        fid = f"{module}.{qual}"
        k = 2
        while fid in defs:  # overloads
            fid = f"{module}.{qual}~{k}"
            k += 1
        d = Def(fid, kind, qual, module, rel, parent, node, _lines_of(node), name)
        defs[fid] = d
        return d

    def visit_class(node, qual_prefix, parent_id):
        nm = _name(node, src, lang) or "anonymous"
        qual = f"{qual_prefix}.{nm}" if qual_prefix else nm
        cls = add("class", node, qual, parent_id, nm)
        for f in spec["super"]:
            for sup in [c for c in node.named_children if c.type == f]:
                cls.bases += re.findall(r"[A-Za-z_]\w*", _txt(sup, src).replace("extends", " ").replace("implements", " "))
        body = node.child_by_field_name("body")
        if body is None:
            body = next((c for c in node.named_children if c.type in spec["body"]), None)
        if body is None:
            return
        members = [body] if lang != "matlab" else [c for c in node.named_children if c.type == "methods"]
        if lang == "java":
            members += [c for c in body.named_children if c.type == "enum_body_declarations"]
        for b in members:
            for ch in b.named_children:
                inner = ch
                if ch.type == "template_declaration":
                    inner = next((c for c in ch.named_children if c.type in spec["method"] | spec["class"]), ch)
                if inner.type in spec["class"] and (inner.child_by_field_name("body") is not None or lang == "java"
                                                    or lang == "csharp"):
                    visit_class(inner, qual, cls.id)
                elif inner.type in spec["method"] and (lang != "javascript" or inner.type == "method_definition"):
                    if lang == "cpp" and inner.type == "function_definition" and inner.child_by_field_name("body") is None:
                        continue
                    mn = _name(inner, src, lang)
                    if lang == "csharp" and inner.type == "constructor_declaration":
                        mn = nm
                    if mn:
                        m = add("method", ch, f"{qual}.{mn.split('::')[-1]}", cls.id, mn.split("::")[-1])
                        m.signature = signature(inner, src, lang, mn)
                elif lang == "csharp" and inner.type == "property_declaration" and _has_bodies(inner):
                    mn = _name(inner, src, lang)
                    m = add("method", ch, f"{qual}.{mn}", cls.id, mn)
                    m.signature = {"text": _txt(inner, src).split("{")[0].strip(), "params": [], "returns": None,
                                   "decorators": [], "property": True}

    def visit_object(outer, decl, obj, qual_prefix):
        """JavaScript `const X = { f: function () {...}, g() {...}, h: () => ... }`: X is class-like, its function
        properties are its methods (called as X.f)."""
        nm = _txt(decl.child_by_field_name("name"), src)
        qual = f"{qual_prefix}.{nm}" if qual_prefix else nm
        cls = add("class", outer, qual, None, nm)
        for p in obj.named_children:
            if p.type == "pair":
                key, val = p.child_by_field_name("key"), p.child_by_field_name("value")
                if key is not None and val is not None and val.type in ("function_expression", "function",
                                                                        "arrow_function", "generator_function"):
                    mn = _txt(key, src).strip("'\"")
                    m = add("method", p, f"{qual}.{mn}", cls.id, mn)
                    m.signature = signature(val, src, lang, mn)
            elif p.type == "method_definition":
                mn = _name(p, src, lang)
                if mn:
                    m = add("method", p, f"{qual}.{mn}", cls.id, mn)
                    m.signature = signature(p, src, lang, mn)

    def visit(node, qual_prefix):
        for ch in node.named_children:
            t = ch.type
            inner = ch
            if lang in ("c", "cpp") and t in ("preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif",
                                              "preproc_ifndef"):
                rest_nodes.append(ch)  # its directive lines stay in the header; definitions inside are visited
                visit(ch, qual_prefix)
                continue
            if lang == "javascript" and t in ("lexical_declaration", "variable_declaration"):
                d0 = next((d for d in ch.named_children if d.type == "variable_declarator"), None)
                v0 = d0.child_by_field_name("value") if d0 is not None else None
                if v0 is not None and v0.type == "object" and any(
                        (p.type == "pair" and p.child_by_field_name("value") is not None and
                         p.child_by_field_name("value").type in ("function_expression", "function", "arrow_function"))
                        or p.type == "method_definition" for p in v0.named_children):
                    visit_object(ch, d0, v0, qual_prefix)
                    continue
            if t == "export_statement" and lang == "javascript":
                decl = ch.child_by_field_name("declaration") or next(
                    (c for c in ch.named_children if c.type in spec["class"] | spec["function"]
                     | {"lexical_declaration", "variable_declaration"}), None)
                if decl is not None:
                    inner = decl
            if t == "template_declaration":
                inner = next((c for c in ch.named_children if c.type in spec["class"] | spec["function"]
                              | {"declaration"}), ch)
            if inner.type in spec["container"] and inner is ch and t != "export_statement":
                body = ch.child_by_field_name("body")
                nm = ch.child_by_field_name("name")
                prefix = qual_prefix
                if nm is not None and lang == "cpp":
                    prefix = f"{qual_prefix}.{_txt(nm, src)}" if qual_prefix else _txt(nm, src)
                if body is not None:
                    visit(body, prefix)
                elif t == "file_scoped_namespace_declaration":
                    visit(ch, qual_prefix)
                else:
                    rest_nodes.append(ch)
                continue
            if inner.type in spec["class"] and (inner.child_by_field_name("body") is not None
                                                or lang in ("java", "csharp", "matlab")):
                visit_class_wrapped(ch, inner, qual_prefix)
            elif inner.type in spec["function"] and (lang not in ("c", "cpp")
                                                     or inner.child_by_field_name("body") is not None):
                nm = _name(inner, src, lang)
                if not nm:
                    rest_nodes.append(ch)
                    continue
                if "::" in nm and lang == "cpp":  # out-of-line method: Cls::m (parent resolved later)
                    owner, _, mn = nm.rpartition("::")
                    d = add("method", ch, f"{owner.replace('::', '.')}.{mn}", None, mn)
                    d.owner = owner.split("::")[-1]
                else:
                    qual = f"{qual_prefix}.{nm}" if qual_prefix else nm
                    d = add("function", ch, qual, None, nm)
                d.signature = signature(inner, src, lang, nm)
            elif lang == "javascript" and inner.type in ("lexical_declaration", "variable_declaration"):
                decl, val = _decl_type(inner)
                if decl is None:
                    rest_nodes.append(ch)
                    continue
                nm = _txt(decl.child_by_field_name("name"), src)
                if val.type == "class":
                    visit_class_wrapped(ch, val, qual_prefix, nm)
                else:
                    d = add("function", ch, nm, None, nm)
                    d.signature = signature(val, src, lang, nm)
            elif lang == "csharp" and t == "global_statement":
                rest_nodes.append(ch)
            else:
                rest_nodes.append(ch)

    def visit_class_wrapped(outer, cls_node, qual_prefix, forced_name=None):
        if forced_name:
            node_name = forced_name
            qual = f"{qual_prefix}.{node_name}" if qual_prefix else node_name
            d = add("class", outer, qual, None, node_name)
            body = cls_node.child_by_field_name("body")
            for ch in body.named_children if body is not None else []:
                if ch.type in spec["method"]:
                    mn = _name(ch, src, lang)
                    if mn:
                        m = add("method", ch, f"{qual}.{mn}", d.id, mn)
                        m.signature = signature(ch, src, lang, mn)
            return
        visit_class(cls_node, qual_prefix, None)
        if outer is not cls_node:  # e.g. `export class X` / `template <...> class X`: the class owns the wrapper
            last = [d for d in defs.values() if d.node is cls_node]
            if last:
                last[0].lines |= set(_lines_of(outer))

    if lang == "matlab":
        visit_matlab(tree.root_node, src, add, rest_nodes, visit_class, module)
    else:
        visit(tree.root_node, "")
    return tree


def visit_matlab(root, src, add, rest_nodes, visit_class, module):
    for ch in root.named_children:
        if ch.type == "function_definition":
            nm = ch.child_by_field_name("name")
            nm = _txt(nm, src) if nm is not None else module.split(".")[-1]
            d = add("function", ch, nm, None, nm)
            d.signature = signature(ch, src, "matlab", nm)
        elif ch.type == "class_definition":
            visit_class(ch, "", None)
        else:
            rest_nodes.append(ch)


def _has_bodies(prop):
    return any(c.type in ("accessor_list",) and any(a.child_by_field_name("body") is not None
                                                    for a in c.named_children) for c in prop.named_children) or \
        any(c.type == "arrow_expression_clause" for c in prop.named_children)


def signature(node, src, lang, name):
    """{text, params: [{name, default, annotation, kind}], returns, decorators}."""
    params = []
    plist = node.child_by_field_name("parameters")
    if plist is None and lang in ("c", "cpp"):
        d = node.child_by_field_name("declarator")
        while d is not None and d.type != "function_declarator":
            nxt = d.child_by_field_name("declarator")
            d = nxt if nxt is not None else (d.named_children[0] if d.named_children else None)
        plist = d.child_by_field_name("parameters") if d is not None else None
    if plist is None and lang == "matlab":
        plist = next((c for c in node.named_children if c.type == "function_arguments"), None)
    for p in plist.named_children if plist is not None else []:
        if p.type in ("comment", "line_comment", "block_comment"):
            continue
        kind = "positional"
        if p.type in ("spread_parameter", "rest_pattern", "variadic_parameter", "params_array") or "..." in _txt(p, src):
            kind = "var_positional"
        pname = p.child_by_field_name("name") or p.child_by_field_name("pattern") or p.child_by_field_name("declarator") \
            or p.child_by_field_name("left")
        ptype = p.child_by_field_name("type")
        default = p.child_by_field_name("value") or p.child_by_field_name("default_value") or p.child_by_field_name("right")
        text = _txt(p, src)
        nm = _txt(pname, src) if pname is not None else text.split("=")[0].split()[-1] if text.split() else text
        nm = re.sub(r"^[*&\s]+|\[.*$", "", nm)
        params.append({"name": nm, "default": _txt(default, src) if default is not None else None,
                       "annotation": _txt(ptype, src) if ptype is not None else (
                           text.rsplit(nm, 1)[0].strip() if lang in ("c", "cpp") and nm in text else None),
                       "kind": kind})
    rt = node.child_by_field_name("type") or node.child_by_field_name("return_type") or node.child_by_field_name("returns")
    if lang == "matlab":
        out = next((c for c in node.named_children if c.type == "function_output"), None)
        rt = out
    head = _txt(node, src).split("{")[0].split("\n")[0].strip() if lang != "matlab" else _txt(node, src).split("\n")[0]
    decos = [_txt(c, src) for c in node.named_children if c.type in ("annotation", "marker_annotation", "attribute_list",
                                                                     "decorator", "modifiers")]
    return {"text": head[:300], "params": params, "returns": _txt(rt, src) if rt is not None else None,
            "decorators": decos, "is_async": "async" in head.split("(")[0]}


IDENT = {"identifier", "type_identifier", "field_identifier", "property_identifier", "namespace_identifier",
         "shorthand_property_identifier", "simple_identifier", "name"}


def references(node, src, lang, own_names):
    """{name: {kinds}} for identifiers used inside `node` (calls, constructions, other references)."""
    spec = SPECS[lang]
    refs = defaultdict(set)
    this_calls = set()
    stack = [node]
    while stack:
        n = stack.pop()
        t = n.type
        if t in spec["call"]:
            f = n.child_by_field_name(spec["call"][t]) if spec["call"][t] else None
            if f is not None:
                ft = _txt(f, src)
                leaf = re.split(r"[.:>\-]+", ft)[-1].strip()
                leaf = re.sub(r"<.*$", "", leaf)
                obj = n.child_by_field_name("object")
                recv = _txt(obj, src) if obj is not None else ft.rsplit(".", 1)[0] if "." in ft else ""
                kind = "constructor" if t in ("object_creation_expression", "new_expression") else "call"
                refs[leaf].add(kind)
                if recv in ("this", "self", "super", "base") or (not recv and t == "method_invocation"):
                    this_calls.add(leaf)
                if "." in ft or "::" in ft or recv:
                    refs[(recv or ft.rsplit(".", 1)[0]) + "." + leaf].add("qualified")
        elif t in IDENT:
            refs[_txt(n, src)].add("reference")
        stack.extend(n.named_children)
    for o in own_names:
        refs.pop(o, None)
    return refs, this_calls


def comment_line(b, lang):
    s = b.strip()
    if lang == "matlab":
        return s.startswith(b"%")
    return s.startswith((b"//", b"/*", b"*", b"*/"))


def build_generic(name, code, java, out, source_root, source_language, target_language, target=None):
    """The bundle for a non-Python repo. Mirrors build.build_bundle's records and files."""
    import datetime
    import time
    from collections import Counter

    from build import FORMAT, OVERSIZED_LOC, _offsets, dump, segments
    started = time.time()
    lang = source_language
    out = Path(out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    root = Path(code)
    files, excluded = collect(code, lang)
    raw_text, raw_lines, modules, trees = {}, {}, {}, {}
    defs, rest = {}, defaultdict(list)
    for rel in files:
        data = (root / rel).read_bytes()
        raw_text[rel], raw_lines[rel] = data, data.splitlines()
        mod = module_name(rel, lang, data.decode("utf-8", "replace"))
        while mod in modules.values():
            mod += "_"
        modules[rel] = mod
        try:
            trees[rel] = decompose_file(rel, data, lang, mod, defs, rest[rel])
        except Exception as e:  # noqa: BLE001 a file tree-sitter cannot handle is excluded, not fatal
            excluded.append({"file": rel, "reason": f"cannot be parsed: {type(e).__name__}"})
    files = [f for f in files if f in trees]
    module_of = {f: modules[f] for f in files}

    # out-of-line C++ methods join their class
    by_leaf_class = defaultdict(list)
    for d in defs.values():
        if d.kind == "class":
            by_leaf_class[d.name].append(d)
    for d in defs.values():
        if getattr(d, "owner", None) and d.parent is None:
            owners = by_leaf_class.get(d.owner, [])
            if owners:
                d.parent = owners[0].id

    # line ownership: a method's lines leave its class; nested classes leave their parent
    for d in sorted(defs.values(), key=lambda d: -len(d.id)):
        if d.parent and d.parent in defs and defs[d.parent].file == d.file:
            defs[d.parent].lines -= d.lines
    # siblings sharing a line (two definitions on one line): the line stays with the first
    seen = {}
    for d in sorted(defs.values(), key=lambda d: (d.file, min(d.lines) if d.lines else 0)):
        for l in sorted(d.lines):
            if (d.file, l) in seen and seen[(d.file, l)] != d.id:
                d.lines.discard(l)
            else:
                seen[(d.file, l)] = d.id

    # the rest of each file: header (imports, types, constants) and init (top-level code using the project)
    def_names = {d.name for d in defs.values() if d.kind in ("function", "class")}
    frag = dict(defs)
    for rel in files:
        mod = modules[rel]
        data = raw_text[rel]
        header, init = Def(f"{mod}::header", "module_header", f"{mod}::header", mod, rel, None, None, [], "header"), \
            Def(f"{mod}::init", "module_init", f"{mod}::init", mod, rel, None, None, [], "init")
        owned = {l for d in defs.values() if d.file == rel for l in d.lines}
        for n in rest[rel]:
            lines = set(_lines_of(n)) - owned - header.lines - init.lines  # a line goes to one fragment only
            if not lines:
                continue
            spec = SPECS[lang]
            uses = set(re.findall(r"[A-Za-z_]\w*", _txt(n, data))) & def_names
            if n.type in spec["header"] or not uses or n.type in ("comment", "line_comment", "block_comment"):
                header.lines |= lines
                header.nodes.append(n)
            else:
                init.lines |= lines
                init.nodes.append(n)
        # every remaining code line (e.g. a stray `;`) goes to the header
        all_owned = owned | header.lines | init.lines
        for i, line in enumerate(raw_lines[rel], 1):
            if i not in all_owned and line.strip() and not comment_line(line, lang):
                header.lines.add(i)
        for d in (header, init):
            if any(raw_lines[rel][l - 1].strip() and not comment_line(raw_lines[rel][l - 1], lang) for l in d.lines):
                frag[d.id] = d

    # references and edges
    leaf_index = defaultdict(list)
    for d in frag.values():
        if d.kind in ("function", "class", "method"):
            leaf_index[d.name].append(d)
    edges = {}

    def add_edge(s, t, kind, via="tree-sitter"):
        if s == t or s is None or t is None:
            return
        if t in frag and frag[t].parent == s and frag[s].kind == "class" and kind != "init":
            return  # a class shell does not depend on its own members
        e = edges.setdefault((s, t), {"kinds": set(), "via": set()})
        e["kinds"].add(kind)
        e["via"].add(via)

    def resolve(d, name, kinds):
        cands = [c for c in leaf_index.get(name, []) if c.id != d.id]
        if not cands:
            return []
        cls = d.parent if d.kind == "method" else d.id if d.kind == "class" else None
        same_cls = [c for c in cands if c.parent and c.parent == cls]
        if same_cls:
            return same_cls[:1]
        types = [c for c in cands if c.kind == "class"]
        if ("constructor" in kinds or "reference" in kinds) and types:
            same_mod = [c for c in types if c.module == d.module]
            return (same_mod or types)[:1] if len(same_mod or types) == 1 or same_mod else []
        fns = [c for c in cands if c.kind == "function"]
        same_mod = [c for c in fns if c.module == d.module]
        if same_mod:
            return same_mod[:1]
        if len(fns) == 1:
            return fns
        meths = [c for c in cands if c.kind == "method"]
        if "call" in kinds and len(meths) == 1 and not fns:
            return meths
        return []

    recursive = set()
    for d in frag.values():
        nodes = [n for n in d.nodes if n is not None]
        data = raw_text[d.file]
        for n in nodes:
            refs, this_calls = references(n, data, lang, [])
            for nm, ks in refs.items():
                if isinstance(nm, str):
                    d.refs[nm] |= ks
            d.this_calls |= this_calls
        if d.kind in ("function", "method") and d.name in d.refs and "call" in d.refs[d.name]:
            if d.kind == "function" or d.name in d.this_calls:
                recursive.add(d.id)
        for nm, ks in d.refs.items():
            if "." in nm or nm == d.name:
                continue
            for t in resolve(d, nm, ks):
                if d.kind == "module_header" and t.kind != "class":
                    continue
                kind = "constructor" if "constructor" in ks and t.kind == "class" else \
                    "call" if "call" in ks else "reference"
                if kind == "constructor":
                    add_edge(d.id, t.id, "reference")
                    for ctor in [c for c in frag.values() if c.parent == t.id and c.name in (t.name, "constructor",
                                                                                              "__init__")]:
                        add_edge(d.id, ctor.id, "constructor")
                else:
                    add_edge(d.id, t.id, kind)
        if d.kind == "class":
            for b in d.bases:
                for t in [c for c in leaf_index.get(b, []) if c.kind == "class" and c.id != d.id]:
                    add_edge(d.id, t.id, "inherit")
        if d.parent:
            add_edge(d.id, d.parent, "member", "structure")
        hdr = f"{d.module}::header"
        if d.kind != "module_header" and hdr in frag:
            add_edge(d.id, hdr, "module", "structure")
    # headers of files that include / import other project files
    for rel in files:
        hid = f"{modules[rel]}::header"
        if hid not in frag:
            continue
        text = raw_text[rel].decode("utf-8", "replace")
        for inc in re.findall(r'#\s*include\s+"([^"]+)"', text):
            target_file = next((f for f in files if f == inc or f.endswith("/" + inc)), None)
            if target_file and f"{modules[target_file]}::header" in frag and target_file != rel:
                add_edge(hid, f"{modules[target_file]}::header", "module", "structure")
        for req in re.findall(r"require\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)|from\s+['\"](\.{1,2}/[^'\"]+)['\"]", text):
            req = next(r for r in req if r)
            base = (Path(rel).parent / req).as_posix()
            target_file = next((f for f in files if f.rsplit(".", 1)[0] == str(Path(base)).replace("\\", "/")
                                or f == base), None)
            if target_file and target_file != rel and f"{modules[target_file]}::header" in frag:
                add_edge(hid, f"{modules[target_file]}::header", "module", "structure")
    if target_language in ("java", "csharp", "cpp"):  # a class compiles only with its constructors
        for d in frag.values():
            if d.kind == "class":
                for c in frag.values():
                    if c.parent == d.id and c.kind == "method" and c.name in (d.name, "constructor") \
                            and c.file == d.file:
                        add_edge(d.id, c.id, "init", "structure")

    # order
    file_rank = {f: i for i, f in enumerate(files)}
    key = lambda fid: (file_rank[frag[fid].file], min(frag[fid].lines) if frag[fid].lines else 0, fid)  # noqa: E731
    steps = migration_order(list(frag), list(edges), key)
    where = {fid: s for s in steps for fid in s["fragments"]}

    # lizard metrics
    metrics = {}
    try:
        import lizard
        for rel in files:
            info = lizard.analyze_file.analyze_source_code(rel, raw_text[rel].decode("utf-8", "replace"))
            for fn in info.function_list:
                metrics[(rel, fn.start_line)] = {"nloc": fn.nloc, "ccn": fn.cyclomatic_complexity,
                                                 "tokens": fn.token_count}
    except Exception:  # noqa: BLE001 lizard does not know every language (Matlab)
        pass

    # records
    offsets = {f: _offsets(raw_text[f]) for f in files}
    owned_by_file = defaultdict(set)
    for d in frag.values():
        owned_by_file[d.file] |= d.lines
    std = STD.get(lang, ())
    records = []
    hazards_re = [(r, re.compile(p)) for r, p in HAZARDS.get(lang, [])]
    for fid in sorted(frag, key=key):
        d = frag[fid]
        rl = raw_lines[d.file]
        segs = segments_generic(d.lines, rl, offsets[d.file], owned_by_file[d.file] - d.lines, lang)
        if not segs:
            continue
        text = [raw_text[d.file][s["start_byte"]:s["end_byte"]].decode("utf-8", "replace") for s in segs]
        seg_lines = [l for s in segs for l in range(s["start_line"], s["end_line"] + 1)]
        hz = []
        for rule, rx in hazards_re:
            hit = [l for l in seg_lines if rx.search(rl[l - 1].decode("utf-8", "replace"))
                   and not comment_line(rl[l - 1], lang)]
            if hit:
                hz.append({"tool": "acm-patterns", "rule": rule, "lines": hit[:20]})
        ext = defaultdict(set)
        for nm, ks in d.refs.items():
            if "qualified" in ks and isinstance(nm, str):
                head = nm.split(".")[0]
                if nm.startswith(std) or head in ("Math", "JSON", "console", "Object", "Array", "String", "Number",
                                                   "Promise", "System", "Console", "Collections", "Arrays",
                                                   "Objects", "Integer", "Long", "Double"):
                    ext["stdlib"].add(nm)
                elif head not in leaf_index and head not in ("this", "self", "super", "base"):
                    ext["unresolved"].add(nm)
            elif "call" in ks and nm not in leaf_index and isinstance(nm, str):
                ext["builtin" if lang in ("c", "matlab", "javascript") else "unresolved"].add(nm)
        branch = []
        if d.kind in ("function", "method"):
            for n in d.nodes:
                branch += branches(n, raw_text[d.file], lang)
        branch = [b for b in branch if any(s["start_line"] <= b["line"] <= s["end_line"] for s in segs)]
        m = {}
        if d.kind in ("function", "method") and d.node is not None:
            for l in range(d.node.start_point[0] + 1, d.node.start_point[0] + 4):
                if (d.file, l) in metrics:
                    m = metrics[(d.file, l)]
                    break
        loc = sum(1 for s in segs for l in range(s["start_line"], s["end_line"] + 1)
                  if rl[l - 1].strip() and not comment_line(rl[l - 1], lang))
        flags = []
        if fid in recursive:
            flags.append("recursive")
        if where[fid]["cycle"]:
            flags.append("in_cycle")
        if loc > OVERSIZED_LOC:
            flags.append("oversized")
        if VENDORED.search(d.file):
            flags.append("vendored")
        if d.name in ("main", "Main") and d.kind in ("function", "method"):
            flags.append("entrypoint")
        rec = {"id": fid, "kind": d.kind, "qualname": d.qualname, "module": d.module, "file": d.file,
               "parent": d.parent, "children": sorted(c.id for c in frag.values() if c.parent == fid),
               "segments": segs, "loc": loc, "source": "# ...\n".join(text),
               "signature": d.signature if d.kind in ("function", "method") else (
                   {"text": raw_lines[d.file][min(d.lines) - 1].decode("utf-8", "replace").strip()[:300],
                    "bases": d.bases, "keywords": [], "decorators": []} if d.kind == "class" and d.lines else None),
               "depends_on": sorted(t for (s, t) in edges if s == fid),
               "dependents": sorted(s for (s, t) in edges if t == fid),
               "external_apis": {k: sorted(v)[:40] for k, v in sorted(ext.items())},
               "features": features(d, raw_text[d.file], lang), "metrics": m, "branch_conditions": branch,
               "hazards": hz, "diagnostics": [],
               "order": {"step": where[fid]["step"], "wave": where[fid]["wave"], "cycle": where[fid]["cycle"]},
               "flags": flags, "target_hints": []}
        if d.kind in ("module_header", "module_init"):
            rec["imports"] = {"internal_modules": [], "external": {}}
            rec["defines"] = []
        records.append(rec)
    kept = {r["id"] for r in records}
    if kept != set(frag):  # a definition with no code lines left (e.g. a one-line overload collapsed): drop it
        edges = {k: v for k, v in edges.items() if k[0] in kept and k[1] in kept}
        steps = migration_order(sorted(kept, key=key), list(edges), key)
        where = {fid: s for s in steps for fid in s["fragments"]}
        for r in records:
            r["depends_on"] = sorted(t for (s, t) in edges if s == r["id"])
            r["dependents"] = sorted(s for (s, t) in edges if t == r["id"])
            r["children"] = [c for c in r["children"] if c in kept]
            r["order"] = {"step": where[r["id"]]["step"], "wave": where[r["id"]]["wave"],
                          "cycle": where[r["id"]]["cycle"]}
            if r["parent"] not in kept:
                r["parent"] = None
            r["flags"] = [f for f in r["flags"] if f != "in_cycle"] + (["in_cycle"] if where[r["id"]]["cycle"] else [])
    if target:
        from target import link
        hints = link(records, target)
        for r in records:
            r["target_hints"] = hints.get(r["id"], [])

    (out / "fragments.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    via = Counter(v for e in edges.values() for v in e["via"])
    dump(out / "dependency_graph.json", {
        "format": FORMAT, "edge_semantics": "source depends on target: target is migrated first",
        "nodes": [{"id": r["id"], "kind": r["kind"], "module": r["module"], "file": r["file"]} for r in records],
        "edges": [{"source": s, "target": t, "kinds": sorted(e["kinds"]), "via": sorted(e["via"])}
                  for (s, t), e in sorted(edges.items())],
        "modules": {modules[f]: {"file": f, "vendored": bool(VENDORED.search(f)), "python2_source": False,
                                 "imports_internal": [], "imports_external": {}} for f in files},
        "cycles": [s["fragments"] for s in steps if s["cycle"]], "recursive": sorted(recursive & kept),
        "dropped_unconfirmed": [], "external_apis": {}, "edge_sources": dict(sorted(via.items())),
    })
    waves = Counter(s["wave"] for s in steps)
    loc = {r["id"]: r["loc"] for r in records}
    kinds_of = {r["id"]: r["kind"] for r in records}
    dump(out / "migration_order.json", {
        "format": FORMAT,
        "strategy": "bottom-up: strongly connected components of the dependency graph in reverse topological "
                    "order; a step is migrated only after every step it depends on; steps in one wave are "
                    "independent",
        "steps": [{**s, "loc": sum(loc[f] for f in s["fragments"]), "kinds": sorted({kinds_of[f] for f in s["fragments"]})}
                  for s in steps],
        "waves": len(waves), "steps_per_wave": [waves[w] for w in sorted(waves)],
    })
    kinds = Counter(r["kind"] for r in records)
    lizard_version = None
    try:
        import importlib.metadata as md
        lizard_version, ts_version = md.version("lizard"), md.version("tree-sitter-language-pack")
    except Exception:  # noqa: BLE001
        ts_version = None
    manifest = {
        "format": FORMAT, "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "repo": {"name": name, "source_root": source_root, "packages": [], "modules": sorted(modules.values())},
        "migration": {"source_language": lang, "target_language": target_language, "regime": f"{lang}->{target_language}"},
        "files": [{"file": f, "module": module_of[f], "lines": len(raw_lines[f]), "python2_source": False,
                   "vendored": bool(VENDORED.search(f)), "maintainability_index": None} for f in files],
        "excluded_files": excluded, "target": target,
        "toolchain": {"image": "acm-sandbox/analysis", "pipeline": "generic (tree-sitter)",
                      "tools": {"tree-sitter-language-pack": {"ok": True, "version": ts_version, "seconds": 0},
                                "lizard": {"ok": True, "version": lizard_version, "seconds": 0}}},
        "stats": {"fragments": len(records), "by_kind": dict(sorted(kinds.items())), "edges": len(edges),
                  "steps": len(steps), "waves": len(waves), "cycles": sum(s["cycle"] for s in steps),
                  "largest_cycle": max((len(s["fragments"]) for s in steps), default=0), "loc": sum(loc.values()),
                  "fragments_with_hazards": sum(bool(r["hazards"]) for r in records),
                  "fragments_with_target_hints": sum(bool(r["target_hints"]) for r in records),
                  "unconfirmed_edges_dropped": 0, "flags": dict(Counter(f for r in records for f in r["flags"])),
                  "seconds": round(time.time() - started, 1)},
        "outputs": {"fragments": "fragments.jsonl", "dependency_graph": "dependency_graph.json",
                    "migration_order": "migration_order.json", "raw_tool_outputs": "raw/"},
        "warnings": ([f"{len(excluded)} file(s) excluded (tests, build output, unparsable)"] if excluded else []),
    }
    dump(out / "manifest.json", manifest)
    return manifest


def segments_generic(lines, raw_lines, offsets, owned, lang):
    """build.segments with this language's comments."""
    blank = lambda b: not b.strip()  # noqa: E731
    keep = sorted(l for l in lines if l <= len(raw_lines) and not blank(raw_lines[l - 1]))
    ranges = []
    for l in keep:
        if ranges and all((blank(raw_lines[g - 1]) or comment_line(raw_lines[g - 1], lang)) and g not in owned
                          for g in range(ranges[-1][1] + 1, l)):
            ranges[-1][1] = l
        else:
            ranges.append([l, l])
    out = []
    for a, b in ranges:
        if all(comment_line(raw_lines[l - 1], lang) or blank(raw_lines[l - 1]) for l in range(a, b + 1)):
            continue
        out.append({"start_line": a, "end_line": b, "start_byte": offsets[a - 1], "end_byte": offsets[b]})
    return out


def branches(node, src, lang):
    spec = SPECS[lang]["branch"]
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type in spec:
            field = spec[n.type]
            c = n.child_by_field_name(field) if field else n
            if c is not None:
                text = re.sub(r"\s+", " ", _txt(c, src)).strip()
                if field is None:
                    text = text.split("{")[0].strip()
                if text.startswith("(") and text.endswith(")"):
                    text = text[1:-1].strip()
                out.append({"condition": text[:200], "line": n.start_point[0] + 1})
        stack.extend(n.named_children)
    return sorted(out, key=lambda b: b["line"])


def features(d, src, lang):
    text = "\n".join(_txt(n, src) for n in d.nodes if n is not None)
    feats = {}
    for name, rx in (("exception_handling", r"\btry\b"), ("loops", r"\b(for|while|foreach|do)\b"),
                     ("lambdas", r"=>|->|\blambda\b|@\("), ("generics", r"<\s*[A-Z]\w*\s*>"),
                     ("recursion_candidates", r"\b" + re.escape(d.name or "~") + r"\s*\(")):
        n = len(re.findall(rx, text))
        if n:
            feats[name] = n
    return feats
