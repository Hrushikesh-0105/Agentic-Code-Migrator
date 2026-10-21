"""Graph-quality proxies for one bundle, measured from the fragments' source text rather than from the tools that
built the graph (host side, standard library only). Not contract checks: a static call graph cannot be exact.

evidence  of the edges to a function/method/class, how many name their target in the source fragment's code.
          Skipped: structural edges (`member`, `module`), edges to the source's own class (a method reading a
          class attribute) and `constructor` edges (instantiating a subclass that inherits `__init__` correctly
          names the subclass, not the class that defines `__init__`)
recall    of the plain calls `name(...)` to a uniquely named project function/class, how many have an edge
"""
import re
from collections import Counter

from .contract import load


def _leaf(fid):
    parts = fid.rsplit(".", 2)
    return parts[-2] if parts[-1] == "__init__" and len(parts) > 1 else parts[-1]


def audit(bundle):
    b = load(bundle)
    frs = {r["id"]: r for r in b["fragments"]}
    sem = [e for e in b["graph"]["edges"] if "::" not in e["target"] and not {"member", "constructor", "init"} & set(e["kinds"])
           and set(e["kinds"]) - {"module"}]
    unevidenced = [[e["source"], e["target"], e["via"]] for e in sem
                   if not re.search(rf"\b{re.escape(_leaf(e['target']))}\b", frs[e["source"]]["source"])]

    defs = [r for r in frs.values() if r["kind"] in ("function", "method", "class")]
    counts = Counter(r["qualname"].rsplit(".", 1)[-1] for r in defs)
    unique = {r["qualname"].rsplit(".", 1)[-1]: r["id"] for r in defs
              if r["kind"] in ("function", "class") and counts[r["qualname"].rsplit(".", 1)[-1]] == 1}
    found, missed = 0, []
    for r in frs.values():
        if r["kind"] not in ("function", "method", "module_init"):
            continue
        code = re.sub(r"(?m)#.*$|'[^'\n]*'|\"[^\"\n]*\"", "", r["source"])  # drop comments and one-line strings
        for name in set(re.findall(r"(?<![\w.])(\w+)\s*\(", code)):
            target = unique.get(name)
            if target and target != r["id"] and not re.match(rf"\s*(async\s+)?(def|class)\s+{name}\b", code):
                if target in r["depends_on"]:
                    found += 1
                else:
                    missed.append([r["id"], target])
    return {"edge_evidence": f"{len(sem) - len(unevidenced)}/{len(sem)}",
            "call_recall": f"{found}/{found + len(missed)}",
            "unevidenced_edges": unevidenced, "missed_calls": missed}
