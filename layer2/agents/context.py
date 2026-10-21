"""One attempt at one unit (`Job`), and how its pieces of context are written into prompts."""
import json
from pathlib import Path

from layer2.javatext import outline

SEP = "# ...\n"


class Job:
    def __init__(self, req, out, root):
        self.req, self.unit, self.attempt = req, req["unit"], req["attempt"]
        self.fragments = req["fragments"]
        self.ids = [f["id"] for f in self.fragments]
        self.deps = req["dependencies"]
        self.dir = Path(out) / "units" / self.unit / f"attempt-{self.attempt}" / "layer2"
        self.store = Path(root) / "units" / self.unit  # kept across attempts: contract, tests, earlier failures
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store.mkdir(parents=True, exist_ok=True)

    def save(self, name, obj):
        p = self.dir / name
        p.write_text(json.dumps(obj, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
        return p

    def load_store(self, name):
        p = self.store / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_store(self, name, obj):
        (self.store / name).write_text(json.dumps(obj, indent=1, default=str, ensure_ascii=False), encoding="utf-8")


def numbered_source(f):
    """The fragment's source with its file line numbers (segments of a multi-part fragment separated by `...`)."""
    parts = f["source"].split(SEP)
    out = []
    for seg, part in zip(f["segments"], parts):
        if out:
            out.append("      ...")
        for k, line in enumerate(part.rstrip("\n").splitlines()):
            out.append(f"{seg['start_line'] + k:>5}| {line}")
    return "\n".join(out)


def facts(f):
    """Layer 0's findings about a fragment, compactly."""
    sig = f.get("signature") or {}
    lines = []
    if sig.get("text"):
        lines.append(f"signature: {sig['text']}" + (f"   (inferred return type: {sig['inferred_return']})"
                                                    if sig.get("inferred_return") else ""))
    if sig.get("decorators"):
        lines.append("decorators: " + ", ".join(sig["decorators"]))
    if f.get("branch_conditions"):
        lines.append("branch conditions: " + "; ".join(f"line {b['line']}: {b['condition']}"
                                                         for b in f["branch_conditions"][:15]))
    if f.get("hazards"):
        lines.append("migration hazards found by Layer 0: " + "; ".join(
            f"lines {','.join(map(str, h.get('lines') or []))}: {h.get('rule')} ({h.get('tool')})"
            for h in f["hazards"][:10]))
    ext = f.get("external_apis") or {}
    used = [x for k in ("stdlib", "third_party", "builtin") for x in ext.get(k, [])]
    if used:
        lines.append("external APIs: " + ", ".join(used[:25]))
    if f.get("features"):
        lines.append("features: " + ", ".join(f"{k}={v}" for k, v in sorted(f["features"].items())))
    if f.get("flags"):
        lines.append("flags: " + ", ".join(f["flags"]))
    return "\n".join(lines)


BRACES = ("java", "csharp", "go", "rust", "cpp", "c", "javascript")


def piece_code(pieces, language):
    """A committed translation as the code a reader needs: for a brace language an outline of the members (bodies
    elided), else the code itself. `language` may be True (Java) / False (Python), as before."""
    language = {True: "java", False: "python3"}.get(language, language)
    body = [p["code"] for p in pieces if p.get("section", "body") == "body" and not p.get("scaffold")]
    text = "\n".join(body)
    return outline(text, members=True).strip() if language in BRACES else text.strip()


def dependencies(job, sk, language, limit_each=900, source="python"):
    """The unit's dependencies as prompt text: where each lives in the target, its source signature and its
    translated API. `language`: the target language id (True / False: Java / Python, as before)."""
    out = []
    for d in job.deps:
        head = f"- {sk.describe(d['id'])}   [{', '.join(d['edge_kinds'])}]"
        if d["status"] == "vendored":
            out.append(head + "  (vendored third-party code, not migrated: use the target standard library's "
                              "equivalent)")
            continue
        sig = (d.get("signature") or {}).get("text")
        code = piece_code(d["translations"], language)
        if len(code) > limit_each:
            code = code[:limit_each] + " ..."
        out.append(head + (f"\n    {source}: {sig}" if sig else "") + ("\n    " + code.replace("\n", "\n    ")
                                                                         if code else ""))
    return "\n".join(out)
