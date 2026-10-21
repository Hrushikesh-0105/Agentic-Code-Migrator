"""Executor & Sandbox (no LLM): builds the candidate tree (committed codebase + the unit's candidate pieces) through
the acm-workspace MCP server, then compiles it and runs the unit's differential tests on it in the Docker sandbox
through the acm-sandbox MCP server, and compares each case with the legacy run (the oracle).

  Java target           javac + the Java harness (run_java_cases)
  Python -> Python      Python's compiler on the unit's files + runner.py (run_python_cases)
  any other target      the language's toolchain on the whole tree (build_tree: go build + vet, cargo build,
                        g++ / gcc -fsyntax-only, dotnet build, node --check, Python compile) + its differential
                        harness (run_cases, sandbox/polyglot.py)
Compile errors are attached to the fragment whose piece holds the failing line."""
import time

from layer2.javatext import piece_lines
from sandbox.compare import profile_for


class Executor:
    name = "executor"

    def __init__(self, hub, sk, bundle):
        self.hub, self.sk = hub, sk
        mig = bundle["manifest"]["migration"]
        self.source, self.target = mig["source_language"], mig["target_language"]
        self.java = self.target == "java"
        self.splice = self.source.startswith("python") and self.target.startswith("python")
        py2 = self.source == "python2"
        if self.java and self.source.startswith("python"):
            self.profile = "python2-java" if py2 else "python-java"
        elif self.splice:
            self.profile = "py2to3" if py2 else "strict"
        else:
            self.profile = profile_for(self.source, self.target)

    def run(self, job, pieces, tests, tag):
        start = time.time()
        tree = f"{job.unit}-a{job.attempt}-{tag}"
        asm = self.hub.call("workspace", "assemble_candidate", {"candidate": pieces, "tree": tree}, who=self.name)
        cases = [c for c in tests.get("cases", []) if c["fragment"] in job.ids]
        rep = {"tree": asm["dir"], "files": asm["files"], "assembly_problems": asm["problems"], "cases": len(cases),
               "profile": self.profile}
        if self.java:
            r = self.hub.call("sandbox", "run_java_cases", {
                "code_dir": asm["dir"], "cases": [{"case_id": c["case_id"], "java": c["java"]} for c in cases],
                "log_dir": str(job.dir / "sandbox"), "log_name": tag}, who=self.name)
            rep.update(compiled=r["compiled"], compile_errors=self.locate(job, pieces, r["errors"], tree),
                       javac_tail=r.get("javac_tail", ""), results=r["results"])
        elif self.splice:
            errors = self.py_compile(job, pieces, tree)
            rep.update(compiled=not errors, compile_errors=errors, results=[])
            if not errors and cases:
                rep["results"] = self.hub.call("sandbox", "run_python_cases", {
                    "code_dir": asm["dir"], "cases": [c["python"] for c in cases], "image": "py3",
                    "log_dir": str(job.dir / "sandbox"), "log_name": tag}, who=self.name)
        else:
            b = self.hub.call("sandbox", "build_tree", {"language": self.target, "code_dir": asm["dir"],
                                                        "log_dir": str(job.dir / "sandbox" / f"build-{tag}")},
                              who=self.name)
            errors = self.locate(job, pieces, b["errors"], tree)
            if not b["built"] and not errors:
                errors = [{"fragment": "(build)", "file": "", "line": 0, "message": (b.get("log_tail") or
                                                                                    "the build failed")[-600:]}]
            rep.update(compiled=b["built"], compile_errors=errors, results=[], build_tool=b.get("tool"))
            if b["built"] and cases:
                r = self.hub.call("sandbox", "run_cases", {
                    "language": self.target, "code_dir": asm["dir"],
                    "cases": [{"case_id": c["case_id"], "call": c["target_call"]} for c in cases],
                    "log_dir": str(job.dir / "sandbox"), "log_name": tag}, who=self.name)
                rep["results"] = r["results"]
                if not r["built"]:  # the harness's own build of the tree failed (e.g. a driver type error)
                    rep["harness_errors"] = r.get("errors", [])[:8]
        if rep["compiled"] and cases and not rep["results"]:  # never a silent pass: no result = a failed case
            rep["results"] = [{"case_id": c["case_id"], "status": "crash", "message": "no result from the harness"}
                              for c in cases]
        rep["comparisons"] = self.hub.call("sandbox", "compare_results", {
            "legacy": [c["legacy"] for c in cases], "migrated": rep["results"], "profile": self.profile},
            who=self.name) if rep["compiled"] and cases and rep["results"] else []
        rep["seconds"] = round(time.time() - start, 1)
        return rep

    def locate(self, job, pieces, errors, tree):
        """Attach each compile error to the fragment whose piece holds the failing line."""
        texts, out = {}, []
        for e in errors[:12]:
            path = str(e.get("file") or "").replace("\\", "/").lstrip("./") if not str(e.get("file", "")).startswith(
                "../") else e["file"]
            if path not in texts:
                try:
                    texts[path] = self.hub.call("workspace", "read_file", {"path": path, "tree": tree,
                                                                           "numbered_lines": False}, who=self.name)
                except Exception:  # noqa: BLE001
                    texts[path] = ""
            lines = texts[path].splitlines()
            owner = None
            for fid, ps in pieces.items():
                for p in ps:
                    if p["target_file"] != path or p.get("section") == "preamble" or p.get("scaffold"):
                        continue
                    span = piece_lines(texts[path], p["code"])
                    if span and span[0] <= e["line"] <= span[1]:
                        owner = fid
            out.append({**e, "file": path,
                        "fragment": owner or ("(the unit's imports)" if e["line"] < 5 else "(committed code)"),
                        "code": lines[e["line"] - 1].strip() if 0 < e["line"] <= len(lines) else ""})
        return out

    def py_compile(self, job, pieces, tree):
        errors = []
        for fid, ps in pieces.items():
            for p in ps:
                try:
                    text = self.hub.call("workspace", "read_file", {"path": p["target_file"], "tree": tree,
                                                                    "numbered_lines": False}, who=self.name)
                    compile(text, p["target_file"], "exec")
                except SyntaxError as e:
                    errors.append({"fragment": fid, "file": p["target_file"], "line": e.lineno or 0,
                                   "message": e.msg, "detail": (e.text or "").rstrip(), "code": (e.text or "").strip()})
        return errors
