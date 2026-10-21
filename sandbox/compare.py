"""Behavioural comparison of two harness results (legacy vs migrated), for any pair of languages. Runs on the host.

Every harness (sandbox/harness/) reports a case in the same type-tagged JSON: status (ok / exception / timeout /
crash / unresolved), the value, the exception (type, class hierarchy), the arguments and the object afterwards,
and stdout. A *profile* says how strictly two runs must agree:

  strict          same language: exact types everywhere
  py2to3          Python 2 -> 3: a Python 2 native str may match Python 3 text (a hazard note)
  cross:S->T      two different languages S and T (language ids from langs/, e.g. "cross:python3->rust"):
                  - text and bytes stay distinct (a Python 2 / C `char *` str may match text: a hazard note)
                  - sequences compare by items, whatever the container (list, tuple, iterator, array, Vec, slice)
                  - numbers compare by value across int / float when S or T has only one number type
                    (JavaScript, Matlab)
                  - objects compare by class name and by the fields both sides have, names normalised
                    (`_secret_key` ~ `secretKey` ~ `SecretKey`); one-sided fields and values only one language can
                    show (a function, a class) are hazard notes, not differences
                  - exceptions match by name (project exceptions keep their names) or by error category: each
                    language maps its exceptions / errors onto the categories in langs/model.py (ValueError and
                    IllegalArgumentException are both `value`; a Go error or a Rust Err stands for any category)
"python-java" / "python2-java" are kept as names for cross:python3->java / cross:python2->java.
"""
import base64
import json
import math
import re

SEQUENCES = ("list", "tuple", "iterator")
NOT_DATA = ("type", "function", "method", "builtin_function_or_method", "module", "classobj", "instancemethod")
ONE_NUMBER_TYPE = ("javascript", "matlab")
IMMUTABLE = ("none", "bool", "int", "float", "text", "str", "bytes", "tuple")  # argument values a call cannot change
OUT_PARAMS = ("c", "cpp", "csharp")  # sources whose calls can write through a parameter (pointers, references, ref)
NAMED = {"strict": {"bytes_as_text": False, "iter_as_list": False, "cross_language": False},
         "py2to3": {"bytes_as_text": True, "iter_as_list": False, "cross_language": False},
         "python-java": "cross:python3->java", "python2-java": "cross:python2->java"}
PROFILES = NAMED  # the names `compare_results` accepts besides cross:S->T


def resolve(profile):
    """A profile name ("strict", "py2to3", "cross:python3->go", ...) or dict -> its rules."""
    if isinstance(profile, dict):
        return profile
    p = NAMED.get(profile, profile)
    if isinstance(p, dict):
        return {**p, "name": profile}
    if not str(p).startswith("cross:") or "->" not in p:
        raise ValueError(f"unknown comparison profile {profile!r} (strict, py2to3, cross:<source>-><target>)")
    from langs import get
    s, t = (get(x) for x in p[len("cross:"):].split("->"))
    return {"name": profile, "cross_language": True, "iter_as_list": True, "source": s.id, "target": t.id,
            "bytes_as_text": s.id in ("python2", "c", "cpp"),
            "numbers_by_value": s.id in ONE_NUMBER_TYPE or t.id in ONE_NUMBER_TYPE}


def profile_for(source, target):
    """The profile for comparing a `source`-language run with a `target`-language run."""
    from langs import get
    s, t = get(source), get(target)
    if s.id == t.id:
        return "strict"
    if s.id == "python2" and t.id == "python3":
        return "py2to3"
    return f"cross:{s.id}->{t.id}"


def norm_name(name):
    """`_secret_key`, `secretKey`, `SECRET_KEY`, `SecretKey` -> `secretkey`; `std::out_of_range` -> `outofrange`."""
    return re.sub(r"[^a-z0-9]", "", str(name).split("::")[-1].lower())


def _key(v):
    return json.dumps(v, sort_keys=True)


def _coerce(v, rules, notes, path):
    """Migration-aware coercion of a LEGACY value, only tried when the types differ."""
    if v["t"] == "bytes" and rules.get("bytes_as_text"):
        try:
            s = base64.b64decode(v["v"]).decode("utf-8")
        except UnicodeDecodeError:
            return v
        notes.append({"path": path, "hazard": "byte_string_compared_as_text"})
        return {"t": "text", "v": s}
    if v["t"] == "iterator" and rules.get("iter_as_list"):
        notes.append({"path": path, "hazard": "iterator_compared_as_list"})
        return {"t": "list", "v": v["v"]}
    return v


def _not_data(v):
    return v["t"] == "repr" or (v["t"] == "object" and v.get("class") in NOT_DATA)


def _attrs(obj):
    return {norm_name(k.get("v")): (k.get("v"), x) for k, x in obj["v"]["v"]} if obj["v"]["t"] == "dict" else {}


def _number(v):
    try:
        return float(v["v"]) if v["t"] in ("int", "float") else None
    except (TypeError, ValueError):
        return None


DYNAMIC = ("python2", "python3", "javascript", "matlab")


def _number_kinds(v, out=None):
    """The number tags ("int", "float") inside a tagged value."""
    out = set() if out is None else out
    t = v.get("t") if isinstance(v, dict) else None
    if t in ("int", "float"):
        out.add(t)
    elif t in ("list", "tuple", "iterator", "set"):
        for x in v["v"]:
            _number_kinds(x, out)
    return out


def diff(a, b, rules, path, diffs, notes):
    if rules.get("cross_language"):
        if _not_data(a) or b["t"] == "repr":
            if _key(a) != _key(b):
                notes.append({"path": path, "hazard": "not_comparable", "legacy": a.get("class") or a["t"],
                              "migrated": b.get("class") or b["t"]})
            return
        if a["t"] in SEQUENCES and b["t"] in SEQUENCES:
            a, b = {"t": "list", "v": a["v"]}, {"t": "list", "v": b["v"]}
        if rules.get("numbers_by_value") and {a["t"], b["t"]} == {"int", "float"}:
            x, y = _number(a), _number(b)
            if x is not None and y is not None and (x == y or math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12)):
                return
    if a["t"] != b["t"]:
        a = _coerce(a, rules, notes, path)
    if a["t"] != b["t"]:
        diffs.append({"path": path, "kind": "type", "legacy": a, "migrated": b})
        return
    t = a["t"]
    if t == "float":
        x, y = float(a["v"]), float(b["v"])
        if not (x == y or (math.isnan(x) and math.isnan(y))
                or math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12)):
            diffs.append({"path": path, "kind": "value", "legacy": x, "migrated": y})
    elif t in ("list", "tuple", "iterator"):
        if len(a["v"]) != len(b["v"]):
            diffs.append({"path": path, "kind": "length", "legacy": len(a["v"]), "migrated": len(b["v"])})
        if rules.get("cross_language") and not rules.get("numbers_by_value") and rules.get("source") in DYNAMIC \
                and _number_kinds(a) == {"int", "float"}:
            # the dynamic source mixes ints and floats in one sequence ((2, 6.0) next to (2.0, 3.0)); a statically
            # typed result has one number type: compare its numbers by value
            notes.append({"path": path, "hazard": "mixed_int_float_sequence_compared_by_value"})
            rules = {**rules, "numbers_by_value": True}
        for i, (x, y) in enumerate(zip(a["v"], b["v"])):
            diff(x, y, rules, "%s[%d]" % (path, i), diffs, notes)
    elif t == "set":
        ka = sorted(_key(_coerce(x, rules, [], path)) for x in a["v"])
        kb = sorted(_key(x) for x in b["v"])
        if ka != kb and sorted(_key(x) for x in a["v"]) != kb:
            diffs.append({"path": path, "kind": "value", "legacy": a, "migrated": b})
    elif t == "dict":
        mb = {_key(k): (k, v) for k, v in b["v"]}
        seen = set()
        for k, v in a["v"]:
            kk = _key(k)
            if kk not in mb:
                kk = _key(_coerce(k, rules, notes, path + "{key}"))
            sub = "%s[%r]" % (path, k.get("v"))
            if kk not in mb:
                diffs.append({"path": sub, "kind": "missing_key", "legacy": k})
                continue
            seen.add(kk)
            diff(v, mb[kk][1], rules, sub, diffs, notes)
        for kk, (k, _) in mb.items():
            if kk not in seen:
                diffs.append({"path": "%s[%r]" % (path, k.get("v")), "kind": "extra_key", "migrated": k})
    elif t == "object":
        if not rules.get("cross_language"):
            if a["class"] != b["class"]:
                diffs.append({"path": path, "kind": "class", "legacy": a["class"], "migrated": b["class"]})
            diff(a["v"], b["v"], rules, path + ".__dict__", diffs, notes)
            return
        if norm_name(a["class"]) != norm_name(b["class"]):
            diffs.append({"path": path, "kind": "class", "legacy": a["class"], "migrated": b["class"]})
        la, lb = _attrs(a), _attrs(b)
        for k in sorted(set(la) & set(lb)):
            diff(la[k][1], lb[k][1], rules, "%s.%s" % (path, la[k][0]), diffs, notes)
        only = sorted(la[k][0] for k in set(la) - set(lb)) + sorted(lb[k][0] for k in set(lb) - set(la))
        if only:
            notes.append({"path": path, "hazard": "attributes_on_one_side_only", "names": only})
    elif a.get("v") != b.get("v"):  # none/bool/int/text/bytes/repr
        diffs.append({"path": path, "kind": "value", "legacy": a, "migrated": b})


def _categories(lang, names, own_type):
    cats = set()
    for n in names:
        if n in ("BaseException", "object", "Throwable"):
            continue
        cs = lang.categories_of(n)
        if "generic" in cs and n != own_type:  # every exception derives from the generic one
            cs = cs - {"generic"}
        cats |= cs
    return cats


def exception_matches(legacy, migrated, rules=None):
    """Cross-language: is the target's exception (or error value) a faithful counterpart of the source's?
    An exception of a library the target does not have (`foreign`, e.g. Python's struct.error) matches any
    exception of the target."""
    if legacy.get("foreign"):
        return True
    ln = [legacy["type"]] + list(legacy.get("hierarchy") or [])
    mn = [migrated["type"]] + list(migrated.get("hierarchy") or [])
    if norm_name(legacy["type"]) in {norm_name(n) for n in mn}:  # project exceptions keep their names
        return True
    if not rules or "source" not in rules:
        return False
    from langs import get
    s, t = get(rules["source"]), get(rules["target"])
    return bool(_categories(s, ln, legacy["type"]) & _categories(t, mn, migrated["type"]))


def _type_error(legacy, rules):
    from langs import get
    e = legacy.get("exception") or {}
    names = [e.get("type")] + list(e.get("hierarchy") or [])
    return "type" in _categories(get(rules["source"]), names, e.get("type")) if rules.get("source") else False


def compare_results(legacy, migrated, profile="py2to3"):
    rules, diffs, notes = resolve(profile), [], []
    ls, ms = legacy["status"], migrated["status"]
    if ms == "untestable" and ls == "exception" and rules.get("cross_language") and _type_error(legacy, rules):
        # the source rejects a value of the wrong type (TypeError); the target's declared types cannot even
        # receive that value: nothing to compare
        notes.append({"path": "call", "hazard": "type_error_input_not_expressible_in_target",
                      "legacy": legacy["exception"]["type"], "migrated": migrated.get("message")})
    elif ms in ("unresolved", "untestable"):
        diffs.append({"path": "call", "kind": ms, "migrated": migrated.get("message")})
    elif ls != ms:
        diffs.append({"path": "status", "kind": "value", "legacy": ls, "migrated": ms})
    elif ls == "exception":
        le, me = legacy["exception"], migrated["exception"]
        same = exception_matches(le, me, rules) if rules.get("cross_language") else le["type"] == me["type"]
        if not same:
            diffs.append({"path": "exception.type", "kind": "value", "legacy": le["type"], "migrated": me["type"]})
        elif rules.get("cross_language") and norm_name(le["type"]) != norm_name(me["type"]):
            notes.append({"path": "exception.type", "hazard": "exception_matched_by_category" if not le.get("foreign")
                          else "library_exception_compared_loosely",
                          "legacy": f"{le.get('module') + '.' if le.get('module') else ''}{le['type']}",
                          "migrated": me["type"]})
    elif ls == "ok":
        diff(legacy["value"], migrated["value"], rules, "return", diffs, notes)
    if ls in ("ok", "exception") and ls == ms:
        for i, (x, y) in enumerate(zip(legacy.get("args_after") or [], migrated.get("args_after") or [])):
            if rules.get("cross_language") and rules.get("source") not in OUT_PARAMS and \
                    isinstance(x, dict) and x.get("t") in IMMUTABLE:
                continue  # a scalar argument cannot be changed by the call: only its type would differ
            diff(x, y, rules, "args_after[%d]" % i, diffs, notes)
        if legacy.get("self_after") and migrated.get("self_after"):
            diff(legacy["self_after"], migrated["self_after"], rules, "self_after", diffs, notes)
        if (legacy.get("stdout") or "") != (migrated.get("stdout") or ""):
            diffs.append({"path": "stdout", "kind": "value",
                          "legacy": legacy.get("stdout"), "migrated": migrated.get("stdout")})
    return {"case_id": legacy.get("case_id"), "profile": rules.get("name", profile), "equivalent": not diffs,
            "diffs": diffs, "hazard_notes": notes,
            # raw evidence for the Repair agent
            "migrated_traceback": (migrated.get("exception") or {}).get("traceback")}
