"""The Layer 1 <-> Layer 2 contract: the request Layer 1 sends for one unit, and the verdict it expects back.

A *unit* is one step of Layer 0's migration order: one fragment, or a cycle of fragments migrated together.
Layer 1 sends a TranslationRequest; Layer 2 (Analyzer -> Translator -> Test-Gen -> Executor -> Verdict -> Repair)
answers with a Verdict. Layer 1 never trusts a verdict: evaluate() checks it against the request before anything
is committed, and an invalid verdict counts as a failed attempt.

The translated code comes back as *pieces*. How the pieces become files depends on the assembly mode:
  splice   (Python -> Python) one piece per fragment, written over the fragment's own lines in its own file;
           a fragment with several segments may return several parts joined by SEPARATOR, one per segment
  compose  (other pairs)    pieces are placed in target files by Layer 2. A piece can open a named slot with a
           comment line `// @acm:slot NAME` (or `# ...`, `% ...`); pieces with `slot: NAME` are inserted
           there, in source order. `preamble` pieces (package, imports) go first, de-duplicated by line.
           `scaffold` pieces (e.g. the class that holds a module's functions) may be sent by several fragments;
           identical copies are kept once
"""
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROTOCOL = {"name": "acm-layer1-layer2", "version": "1.0"}
OUTCOMES = ("pass", "fail", "error", "needs_human")
SEPARATOR = "# ...\n"  # Layer 0 joins a fragment's segments with this line (layer0/contract.py)
SLOT = re.compile(r"^([ \t]*)(?://|#|%)[ \t]*@acm:slot[ \t]+(\S+)[ \t]*\r?$", re.M)
SAFE_PATH = re.compile(r"^(?!/)(?![A-Za-z]:)[A-Za-z0-9_.$/-]+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Piece(_Strict):
    fragment: str = Field(description="id of the fragment this code translates")
    target_file: str = Field(description="path in the migrated codebase, relative and POSIX style")
    code: str
    section: Literal["body", "preamble"] = "body"
    slot: Optional[str] = Field(None, description="compose: insert into the slot of this name in target_file")
    scaffold: bool = Field(False, description="compose: shared container code; identical copies are kept once")


class Evidence(_Strict):
    compiled: Optional[bool] = None
    tests_generated: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    failing: list[str] = []
    log_dir: Optional[str] = None
    notes: str = ""


class AgentStep(_Strict):
    agent: str
    status: str
    seconds: float = 0.0
    detail: str = ""


class Verdict(_Strict):
    protocol: dict
    unit: str
    attempt: int
    outcome: Literal["pass", "fail", "error", "needs_human"]
    reason: str = ""
    translations: list[Piece] = Field([], description="required for pass; optional candidate code otherwise")
    evidence: Evidence = Field(default_factory=Evidence)
    trace: list[AgentStep] = []
    usage: dict[str, float] = {}


class Dependency(_Strict):
    id: str
    kind: str
    edge_kinds: list[str]
    status: Literal["migrated", "vendored"]
    file: str
    signature: Optional[dict]
    translations: list[Piece] = Field(description="the committed translation (empty for vendored code)")


class Feedback(_Strict):
    attempt: int
    outcome: str
    reason: str
    problems: list[str]
    evidence: dict


class TranslationRequest(_Strict):
    protocol: dict
    run: str
    unit: str
    step: int
    wave: int
    cycle: bool
    attempt: int
    max_attempts: int = Field(description="the last attempt Layer 1 will make before escalating")
    migration: dict = Field(description="source_language, target_language, regime (from the Layer 0 manifest)")
    assembly: Literal["splice", "compose"]
    target: Optional[dict] = Field(description="language, package, and the expected API of the classes this "
                                               "unit's target hints name (Java targets only)")
    fragments: list[dict] = Field(description="the unit's Layer 0 fragment records, verbatim")
    dependencies: list[Dependency] = Field(description="every fragment the unit depends on, already migrated")
    feedback: list[Feedback] = Field(description="previous attempts of this unit, oldest first")
    human_guidance: Optional[str] = None


def schemas():
    return {"translation_request": TranslationRequest.model_json_schema(), "verdict": Verdict.model_json_schema()}


def opened_slots(code):
    return [m.group(2) for m in SLOT.finditer(code)]


def check_pieces(pieces, fragments, mode, committed):
    """Problems with the pieces offered for the unit's `fragments` (Layer 0 records). `committed` are the pieces
    already in the migrated codebase (compose: their slots can be used)."""
    problems, ids = [], {f["id"]: f for f in fragments}
    for p in pieces:
        if p.fragment not in ids:
            problems.append(f"piece for {p.fragment}, which is not in this unit")
        if not SAFE_PATH.match(p.target_file) or ".." in p.target_file.split("/"):
            problems.append(f"{p.fragment}: unsafe target_file {p.target_file!r}")
    missing = sorted(set(ids) - {p.fragment for p in pieces})
    if missing:
        problems.append(f"no translation for {', '.join(missing)}")
    if mode == "splice":
        for fid, f in ids.items():
            mine = [p for p in pieces if p.fragment == fid]
            if len(mine) > 1:
                problems.append(f"{fid}: splice mode takes one piece per fragment, got {len(mine)}")
            for p in mine:
                if p.target_file != f["file"]:
                    problems.append(f"{fid}: splice mode writes the fragment's own file {f['file']}, not {p.target_file}")
                if p.section != "body" or p.slot or p.scaffold:
                    problems.append(f"{fid}: section/slot/scaffold are compose-mode fields")
        return problems
    opened = {}  # (file, slot) -> code of the piece that opens it
    for p in list(committed) + list(pieces):
        for name in opened_slots(p.code):
            key = (p.target_file, name)
            if key in opened and not (p.scaffold and opened[key] == p.code):
                problems.append(f"{p.fragment}: slot {name} is already open in {p.target_file}")
            opened[key] = p.code
    for p in pieces:
        if p.section == "preamble" and (p.slot or opened_slots(p.code)):
            problems.append(f"{p.fragment}: a preamble piece cannot use or open slots")
        if p.slot and (p.target_file, p.slot) not in opened:
            problems.append(f"{p.fragment}: slot {p.slot} is not open in {p.target_file}")
    return problems


def evaluate(raw, unit, attempt, fragments, mode, committed):
    """Layer 1's check of a verdict. Returns {outcome, valid, problems, reason, evidence, pieces}: an invalid
    verdict becomes outcome "error" (a failed attempt), whatever Layer 2 claimed."""
    if isinstance(raw, dict) and "_exception" in raw:
        return {"outcome": "error", "valid": False, "problems": [f"Layer 2 raised {raw['_exception']}"],
                "reason": "", "evidence": {}, "pieces": []}
    try:
        v = Verdict.model_validate(raw)
    except ValidationError as e:
        errs = [f"{'.'.join(map(str, x['loc'])) or 'verdict'}: {x['msg']}" for x in e.errors()[:5]]
        return {"outcome": "error", "valid": False, "problems": ["verdict does not match the schema: " + "; ".join(errs)],
                "reason": raw.get("reason", "") if isinstance(raw, dict) else "", "evidence": {}, "pieces": []}
    problems = []
    if v.protocol != PROTOCOL:
        problems.append(f"protocol {v.protocol}, expected {PROTOCOL}")
    if (v.unit, v.attempt) != (unit, attempt):
        problems.append(f"verdict is for {v.unit} attempt {v.attempt}, expected {unit} attempt {attempt}")
    if v.outcome == "pass":
        problems += check_pieces(v.translations, fragments, mode, [Piece(**p) for p in committed])
    return {"outcome": v.outcome if not problems else "error", "valid": not problems, "problems": problems,
            "reason": v.reason, "evidence": v.evidence.model_dump(),
            "pieces": [p.model_dump() for p in v.translations] if v.outcome == "pass" and not problems else []}
