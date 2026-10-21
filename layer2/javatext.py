"""Small Java source utilities for prompts: outlines (declarations kept, method bodies elided), numbered listings,
and locating a piece's lines in an assembled file (to map a javac error to the fragment that caused it)."""
import re

TYPE_KEYWORD = re.compile(r"\b(class|interface|enum|record)\b")


def _skip(src, i):
    """If a comment, string, char or text block starts at i, return the index after it, else None."""
    if src.startswith("//", i):
        j = src.find("\n", i)
        return len(src) if j < 0 else j
    if src.startswith("/*", i):
        j = src.find("*/", i + 2)
        return len(src) if j < 0 else j + 2
    if src.startswith('"""', i):
        j = src.find('"""', i + 3)
        return len(src) if j < 0 else j + 3
    if src[i] in "\"'":
        q, j = src[i], i + 1
        while j < len(src) and src[j] != q and src[j] != "\n":
            j += 2 if src[j] == "\\" else 1
        return j + 1
    return None


def matching_brace(src, i):
    depth = 0
    while i < len(src):
        j = _skip(src, i)
        if j is not None:
            i = j
            continue
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(src) - 1


def _code_only(text):
    out, i = [], 0
    while i < len(text):
        j = _skip(text, i)
        if j is not None:
            out.append(" ")
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def outline(src, members=False):
    """Type declarations, fields and member signatures; every method / initializer body becomes `{ ... }`.
    `members`: `src` is a list of class members (a piece), not a compilation unit."""
    out, i, depth, class_body, header = [], 0, 0, ({0} if members else set()), 0
    while i < len(src):
        j = _skip(src, i)
        if j is not None:
            out.append(src[i:j])
            i = j
            continue
        c = src[i]
        if c == "{":
            hdr = _code_only(src[header:i])
            if (depth == 0 and not members) or (depth in class_body and TYPE_KEYWORD.search(hdr) and "->" not in hdr
                              and "=" not in hdr and "new " not in hdr):
                depth += 1
                class_body.add(depth)
                out.append(c)
            elif depth in class_body:
                i = matching_brace(src, i)
                out.append("{ ... }")
            else:
                depth += 1
                out.append(c)
            i += 1
            header = i
            continue
        if c == "}":
            class_body.discard(depth)
            depth -= 1
            header = i + 1
        elif c == ";":
            header = i + 1
        out.append(c)
        i += 1
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(out))
    return re.sub(r"(?m)^[ \t]*// @acm:slot .*\n", "", text)


def numbered(text, start=1, width=4):
    return "\n".join(f"{n:>{width}}| {line}" for n, line in enumerate(text.splitlines(), start))


def piece_lines(file_text, code):
    """1-based (first, last) lines of `code` inside `file_text` (indentation ignored), or None."""
    want = [l.strip() for l in code.strip("\n").splitlines()]
    have = [l.strip() for l in file_text.splitlines()]
    if not want:
        return None
    for k in range(len(have) - len(want) + 1):
        if have[k] == want[0] and have[k:k + len(want)] == want:
            return k + 1, k + len(want)
    first = next((w for w in want if w), None)
    for k, h in enumerate(have):
        if first and h == first:
            return k + 1, k + len(want)
    return None
