"""MCP server `acm-analysis`: read-only queries over the Layer 0 bundle of the repository being migrated.

python -m layer2.servers.analysis --bundle DIR [--log FILE]
Tools: get_fragment, get_dependencies, get_dependents, find_fragments, get_module_header, get_class_members,
get_target_location, get_expected_api. Fragment ids are Layer 0's (`pkg.mod.Class.method`, `pkg.mod::header`).
"""
import argparse

from layer0 import contract as bundle_contract
from layer2.layout import make_skeleton
from layer2.servers import make_server, tool

ap = argparse.ArgumentParser()
ap.add_argument("--bundle", required=True)
ap.add_argument("--log")
a = ap.parse_args()

server = make_server(
    "acm-analysis", log_file=a.log,
    instructions="Read-only static analysis of the legacy repository (Layer 0): the source, signature, inferred "
                 "types, branch conditions and migration hazards of each fragment, the dependency graph, and where "
                 "each fragment lives in the target skeleton.")
B = bundle_contract.load(a.bundle)
FR = {f["id"]: f for f in B["fragments"]}
EDGES = {(e["source"], e["target"]): e["kinds"] for e in B["graph"]["edges"]}
SK = make_skeleton(B)


def _get(fid):
    if fid not in FR:
        near = [x for x in FR if fid.split(".")[-1] in x][:5]
        raise ValueError(f"no fragment {fid!r}" + (f"; similar: {near}" if near else ""))
    return FR[fid]


def _brief(fid, edge=None):
    f = FR[fid]
    row = {"id": fid, "kind": f["kind"], "file": f["file"], "lines": [f["segments"][0]["start_line"],
                                                                      f["segments"][-1]["end_line"]],
           "signature": (f.get("signature") or {}).get("text"), "target": SK.describe(fid)}
    if edge:
        row["edge_kinds"] = edge
    return row


@tool(server)
def get_fragment(fragment_id: str, include_source: bool = True) -> dict:
    """One fragment of the legacy code: kind, file and lines, source, signature (with the inferred return type
    where Layer 0 has one), external APIs, branch conditions, migration hazards, complexity, flags, and where it
    goes in the target layout."""
    f = _get(fragment_id)
    keep = ("id", "kind", "module", "file", "parent", "signature", "external_apis", "features", "metrics",
            "branch_conditions", "hazards", "diagnostics", "flags", "target_hints")
    out = {k: f.get(k) for k in keep}
    out["lines"] = [[s["start_line"], s["end_line"]] for s in f["segments"]]
    out["target"] = SK.describe(fragment_id)
    out["depends_on"], out["dependents"] = f["depends_on"], f["dependents"]
    if include_source:
        out["source"] = f["source"]
    return out


@tool(server)
def get_dependencies(fragment_id: str) -> list:
    """The fragments this one depends on (calls, references, bases, members...), with edge kinds and their location
    in the target skeleton. In bottom-up order they are migrated before it."""
    f = _get(fragment_id)
    return [_brief(d, EDGES.get((fragment_id, d))) for d in f["depends_on"]]


@tool(server)
def get_dependents(fragment_id: str) -> list:
    """The fragments that depend on this one (they are migrated after it)."""
    f = _get(fragment_id)
    return [_brief(d, EDGES.get((d, fragment_id))) for d in f["dependents"]]


@tool(server)
def find_fragments(name: str, limit: int = 10) -> list:
    """Fragments whose id contains `name` (case-insensitive), e.g. a function or class name."""
    hits = [fid for fid in FR if name.lower() in fid.lower()]
    return [_brief(fid) for fid in sorted(hits, key=len)[:max(1, min(limit, 50))]]


@tool(server)
def get_module_header(module: str) -> dict:
    """The imports, constants and shims at the top of a module (its `::header` fragment)."""
    f = _get(f"{module}::header")
    return {"id": f["id"], "file": f["file"], "source": f["source"], "target": SK.describe(f["id"])}


@tool(server)
def get_class_members(class_id: str) -> list:
    """The methods (and nested classes) of a class fragment, with signatures and target names."""
    _get(class_id)
    return [_brief(fid) for fid, f in FR.items() if f.get("parent") == class_id]


@tool(server)
def get_target_location(fragment_id: str) -> dict:
    """Where the fragment's translation goes: target file, class, member name and role (static method, method,
    getter, constructor, class, static members), and how tests call it."""
    _get(fragment_id)
    out = dict(SK.location(fragment_id))
    out["describe"] = SK.describe(fragment_id)
    if SK.testable(fragment_id):
        out["legacy_call"] = SK.python_call(fragment_id) if SK.src.python else SK.src.call(fragment_id)
        out["target_call"] = SK.target_call(fragment_id, [], [])
    return out


@tool(server)
def get_expected_api(name: str) -> dict:
    """What the target project's own tests expect (RepoTransBench targets). Java targets: the methods, static
    methods and construction of the class `name`. Other targets: how the tests spell `name` (a class or function
    name, spelling-insensitive), which project modules they import, and the files project_structure.txt lists."""
    api = ((B["manifest"].get("target") or {}).get("expected_api") or {})
    if api:
        if name not in api:
            raise ValueError(f"no expected API for {name!r}; known: {sorted(api)[:30]}")
        return api[name]
    exp = getattr(SK, "exp", None)
    if exp is None or not exp:
        raise ValueError("the target's tests are not available")
    return {"tests_spell_it": exp.spelling(name), "uses": exp.identifiers.get(exp.spelling(name) or name, 0),
            **exp.summary()}


if __name__ == "__main__":
    server.run()
