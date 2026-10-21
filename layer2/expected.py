"""What a target project's own tests expect (RepoTransBench targets ship only tests, a runner and
project_structure.txt). Read on the host from the benchmark's target directory, for any target language:

  paths        files project_structure.txt lists (code files only)
  imports      [(module, [names])]: what the tests import from the project
                 Python `from src.linkedlist import List, size`      C / C++ `#include "benchmarks.h"`
                 Rust `use crate_name::map::*` / `{get, put}`         Go `import "mod/pkg"`
                 C# / Java `using Ns;` / `import a.b.C;`
  identifiers  every identifier the test code uses, with its count
  crate        Rust: the crate name the tests `use`
These are hints for the target layout (layer2/layout.py): a source name keeps the spelling the tests use
(`toStr` stays `toStr` in Python when the tests call it so), and a module goes where the tests import it from.
When the tests say nothing (many are self-contained), the language's conventions decide.
"""
import re
from collections import Counter
from pathlib import Path

from layer0.__main__ import RTB, RTB_NAMES

STD = {"std", "core", "alloc", "crate", "super", "self"}
CODE_EXT = {"python3": (".py",), "java": (".java",), "csharp": (".cs",), "go": (".go",), "rust": (".rs",),
            "cpp": (".h", ".hpp", ".cpp", ".cc"), "c": (".h", ".c"), "javascript": (".js",), "matlab": (".m",)}


def norm(name):
    """Spelling-insensitive key: want_bytes ~ wantBytes ~ WantBytes."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def find_target_project(manifest):
    """The benchmark's target directory for this bundle (None outside RepoTransBench)."""
    mig = manifest["migration"]
    src = RTB_NAMES.get("python3" if mig["source_language"] == "python2" else mig["source_language"])
    tgt = RTB_NAMES.get(mig["target_language"])
    if not src or not tgt:
        return None
    d = RTB / "target_projects" / src / tgt / manifest["repo"]["name"]
    return d if d.is_dir() else None


def structure_paths(text, exts):
    """File paths from a project_structure.txt tree drawing."""
    out, stack = [], []
    for line in text.splitlines():
        m = re.search(r"[├└]──\s*([^\s#│]+)", line)
        if not m:
            continue
        depth = m.start() // 4
        name = m.group(1).strip()
        stack = stack[:depth] + [name.rstrip("/")]
        if not name.endswith("/") and name.endswith(exts):
            out.append("/".join(stack))
    return out


def _strip(code):
    code = re.sub(r"/\*.*?\*/", " ", code, flags=re.S)
    code = re.sub(r"(?m)(//|#(?!include)).*$", " ", code)
    return re.sub(r'"(?:\\.|[^"\\])*"' + r"|'(?:\\.|[^'\\])*'", '""', code)


def _imports(code, lang):
    out = []
    if lang == "python3":
        for m in re.finditer(r"^\s*from\s+([\w.]+)\s+import\s+(\([^)]*\)|[^\n]+)", code, re.M):
            names = [n.split(" as ")[0].strip() for n in m.group(2).strip("()").replace("\n", " ").split(",")]
            out.append((m.group(1), [n for n in names if n and n != "*"]))
        for m in re.finditer(r"^\s*import\s+([\w.]+)", code, re.M):
            out.append((m.group(1), []))
    elif lang == "rust":
        for m in re.finditer(r"^\s*use\s+([\w:]+?)(?:::\{([^}]*)\}|::\*|::(\w+))?\s*;", code, re.M):
            names = [n.strip().split(" as ")[0] for n in (m.group(2) or m.group(3) or "").split(",") if n.strip()]
            out.append((m.group(1), names))
    elif lang in ("c", "cpp"):
        out += [(m.group(1), []) for m in re.finditer(r'#include\s+"([^"]+)"', code)]
    elif lang == "go":
        for block in re.findall(r"import\s*\(([^)]*)\)", code) + re.findall(r'import\s+("[^"]+")', code):
            out += [(p, []) for p in re.findall(r'"([^"]+)"', block)]
    elif lang in ("csharp", "java"):
        out += [(m.group(1), []) for m in re.finditer(r"^\s*(?:using|import)\s+(?:static\s+)?([\w.]+)\s*;", code, re.M)]
    elif lang == "javascript":
        out += [(m.group(1), []) for m in re.finditer(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", code)]
    return out


class Expectations:
    def __init__(self, target_dir=None, language=None):
        self.dir, self.language = (Path(target_dir) if target_dir else None), language
        self.paths, self.imports, self.identifiers, self.crate, self.test_files = [], [], Counter(), None, []
        if not self.dir or not self.dir.is_dir():
            return
        exts = CODE_EXT.get(language, ())
        ps = self.dir / "project_structure.txt"
        if ps.exists():
            self.paths = [p for p in structure_paths(ps.read_text(encoding="utf-8", errors="replace"), exts)
                          if not re.search(r"(^|/)(tests?|public_tests)/|_test\.|test_", p)]
        for f in sorted(self.dir.rglob("*")):
            if not f.is_file() or f.suffix not in exts:
                continue
            raw = f.read_text(encoding="utf-8", errors="replace")
            self.test_files.append(f.relative_to(self.dir).as_posix())
            self.imports += _imports(raw, language)
            self.identifiers.update(re.findall(r"[A-Za-z_]\w*", _strip(raw)))
        if language == "rust":
            roots = Counter(m.split("::")[0] for m, _ in self.imports if m.split("::")[0] not in STD)
            self.crate = roots.most_common(1)[0][0] if roots else None
        self._by_norm = {}
        for name, n in self.identifiers.most_common():
            self._by_norm.setdefault(norm(name), name)

    def __bool__(self):
        return bool(self.test_files or self.paths)

    def spelling(self, name):
        """The tests' spelling of a source name, if they use one (want_bytes -> wantBytes), else None."""
        if not self.identifiers:
            return None
        return self._by_norm.get(norm(name))

    def module_for(self, names):
        """The imported module whose imported names cover most of `names` (a source module's top-level names),
        or None."""
        keys = {norm(n) for n in names}
        best, score = None, 0
        for mod, imported in self.imports:
            s = len(keys & {norm(n) for n in imported})
            if s > score:
                best, score = mod, s
        return best

    def path_for(self, stem, exts):
        """A listed file whose name matches `stem` (spelling-insensitive) and extension, or None."""
        hits = [p for p in self.paths if norm(p.rsplit("/", 1)[-1].rsplit(".", 1)[0]) == norm(stem)
                and p.endswith(exts)]
        return sorted(hits, key=lambda p: (not p.startswith(("src/", "include/")), len(p)))[0] if hits else None

    def summary(self):
        return {"dir": str(self.dir) if self.dir else None, "test_files": len(self.test_files),
                "paths": self.paths[:40], "imports": [m for m, _ in self.imports][:40], "crate": self.crate}
