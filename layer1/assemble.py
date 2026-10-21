"""Assemble the migrated codebase from the committed translations (see protocol.py for the two modes).

splice   every in-scope source file is rewritten: each migrated fragment's segments are replaced by its
         translation, and the bytes between fragments (blank lines, comments) are kept. A fragment that was
         not migrated keeps its original code under an `# ACM: not migrated` marker
compose  target files are built only from pieces: preamble lines, then top-level pieces, with every slot
         filled by the pieces addressed to it, in source order
Both are deterministic: the same translations always give the same bytes.
"""
from collections import defaultdict
from pathlib import Path

from .protocol import SEPARATOR, SLOT

NOT_MIGRATED = "# ACM: not migrated ({status}): {fid}\n"


def source_key(fragment):
    return (fragment["file"], fragment["segments"][0]["start_line"])


def splice(manifest, fragments, status, translations, source_root):
    """-> ({relpath: bytes}, problems)"""
    by_file, out, problems = defaultdict(list), {}, []
    for f in fragments:
        by_file[f["file"]].append(f)
    for entry in manifest["files"]:
        path = entry["file"]
        data = (Path(source_root) / path).read_bytes()
        offs = [0]
        for line in data.splitlines(keepends=True):
            offs.append(offs[-1] + len(line))
        edits = []  # (start_byte, end_byte, replacement bytes)
        for f in by_file.get(path, []):
            segs = f["segments"]
            if status[f["id"]] in ("migrated",):
                pieces = translations.get(f["id"], [])
                if len(pieces) != 1:
                    problems.append(f"{f['id']}: {len(pieces)} pieces in splice mode")
                    continue
                code = pieces[0]["code"]
                parts = code.split(SEPARATOR)
                if len(parts) != len(segs):  # one block for the whole fragment: it takes the first segment's place
                    parts = [code] + [""] * (len(segs) - 1)
            elif status[f["id"]] == "vendored":
                continue
            else:
                first = data[segs[0]["start_byte"]:segs[0]["end_byte"]].decode("utf-8", "replace")
                indent = first[:len(first) - len(first.lstrip())]
                marker = indent + NOT_MIGRATED.format(status=status[f["id"]], fid=f["id"])
                parts = [None] * len(segs)
                edits.append((segs[0]["start_byte"], segs[0]["start_byte"], marker.encode("utf-8")))
            for s, part in zip(segs, parts):
                if part is not None:
                    edits.append((s["start_byte"], s["end_byte"], part.encode("utf-8")))
        res, pos = bytearray(), 0
        for start, end, text in sorted(edits, key=lambda e: (e[0], e[1])):
            res += data[pos:start] + text
            pos = end
        out[path] = bytes(res + data[pos:])
    return out, problems


def _render(code, by_slot, used, problems, file):
    lines = []
    for line in code.split("\n"):
        m = SLOT.match(line)
        if not m:
            lines.append(line)
            continue
        indent, name = m.group(1), m.group(2)
        if name in used:
            problems.append(f"{file}: slot {name} opened twice")
            continue
        used.add(name)
        kids = [_render(p["code"], by_slot, used, problems, file).rstrip("\n") for p in by_slot.get(name, [])]
        for text in _join(kids):
            lines.append(indent + text if text.strip() else "")
    return "\n".join(lines)


def _join(blocks):
    out = []
    for i, b in enumerate(blocks):
        if i:
            out.append("")
        out.extend(b.split("\n"))
    return out


def compose(fragments, status, translations):
    """-> ({relpath: bytes}, problems)"""
    key = {f["id"]: source_key(f) for f in fragments}
    files = defaultdict(list)
    for fid, pieces in translations.items():
        if status.get(fid) != "migrated":
            continue
        for i, p in enumerate(pieces):
            files[p["target_file"]].append((key[fid], i, p))
    out, problems = {}, []
    for path in sorted(files):
        pieces, seen = [], set()
        for _, _, p in sorted(files[path], key=lambda x: (x[0], x[1])):
            if p.get("scaffold"):
                ident = (p.get("section"), p.get("slot"), p["code"])
                if ident in seen:
                    continue
                seen.add(ident)
            pieces.append(p)
        pre, pre_seen = [], set()
        for p in pieces:
            if p.get("section") == "preamble":
                for line in p["code"].splitlines():
                    if line.strip() and line not in pre_seen:
                        pre_seen.add(line)
                        pre.append(line)
        body = [p for p in pieces if p.get("section", "body") == "body"]
        by_slot = defaultdict(list)
        for p in body:
            if p.get("slot"):
                by_slot[p["slot"]].append(p)
        used = set()
        top = [_render(p["code"], by_slot, used, problems, path).rstrip("\n") for p in body if not p.get("slot")]
        for name in sorted(set(by_slot) - used):
            problems.append(f"{path}: {len(by_slot[name])} piece(s) for slot {name}, which no piece opens")
        text = "\n".join(pre) + ("\n\n" if pre and top else "\n" if pre else "") + "\n".join(_join(top))
        out[path] = (text.rstrip("\n") + "\n").encode("utf-8")
    return out, problems


def assemble(mode, bundle, status, translations, source_root, out_dir):
    """Write the migrated codebase to out_dir. Returns {mode, files: {path: lines}, problems}."""
    if mode == "splice":
        files, problems = splice(bundle["manifest"], bundle["fragments"], status, translations, source_root)
    else:
        files, problems = compose(bundle["fragments"], status, translations)
    out_dir = Path(out_dir)
    for path, data in files.items():
        (out_dir / path).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / path).write_bytes(data)
    return {"mode": mode, "files": {p: data.count(b"\n") for p, data in sorted(files.items())}, "problems": problems}
