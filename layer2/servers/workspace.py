"""MCP server `acm-workspace`: the migrated project under construction (the File System box).

python -m layer2.servers.workspace --root DIR --bundle DIR --source DIR [--log FILE]
State lives in <root>/committed.json: the translation pieces of every fragment migrated so far. Layer 1 is the
authority (sync_committed, from each request's dependencies); a passing unit is recorded at once (record_pass),
so the next unit is built against it. A candidate tree = the whole committed codebase + one unit's candidate
pieces, assembled exactly as Layer 1 assembles the final codebase (layer1/assemble.py); the sandbox then compiles
and tests it.
Tools: sync_committed, record_pass, get_pieces, assemble_candidate, read_file, outline_file, list_files.
"""
import argparse
import json
import shutil
from pathlib import Path

from layer0 import contract as bundle_contract
from layer1.assemble import compose, splice
from layer2.javatext import numbered, outline
from layer2.servers import Roots, make_server, tool

ap = argparse.ArgumentParser()
ap.add_argument("--root", required=True)
ap.add_argument("--bundle", required=True)
ap.add_argument("--source", required=True, help="the repo's source root (Layer 1's source_root)")
ap.add_argument("--log")
a = ap.parse_args()

server = make_server(
    "acm-workspace", log_file=a.log,
    instructions="The migrated codebase as it stands: committed translations, candidate trees for testing a "
                 "unit, file contents and Java outlines.")
ROOT = Path(a.root).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
B = bundle_contract.load(a.bundle)
FR = {f["id"]: f for f in B["fragments"]}
_MIG = B["manifest"]["migration"]
MODE = "splice" if _MIG["target_language"].startswith("python") and _MIG["source_language"].startswith("python") \
    else "compose"
STATE = ROOT / "committed.json"
ALLOWED = Roots([ROOT])
BRACES = (".java", ".cs", ".go", ".rs", ".hpp", ".h", ".cpp", ".c", ".js")


def _load():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"pieces": {}, "source": {}}


def _save(st):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def _tree(tree):
    d = ROOT / "trees" / tree if tree else ROOT / "trees" / "committed"
    return ALLOWED.check(d)


@tool(server)
def sync_committed(dependencies: list) -> dict:
    """Layer 1's committed translations (a request's `dependencies`: [{id, status, translations}]) replace what
    the workspace holds for those fragments."""
    st, changed = _load(), 0
    for d in dependencies:
        if d.get("status") == "migrated" and d.get("translations") and st["pieces"].get(d["id"]) != d["translations"]:
            st["pieces"][d["id"]], st["source"][d["id"]] = d["translations"], "layer1"
            changed += 1
    _save(st)
    return {"fragments": len(st["pieces"]), "updated_from_layer1": changed}


@tool(server)
def record_pass(pieces: dict, unit: str) -> dict:
    """Record a unit's passing translation ({fragment id: [pieces]}) so later units are built against it."""
    st = _load()
    for fid, ps in pieces.items():
        if fid not in FR:
            raise ValueError(f"unknown fragment {fid}")
        st["pieces"][fid], st["source"][fid] = ps, f"layer2:{unit}"
    _save(st)
    return {"fragments": len(st["pieces"])}


@tool(server)
def get_pieces(fragment_id: str) -> list:
    """The committed translation pieces of a fragment (empty if it is not migrated yet)."""
    return _load()["pieces"].get(fragment_id, [])


def _assemble(candidate, out):
    pieces = {**_load()["pieces"], **candidate}
    if MODE == "compose":
        files, problems = compose(B["fragments"], {f: "migrated" for f in pieces}, pieces)
    else:
        status = {f: ("migrated" if f in pieces else "pending") for f in FR}
        files, problems = splice(B["manifest"], B["fragments"], status, pieces, a.source)
    shutil.rmtree(out, ignore_errors=True)
    for path, data in files.items():
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_bytes(data)
    return files, problems


@tool(server)
def assemble_candidate(candidate: dict, tree: str) -> dict:
    """Write the committed codebase plus `candidate` ({fragment id: [pieces]}, overriding) to the tree `tree`.
    Returns the tree's directory, its files and assembly problems (e.g. a slot nobody opens)."""
    if not tree.replace("-", "").replace("_", "").isalnum():
        raise ValueError("tree names are letters, digits, - and _")
    out = _tree(tree)
    files, problems = _assemble(candidate, out)
    return {"dir": str(out), "files": sorted(files), "problems": problems, "mode": MODE}


@tool(server)
def read_file(path: str, tree: str = "", numbered_lines: bool = True) -> str:
    """A file of a candidate tree (default: the committed codebase, assembled on demand), optionally numbered."""
    d = _tree(tree)
    if not tree:
        _assemble({}, d)
    p = ALLOWED.check(d / path)
    if not p.exists():
        raise ValueError(f"{path} does not exist in {'the committed codebase' if not tree else tree}")
    text = p.read_text(encoding="utf-8")
    return numbered(text) if numbered_lines else text


@tool(server)
def outline_file(path: str, tree: str = "") -> str:
    """A file of the committed codebase (or a candidate tree) with function and method bodies elided: its types,
    fields and member signatures (Java, C#, Go, Rust, C++, C, JavaScript). Other files (Python, Matlab): the
    file itself."""
    text = read_file(path, tree, numbered_lines=False)
    return outline(text) if path.endswith(BRACES) else text


@tool(server)
def list_files(tree: str = "") -> list:
    """The files of the committed codebase (or of a candidate tree)."""
    d = _tree(tree)
    if not tree:
        _assemble({}, d)
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())


if __name__ == "__main__":
    server.run()
