"""Host-side sandbox executor. Only needs the `docker` CLI; every run is a throwaway, network-less container."""
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .compare import compare_results
from .logs import LOG_ROOT, run_dir, write_json, write_jsonl, write_text

ROOT = Path(__file__).resolve().parent
HARNESS = ROOT / "harness"
SENTINEL = "__HARNESS_RESULT__"
IMAGES = {"py27": "acm-sandbox/py27", "py3": "acm-sandbox/py3", "java17": "acm-sandbox/java17",
          "gcc": "acm-sandbox/gcc", "go121": "acm-sandbox/go121", "rust": "acm-sandbox/rust",
          "dotnet7": "acm-sandbox/dotnet7", "node18": "acm-sandbox/node18", "octave": "acm-sandbox/octave",
          "analysis": "acm-sandbox/analysis"}  # Layer 0 static-analysis toolchain (analysis/image)
SUITE_CMDS = {
    "py27": "python -m pytest -q -p no:cacheprovider --junitxml=/work/.acm/junit.xml",
    "py3": "python -m pytest -q -p no:cacheprovider --junitxml=/work/.acm/junit.xml",
    "java17": "mvn -o -B -q test",
}


def _hardening(mem="512m", cpus="1", pids="256"):
    # --init: tini as PID 1 reaps orphaned/zombie processes, which would otherwise exhaust --pids-limit
    return ["--init", "--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", pids, "--memory", mem, "--memory-swap", mem, "--cpus", cpus]


def _docker_run(args, stdin, timeout):
    name = "acm-" + uuid.uuid4().hex[:12]
    try:
        return subprocess.run(["docker", "run", "--rm", "-i", "--name", name, *args], input=stdin,
                              capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)  # killing the CLI doesn't stop the container
        raise


def run_cases(image, code_dir, cases, per_case_timeout=10, case_mem_mb=384, log_dir=None, log_name=None):
    """Run harness test cases against the code in `code_dir` (mounted read-only at /code).

    Each case gets `case_mem_mb` of address space (MemoryError beyond it); the container gets headroom on top.
    Results and container stderr go to <log_name>.results.jsonl / <log_name>.container.log in `log_dir`
    (default: a fresh logs/cases/<timestamp>-<image>-<dir>/).
    """
    mem = f"{case_mem_mb + 256}m"
    args = [*_hardening(mem=mem), "--read-only", "--tmpfs", "/tmp:rw,size=64m",
            "--mount", f"type=bind,src={Path(code_dir).resolve()},dst=/code,readonly",
            "--mount", f"type=bind,src={HARNESS},dst=/harness,readonly",
            IMAGES[image], "python", "/harness/runner.py", "--timeout", str(per_case_timeout),
            "--mem-mb", str(case_mem_mb)]
    p = _docker_run(args, "".join(json.dumps(c) + "\n" for c in cases),
                    timeout=30 + len(cases) * (per_case_timeout + 6))
    results = [json.loads(l[len(SENTINEL):]) for l in p.stdout.splitlines() if l.startswith(SENTINEL)]
    d, name = run_dir(log_dir, "cases", f"{image}-{Path(code_dir).name}"), log_name or image
    write_jsonl(d / f"{name}.results.jsonl", results)
    write_text(d / f"{name}.container.log", p.stderr)
    if len(results) != len(cases):
        raise RuntimeError(f"sandbox returned {len(results)}/{len(cases)} results (log: {d}):\n{p.stderr[-3000:]}")
    return results


JAVA_RUN = """cd /code && if [ -n "$ACM_SOURCES" ]; then printf '%s\\n' $ACM_SOURCES > /tmp/sources; \
else find . -name '*.java' | sort > /tmp/sources; fi && echo /harness/AcmHarness.java >> /tmp/sources \
&& javac -nowarn -encoding UTF-8 -J-Xmx384m -d /tmp/classes $ACM_SOURCEPATH @/tmp/sources 2> /tmp/javac.log; rc=$?; \
echo "__JAVAC__ $rc $(($(wc -l < /tmp/sources) - 1))"; cat /tmp/javac.log; \
if [ $rc -eq 0 ] && [ "$ACM_RUN" = 1 ]; then \
java -XX:-UsePerfData -XX:TieredStopAtLevel=1 -Xss4m -Xmx256m -cp /tmp/classes AcmHarness /tmp/classes --timeout $ACM_TIMEOUT; fi"""
JAVAC_ERROR = re.compile(r"^\./(.+?\.java):(\d+): error: (.*)$")


def parse_javac(log):
    """javac's output -> [{file, line, message, detail}], detail being the quoted code line and caret / symbol."""
    errors, lines = [], log.splitlines()
    for i, line in enumerate(lines):
        m = JAVAC_ERROR.match(line)
        if m:
            detail = []
            for nxt in lines[i + 1:i + 5]:
                if JAVAC_ERROR.match(nxt) or re.match(r"^\d+ errors?$", nxt):
                    break
                detail.append(nxt)
            errors.append({"file": m.group(1), "line": int(m.group(2)), "message": m.group(3),
                           "detail": "\n".join(detail)})
    return errors


def java_source_roots(code_dir, sources):
    """The source roots (directories holding package trees) of the given .java files, from their package lines."""
    roots = []
    for rel in sources:
        text = (Path(code_dir) / rel).read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
        parent = Path(rel).parent.as_posix()
        pkg = m.group(1).replace(".", "/") if m else ""
        root = parent[:-len(pkg)].rstrip("/") if pkg and parent.endswith(pkg) else parent
        roots.append(root or ".")
    return list(dict.fromkeys(roots))


def run_java_cases(code_dir, cases, per_case_timeout=10, log_dir=None, log_name="java", sources=None):
    """Compile the Java sources under `code_dir` (mounted read-only) together with the Java harness, then run the
    harness cases (see harness/AcmHarness.java) against them; with no cases, only compile.
    `sources` (relative .java paths): compile only these, and what they reference through -sourcepath, instead of
    every file (a legacy project whose other files need jars that are not available still runs).

    Returns {"compiled", "files", "errors": parsed javac errors, "results"}. The javac output, results and container
    stderr go to <log_name>.javac.log / .results.jsonl / .container.log in `log_dir`
    (default: a fresh logs/cases/<timestamp>-java17-<dir>/).
    """
    only = []
    if sources:
        only = ["-e", "ACM_SOURCES=" + " ".join(f"./{s}" for s in sources), "-e", "ACM_SOURCEPATH=-sourcepath " +
                ":".join(f"/code/{r}" for r in java_source_roots(code_dir, sources))]
    args = [*_hardening(mem="1g", cpus="2", pids="512"), "--read-only", "--tmpfs", "/tmp:rw,size=128m",
            "-e", f"ACM_RUN={1 if cases else 0}", "-e", f"ACM_TIMEOUT={per_case_timeout}", *only,
            "--mount", f"type=bind,src={Path(code_dir).resolve()},dst=/code,readonly",
            "--mount", f"type=bind,src={HARNESS},dst=/harness,readonly",
            IMAGES["java17"], "bash", "-c", JAVA_RUN]
    p = _docker_run(args, "".join(json.dumps(c) + "\n" for c in cases),
                    timeout=120 + len(cases) * (per_case_timeout + 2))
    d = run_dir(log_dir, "cases", f"java17-{Path(code_dir).name}")
    head, _, rest = p.stdout.partition("\n")
    m = re.match(r"__JAVAC__ (\d+) (\d+)", head)
    javac = "\n".join(l for l in rest.splitlines() if not l.startswith(SENTINEL))
    results = [json.loads(l[len(SENTINEL):]) for l in p.stdout.splitlines() if l.startswith(SENTINEL)]
    write_text(d / f"{log_name}.javac.log", javac)
    write_jsonl(d / f"{log_name}.results.jsonl", results)
    write_text(d / f"{log_name}.container.log", p.stderr)
    if not m:
        raise RuntimeError(f"java sandbox did not report a compile result (log: {d}):\n{(p.stdout + p.stderr)[-3000:]}")
    compiled = m.group(1) == "0"
    if compiled and len(results) != len(cases):
        raise RuntimeError(f"java sandbox returned {len(results)}/{len(cases)} results (log: {d}):\n{p.stderr[-3000:]}")
    return {"compiled": compiled, "files": int(m.group(2)), "errors": parse_javac(javac) if not compiled else [],
            "javac_tail": javac[-3000:] if not compiled else "", "results": results, "log_dir": str(d)}


def differential(legacy_dir, migrated_dir, cases, legacy="py27", migrated="py3", profile="py2to3",
                 per_case_timeout=10, log_dir=None):
    """Run identical cases on legacy and migrated code; return one verdict per case.

    Saves the cases, both sides' raw results, the verdicts and a summary to `log_dir`
    (default: a fresh logs/differential/<timestamp>-<legacy>-vs-<migrated>/).
    """
    d = run_dir(log_dir, "differential", f"{Path(legacy_dir).name}-vs-{Path(migrated_dir).name}")
    write_jsonl(d / "cases.jsonl", cases)
    old = run_cases(legacy, legacy_dir, cases, per_case_timeout, log_dir=d, log_name="legacy")
    new = run_cases(migrated, migrated_dir, cases, per_case_timeout, log_dir=d, log_name="migrated")
    verdicts = [compare_results(a, b, profile) for a, b in zip(old, new)]
    write_jsonl(d / "verdicts.jsonl", verdicts)
    write_json(d / "summary.json", {
        "legacy": {"image": legacy, "code": str(legacy_dir)}, "migrated": {"image": migrated, "code": str(migrated_dir)},
        "profile": profile, "cases": len(cases), "equivalent": sum(v["equivalent"] for v in verdicts),
        "inequivalent_cases": [v["case_id"] for v in verdicts if not v["equivalent"]]})
    return verdicts


# Installs a project's third-party dependencies, resolved as of EXCLUDE_NEWER (the benchmark's snapshot date),
# then removes the project's own distribution so tests exercise the code copied into /work.
PROJECT_DOCKERFILE = r"""
ARG BASE
FROM ${BASE}
ARG EXCLUDE_NEWER=
USER root
COPY . /src
RUN cd /src \
 && PIPI="uv pip install --system --no-cache ${EXCLUDE_NEWER:+--exclude-newer $EXCLUDE_NEWER}" \
 && if [ -f requirements.txt ]; then $PIPI -r requirements.txt; fi \
 # best-effort: benchmark snapshots are often pruned (e.g. setup.py lists removed scripts), so building the
 # project itself may fail even though only its dependencies are needed
 && if [ -f setup.py ] || [ -f pyproject.toml ]; then $PIPI . || echo "ACM-WARN: project build failed"; fi \
 # install lines lifted out of run_tests.sh lose their `if [ -f ... ]` guards, so each is best-effort too
 && if [ -f run_tests.sh ]; then \
      (grep -vE '^\s*#' run_tests.sh | grep -oE '(python3? -m )?pip3? install[^;&|]*' || true) \
      | sed -E "s#^(python3? -m )?pip3? install#$PIPI#; s#\$# || echo 'ACM-WARN: install line failed'#" \
      | bash; fi \
 && python -c "import importlib.metadata as m, json; print(*{d.metadata['Name'] for d in m.distributions() \
      if json.loads(d.read_text('direct_url.json') or '{}').get('url', '').startswith('file:///src')})" \
    | xargs -r uv pip uninstall --system \
 && rm -rf /src
USER sandbox
"""


def build_log_path(tag):
    """Where a per-project image build is logged: logs/build/projects/<project>-<base>-<hash>.log."""
    return LOG_ROOT / "build" / "projects" / (tag.split("/", 1)[-1].replace(":", "-") + ".log")


def build_project_image(base, project_dir, exclude_newer=None):
    """Per-project image (ReCodeAgent pattern): deps installed at build time WITH network, tests run without.

    `exclude_newer` (RFC 3339 date/timestamp) pins dependency resolution to what existed then, avoiding drift
    in unpinned requirements. Requires `uv` in the base image (py3 only).
    Images are cached by content hash; every actual build, pass or fail, is logged to build_log_path(tag).
    """
    project_dir = Path(project_dir)
    manifests = [project_dir / f for f in ("requirements.txt", "setup.py", "pyproject.toml", "setup.cfg",
                                             "run_tests.sh") if (project_dir / f).exists()]
    base_id = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", IMAGES[base]],
                             capture_output=True, text=True, check=True).stdout.strip()
    # key = template + base image id + snapshot date + dependency manifests: a changed recipe, rebuilt base or
    # changed deps never reuse a stale image
    digest = hashlib.sha256(PROJECT_DOCKERFILE.encode() + base_id.encode() + str(exclude_newer).encode() + b"".join(
        m.name.encode() + m.read_bytes() for m in manifests)).hexdigest()[:12]
    tag = f"acm-proj/{project_dir.name.lower()}:{base}-{digest}"
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        build_args = ["--build-arg", f"BASE={IMAGES[base]}"]
        if exclude_newer:
            build_args += ["--build-arg", f"EXCLUDE_NEWER={exclude_newer}"]
        start = time.time()
        p = subprocess.run(["docker", "build", "--progress=plain", "-t", tag, *build_args, "-f", "-",
                            str(project_dir)], input=PROJECT_DOCKERFILE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        log = write_text(build_log_path(tag), f"# {tag}\n# base={IMAGES[base]} exclude_newer={exclude_newer} "
                         f"returncode={p.returncode} seconds={time.time() - start:.1f}\n{p.stdout}{p.stderr}")
        if p.returncode != 0:
            causes = [l for l in p.stderr.splitlines() if re.search(r"\b(error|cause|hint):", l)]
            raise RuntimeError(f"dependency install failed for {project_dir.name} (log: {log}):\n"
                               + "\n".join(causes[:10] or p.stderr.splitlines()[-20:]))
    return tag


def _as_str(x):
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else (x or "")


def run_suite(image, project_dir, timeout=900, command=None, env=None, log_dir=None):
    """Run a project's own test suite on a pristine scratch copy and parse the JUnit XML it produces.

    `image` is a key of IMAGES or a full tag (e.g. from build_project_image).
    Saves output.log, the JUnit XML, tests.json and summary.json to `log_dir`
    (default: a fresh logs/suite/<timestamp>-<project>/); the result's "log_dir" says where.
    """
    command = command or SUITE_CMDS[image]
    d = run_dir(log_dir, "suite", Path(project_dir).name)
    work = Path(tempfile.mkdtemp(prefix="acm-suite-")) / "p"
    # Fresh copy each run: the agent can't tamper with graded tests (RepoTransBench issue #7)
    shutil.copytree(project_dir, work, ignore=shutil.ignore_patterns(
        ".pytest_cache", "__pycache__", "htmlcov", ".coverage", "target"))
    env_args = [a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")]
    args = [*_hardening(mem="2g", cpus="2", pids="1024"), *env_args,
            "--mount", f"type=bind,src={work},dst=/work", "-w", "/work",
            IMAGES.get(image, image), "bash", "-c", command]
    try:
        start = time.time()
        try:
            p = _docker_run(args, "", timeout)
        except subprocess.TimeoutExpired as e:
            write_text(d / "output.log", f"{_as_str(e.stdout)}{_as_str(e.stderr)}\nACM: timed out after {timeout}s\n")
            raise
        tests, output = parse_junit(work), p.stdout + p.stderr
        summary = (dict(Counter(t["outcome"] for t in tests)) if tests else unittest_summary(output)) or {}
        write_text(d / "output.log", output)
        for xml in _junit_files(work):
            (d / "junit").mkdir(exist_ok=True)
            shutil.copy(xml, d / "junit" / xml.name)
        write_json(d / "tests.json", tests)
        write_json(d / "summary.json", {"project": Path(project_dir).name, "image": IMAGES.get(image, image),
                                        "command": command, "returncode": p.returncode, "counts": summary,
                                        "seconds": round(time.time() - start, 1)})
        return {"returncode": p.returncode, "tests": tests, "summary": summary, "log_tail": output[-5000:],
                "log_dir": str(d)}
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def unittest_summary(output):
    """Fallback for unittest runners, which emit no JUnit XML: sum their `Ran N tests` / `FAILED (...)` lines."""
    ran = [int(n) for n in re.findall(r"^Ran (\d+) tests? in", output, re.M)]
    if not ran:
        return None
    counts = Counter()
    for detail in re.findall(r"^(?:OK|FAILED) \(([^)]*)\)", output, re.M):
        counts.update({k: int(v) for k, v in re.findall(r"(failures|errors|skipped)=(\d+)", detail)})
    failure, error, skipped = counts["failures"], counts["errors"], counts["skipped"]
    return {"passed": sum(ran) - failure - error - skipped, "failure": failure, "error": error, "skipped": skipped}


def _junit_files(root):
    return [*root.glob(".acm/junit.xml"), *root.glob("**/surefire-reports/TEST-*.xml")]


def parse_junit(root):
    """Parse JUnit XML (pytest --junitxml or Surefire) instead of regex-matching console output."""
    cases = []
    for xml in _junit_files(root):
        for tc in ET.parse(xml).getroot().iter("testcase"):
            bad = next((t for t in ("failure", "error", "skipped") if tc.find(t) is not None), None)
            cases.append({"id": f"{tc.get('classname')}::{tc.get('name')}", "outcome": bad or "passed",
                          "message": tc.find(bad).get("message") if bad else None})
    return cases
