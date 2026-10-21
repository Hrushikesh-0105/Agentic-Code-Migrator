"""MCP server `acm-sandbox`: the Docker sandbox of Phase 1 (sandbox/), for agents.

python -m layer2.servers.sandbox --allow DIR [--allow DIR ...] [--log FILE]
Every run is a throwaway, network-less, capability-less container (sandbox/executor.py, sandbox/polyglot.py).
Tools: run_python_cases, run_java_cases, run_cases (any language of langs/), build_tree (any language),
compare_results, run_test_suite.
"""
import argparse

from layer2.servers import Roots, make_server, tool
from sandbox import executor, polyglot
from sandbox.compare import compare_results as _compare
from sandbox.compare import resolve

ap = argparse.ArgumentParser()
ap.add_argument("--allow", action="append", default=[], help="a directory the sandbox may mount or log to")
ap.add_argument("--log")
a = ap.parse_args()

server = make_server(
    "acm-sandbox", log_file=a.log,
    instructions="Runs code in hardened Docker containers (no network): differential test cases on code in any "
                 "supported language (Python, Java, C#, Go, Rust, C++, C, JavaScript, Matlab/Octave), builds of a "
                 "target tree with its language's toolchain, behavioural comparison of two runs (also across "
                 "languages), and a project's own test suite.")
ALLOWED = Roots(a.allow)


@tool(server)
def run_python_cases(code_dir: str, cases: list, log_dir: str, image: str = "py3", per_case_timeout: float = 10,
                     log_name: str = "python") -> list:
    """Run harness cases ({case_id, module, callable, args, kwargs, init, attr, trace}) on the Python code in
    `code_dir` with interpreter image py27 or py3; one fresh interpreter per case. Returns one result per case:
    status, type-tagged value, exception (type, hierarchy, traceback), args_after, self_after, stdout, lines."""
    if image not in ("py27", "py3"):
        raise ValueError("image is py27 or py3")
    return executor.run_cases(image, ALLOWED.check(code_dir), cases, per_case_timeout,
                              log_dir=ALLOWED.check(log_dir), log_name=log_name)


@tool(server)
def run_java_cases(code_dir: str, cases: list, log_dir: str, per_case_timeout: float = 10,
                   log_name: str = "java") -> dict:
    """Compile every .java file under `code_dir` with javac 17 and, if it compiles, run harness cases
    ({case_id, java: {class, kind: static|instance|new, member, init, args}}) on it; no cases = compile only.
    Returns {compiled, files, errors: [{file, line, message, detail}], results}."""
    return executor.run_java_cases(ALLOWED.check(code_dir), cases, per_case_timeout, log_dir=ALLOWED.check(log_dir),
                                   log_name=log_name)


@tool(server)
def run_cases(language: str, code_dir: str, cases: list, log_dir: str, per_case_timeout: float = 10,
              log_name: str = "run", sources: list = None) -> dict:
    """Run language-neutral cases ({case_id, call: {kind: function|static|method|property|constructor, module,
    class, member, constructor, args, init}}) on the code in `code_dir`, written in `language` (python3, python2,
    java, csharp, go, rust, cpp, c, javascript, matlab), in that language's differential harness; one process per
    case. `sources`: the files under test, relative to code_dir (a legacy Java project is compiled from them alone,
    with -sourcepath; a C / C++ one falls back to them when its other units do not build). Returns {built,
    errors: [{file, line, message}], results: runner.py-style tagged records}."""
    r = polyglot.run_cases(language, ALLOWED.check(code_dir), cases, log_dir=ALLOWED.check(log_dir),
                           per_case_timeout=per_case_timeout, log_name=log_name, sources=sources or None)
    return {"built": r["built"], "errors": r.get("errors", [])[:20], "results": r["results"],
            "log_dir": r.get("log_dir")}


@tool(server)
def build_tree(language: str, code_dir: str, log_dir: str) -> dict:
    """Build the code in `code_dir` with `language`'s toolchain without running it (javac, go build + vet, cargo
    build, g++ / gcc -fsyntax-only per file, dotnet build, node --check, Python compile). Returns {built, errors:
    [{file, line, message}], tool}."""
    return polyglot.build_tree(language, ALLOWED.check(code_dir), log_dir=ALLOWED.check(log_dir))


@tool(server)
def compare_results(legacy: list, migrated: list, profile: str) -> list:
    """Compare two runs of the same cases, case by case: status, exception type, return value, argument mutation,
    object state and stdout. Profiles: strict, py2to3, cross:<source>-><target> (e.g. cross:java->python3;
    python-java and python2-java are names for the Python -> Java ones)."""
    resolve(profile)  # raises on an unknown profile
    by_id = {r.get("case_id"): r for r in migrated}
    return [_compare(r, by_id[r.get("case_id")], profile) for r in legacy if r.get("case_id") in by_id]


@tool(server)
def run_test_suite(project_dir: str, log_dir: str, image: str = "java17", command: str = "",
                   timeout: int = 900) -> dict:
    """Run a project's own test suite (pytest or Maven Surefire) on a pristine copy; JUnit XML is parsed into
    per-test outcomes."""
    r = executor.run_suite(image, ALLOWED.check(project_dir), timeout=timeout, command=command or None,
                           log_dir=ALLOWED.check(log_dir))
    return {"returncode": r["returncode"], "summary": r["summary"], "tests": r["tests"][:200],
            "log_tail": r["log_tail"][-3000:], "log_dir": r["log_dir"]}


if __name__ == "__main__":
    server.run()
