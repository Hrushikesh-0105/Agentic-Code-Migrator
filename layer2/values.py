"""Test values: the plain JSON the Test-Generation agent writes -> the harnesses' type-tagged values.

The agent writes JSON, with markers for what JSON cannot say:
  "text"                       a str (Python 3 source) / native str (Python 2 source); Java String
  {"$bytes": "ab\\u00ff"}      bytes, one byte per character (code points 0-255); Java byte[]
  {"$tuple": [...]}            a tuple                      {"$set": [...]}  a set
  {"$dict": [[key, value]]}    a dict with non-string keys  {"$float": "nan" | "inf" | "-inf"}
  {"$literal": "b'x'"}         a Python literal (used for parameter defaults)
Integers become {"t": "int"} (any size), lists {"t": "list"}, objects with string keys {"t": "dict"}.
"""
import ast
import base64


class ValueProblem(ValueError):
    pass


def _bytes(b):
    return {"t": "bytes", "v": base64.b64encode(b).decode("ascii")}


def from_python(v, py2=False):
    """A Python value (from a literal) -> tagged."""
    if v is None:
        return {"t": "none"}
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, int):
        return {"t": "int", "v": str(v)}
    if isinstance(v, float):
        return {"t": "float", "v": repr(v) if v != v or v in (float("inf"), float("-inf")) else v}
    if isinstance(v, str):
        return {"t": "str" if py2 else "text", "v": v}
    if isinstance(v, bytes):
        return _bytes(v)
    if isinstance(v, (list, tuple, set, frozenset)):
        t = "list" if isinstance(v, list) else "tuple" if isinstance(v, tuple) else "set"
        return {"t": t, "v": [from_python(x, py2) for x in v]}
    if isinstance(v, dict):
        return {"t": "dict", "v": [[from_python(k, py2), from_python(x, py2)] for k, x in v.items()]}
    raise ValueProblem(f"cannot express {type(v).__name__} in a test")


def tagged(v, py2=False):
    """Agent JSON -> tagged value. `py2`: plain strings are the legacy interpreter's native str."""
    if v is None or isinstance(v, bool):
        return {"t": "none"} if v is None else {"t": "bool", "v": v}
    if isinstance(v, int):
        return {"t": "int", "v": str(v)}
    if isinstance(v, float):
        return {"t": "float", "v": v}
    if isinstance(v, str):
        return {"t": "str" if py2 else "text", "v": v}
    if isinstance(v, list):
        return {"t": "list", "v": [tagged(x, py2) for x in v]}
    if not isinstance(v, dict):
        raise ValueProblem(f"unsupported JSON value {v!r}")
    if len(v) == 1:
        (k, x), = v.items()
        if k == "$bytes":
            if not isinstance(x, str):
                raise ValueProblem("$bytes takes a string")
            try:
                return _bytes(x.encode("latin-1"))
            except UnicodeEncodeError:
                return _bytes(x.encode("utf-8"))
        if k == "$tuple":
            return {"t": "tuple", "v": [tagged(i, py2) for i in x]}
        if k == "$set":
            return {"t": "set", "v": [tagged(i, py2) for i in x]}
        if k == "$dict":
            return {"t": "dict", "v": [[tagged(a, py2), tagged(b, py2)] for a, b in x]}
        if k == "$float":
            return {"t": "float", "v": str(x)}
        if k == "$literal":
            try:
                return from_python(ast.literal_eval(x), py2)
            except (ValueError, SyntaxError) as e:
                raise ValueProblem(f"not a literal: {x}") from e
        if k.startswith("$"):
            raise ValueProblem(f"unknown marker {k}")
    return {"t": "dict", "v": [[tagged(k, py2), tagged(x, py2)] for k, x in v.items()]}


def show(t):
    """Tagged value -> short readable text for prompts and reports."""
    k, v = t.get("t"), t.get("v")
    if k == "none":
        return "None"
    if k in ("int", "bool"):
        return str(v)
    if k == "float":
        return str(v)
    if k in ("text", "str"):
        return repr(v)
    if k == "bytes":
        return repr(base64.b64decode(v))
    if k in ("list", "tuple", "set", "iterator"):
        inner = ", ".join(show(x) for x in v[:12]) + (", ..." if len(v) > 12 else "")
        return {"list": "[%s]", "tuple": "(%s)", "set": "{%s}", "iterator": f"<{t.get('kind', 'iter')} %s>"}[k] % inner
    if k == "dict":
        return "{" + ", ".join(f"{show(a)}: {show(b)}" for a, b in v[:12]) + (", ..." if len(v) > 12 else "") + "}"
    if k == "object":
        return f"{t.get('class')}(" + ", ".join(f"{a.get('v')}={show(b)}" for a, b in t["v"]["v"][:8]) + ")"
    return str(v)[:200]
