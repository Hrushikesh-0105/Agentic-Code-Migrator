"""Does the migrated codebase build? Compiles it in the network-less sandbox with the target language's toolchain
(javac in acm-sandbox/java17; Python's own compiler in acm-sandbox/py3; go build + vet, cargo build, g++ / gcc,
dotnet build, node --check through sandbox/polyglot.build_tree). This is a report on the output, not a Layer 1
pass/fail criterion."""
import json
import re
from pathlib import Path

from sandbox.executor import run_suite
from sandbox.logs import rel, write_json

JAVAC = "find . -name '*.java' | sort > /tmp/sources && echo ACM_FILES $(wc -l < /tmp/sources) && " \
        "javac -nowarn -encoding UTF-8 -d /tmp/classes @/tmp/sources"
PY_COMPILE = r"""python - <<'EOF'
import json, pathlib
files, failed = sorted(pathlib.Path('.').rglob('*.py')), []
for p in files:
    try:
        compile(p.read_bytes(), str(p), 'exec')
    except SyntaxError as e:
        failed.append({'file': p.as_posix(), 'line': e.lineno, 'error': e.msg})
print('ACM_RESULT ' + json.dumps({'files': len(files), 'failed': failed}))
EOF"""


def build_check(migrated, language, log_dir):
    """-> {ok, tool, files, errors: [..]} and writes log_dir/build_check.json (+ the sandbox's output.log)."""
    migrated, log_dir = Path(migrated), Path(log_dir)
    if not migrated.exists() or not any(migrated.rglob("*")):
        res = {"ok": False, "tool": None, "files": 0, "errors": ["nothing was assembled"]}
    elif language == "java":
        r = run_suite("java17", migrated, timeout=600, command=JAVAC, log_dir=log_dir / "sandbox")
        out = r["log_tail"]
        m = re.search(r"ACM_FILES (\d+)", out)
        errors = [l for l in out.splitlines() if ": error:" in l]
        res = {"ok": r["returncode"] == 0, "tool": "javac (OpenJDK 17)", "files": int(m.group(1)) if m else 0,
               "errors": errors[:20]}
    elif not str(language).startswith("python"):
        from sandbox.polyglot import build_tree
        b = build_tree(language, migrated, log_dir=log_dir / "sandbox")
        files = sum(1 for p in migrated.rglob("*") if p.is_file())
        res = {"ok": b["built"], "tool": b.get("tool"), "files": files,
               "errors": [f"{e['file']}:{e['line']}: {e['message']}" for e in b["errors"]]
               or ([b["log_tail"][-500:]] if b.get("log_tail") else [])}
        res["log"] = rel(Path(b["log_dir"]) / "build.log")
        write_json(log_dir / "build_check.json", res)
        return res
    else:
        r = run_suite("py3", migrated, timeout=600, command=PY_COMPILE, log_dir=log_dir / "sandbox")
        m = re.search(r"ACM_RESULT (.*)", r["log_tail"])
        data = json.loads(m.group(1)) if m else {"files": 0, "failed": [{"error": r["log_tail"][-500:]}]}
        res = {"ok": r["returncode"] == 0 and not data["failed"], "tool": "compile() (CPython 3.11)",
               "files": data["files"], "errors": [f"{x.get('file')}:{x.get('line')}: {x['error']}"
                                                  for x in data["failed"]]}
    res["log"] = rel(log_dir / "sandbox" / "output.log")
    write_json(log_dir / "build_check.json", res)
    return res
