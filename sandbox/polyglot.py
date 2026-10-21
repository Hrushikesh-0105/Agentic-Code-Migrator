"""Run differential test cases in any language of langs/ (the generic side of the sandbox).

    run_cases(language, code_dir, cases, log_dir, per_case_timeout=10) -> {"built", "errors", "results", ...}

A case is language-neutral: {"case_id", "call": {...}, ...}. The call says what to reach and how:
    kind         function | static | method | property | constructor
    module       where the code lives (a dotted Python module, a source file, a Go package dir, a Rust module path)
    class        the class / struct / type of a method or constructor
    member       the function / method name
    constructor  how a receiver is made (method / property): the constructor's name or "<class>" for the default
    args, init   type-tagged argument lists (init: the constructor's arguments); kwargs (Python only)
Results are runner.py's type-tagged records (status ok / exception / timeout / crash / unresolved / untestable),
so sandbox/compare.py can compare any two languages. "unresolved" = the API asked for is not there (a defect of
the code under test); "untestable" = the harness cannot express an argument in that language (e.g. a struct).

Adapters (one per harness kind in langs/catalog.py):
    python   sandbox/harness/runner.py             (existing; fresh interpreter per case)
    java     sandbox/harness/AcmHarness.java        (existing; reflection, fresh class loader per case)
    go       sandbox/harness/go/                    reflection over a generated registry; `go test -c` once,
                                                    one process per case
    rust     sandbox/harness/rust/                  generated driver: arguments rendered as typed literals
                                                    from the parsed signature; results printed with {:?} and
                                                    parsed here; one process per case
    cpp, c   sandbox/harness/cpp/                   generated driver (typed literals; overloaded encoders)
    csharp   sandbox/harness/csharp/AcmHarness.cs   reflection; one process per case
    node     sandbox/harness/node/acm_harness.js    require() / vm fallback; one process per case
    octave   sandbox/harness/octave/acm_harness.m   feval; one process per case
Every run is a hardened, network-less container on a scratch copy of the code.
"""
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .executor import HARNESS, IMAGES, SENTINEL, _hardening, parse_javac, run_cases as run_python, run_java_cases
from .logs import run_dir, write_json, write_jsonl, write_text

CASE_MARK = "__ACMCASE__"
BUILD_MARK = "__ACMBUILD__"


def _docker(image, work, script, timeout, env=None, mem="2g"):
    name = "acm-" + uuid.uuid4().hex[:12]
    args = ["docker", "run", "--rm", "-i", "--name", name, *_hardening(mem=mem, cpus="2", pids="1024"),
            "--tmpfs", "/tmp:rw,exec,size=512m",
            *[a for k, v in (env or {}).items() for a in ("-e", f"{k}={v}")],
            "--mount", f"type=bind,src={Path(work).resolve()},dst=/work", "-w", "/work",
            IMAGES[image], "bash", "-c", script]
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        raise


def _scratch(code_dir):
    work = Path(tempfile.mkdtemp(prefix="acm-poly-")) / "w"
    shutil.copytree(code_dir, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "target", "bin", "obj",
                                                                  "node_modules", "build", ".acm"))
    (work / ".acm").mkdir(exist_ok=True)
    return work


def _loop(n, cmd, timeout, offset=0):
    """Shell loop running `cmd` once per case (ACM_CASE = index within its batch) with a timeout; marks each case
    with its global index and exit code."""
    return (f"for i in $(seq 0 {n - 1}); do ACM_CASE=$i timeout -s KILL {int(timeout) + 2} {cmd}; "
            f"echo \"{CASE_MARK} $((i + {offset})) $?\"; done")


def _collect(stdout, cases, extra=None):
    """Results from the case loop: the SENTINEL record a case printed, or a crash / timeout record."""
    out, current = {}, []
    for line in stdout.splitlines():
        if line.startswith(SENTINEL):
            try:
                current.append(json.loads(line[len(SENTINEL):]))
            except ValueError:
                pass
        elif line.startswith(CASE_MARK):
            _, i, rc = line.split()
            i, rc = int(i), int(rc)
            rec = current[-1] if current else None
            if rec is None:
                rec = {"case_id": cases[i]["case_id"], "status": "timeout" if rc in (124, 137) else "crash",
                       "returncode": rc}
            rec["case_id"] = cases[i]["case_id"]
            out[i] = extra(rec, cases[i]) if extra else rec
            current = []
    return [out.get(i) or {"case_id": c["case_id"], "status": "crash", "message": "no result"}
            for i, c in enumerate(cases)]


def _finish(d, name, p, built, errors, results, files=None):
    write_text(d / f"{name}.build.log", p.stdout.split(BUILD_MARK)[0] if p else "")
    write_jsonl(d / f"{name}.results.jsonl", results)
    write_text(d / f"{name}.container.log", (p.stderr if p else "")[-20000:])
    return {"built": built, "compiled": built, "errors": errors, "results": results, "log_dir": str(d),
            "files": files}


# ------------------------------------------------------------------------------------------------ Go
GO_FUNC = re.compile(r"^func\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\(", re.M)
GO_METHOD = re.compile(r"^func\s+\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*([A-Za-z_]\w*)\s*\(", re.M)
GO_TYPE = re.compile(r"^type\s+([A-Za-z_]\w*)\s+struct\b", re.M)
GO_ERROR = re.compile(r"^(?:#.*\n)?\.?/?(.+?\.go):(\d+):(\d+): (.*)$", re.M)


def _go_packages(work):
    pkgs = {}
    for f in sorted(work.rglob("*.go")):
        if f.name.endswith("_test.go"):
            continue
        rel = f.parent.relative_to(work).as_posix() or "."
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^package\s+(\w+)", text, re.M)
        p = pkgs.setdefault(rel, {"name": m.group(1) if m else "main", "funcs": set(), "methods": set(),
                                  "types": set()})
        p["funcs"] |= set(GO_FUNC.findall(text))
        p["methods"] |= {f"{t}.{m}" for t, m in GO_METHOD.findall(text)}
        p["types"] |= set(GO_TYPE.findall(text))
    return pkgs


def run_go(work, cases, timeout, d):
    pkgs = _go_packages(work)
    by_pkg = {}
    for i, c in enumerate(cases):
        by_pkg.setdefault(c["call"].get("module") or ".", []).append(i)
    tmpl = (HARNESS / "go" / "acm_harness_test.go.tmpl").read_text(encoding="utf-8")
    script = ["set -u", "export GOCACHE=/opt/gocache", "rc_all=0"]
    order = []
    for pkg, idx in by_pkg.items():
        info = pkgs.get(pkg)
        if not info:
            continue
        reg = []
        for f in sorted(info["funcs"]):
            reg.append(f'\t"{f}": {f},')
        for m in sorted(info["methods"]):
            t, name = m.split(".")
            reg.append(f'\t"{m}": (*{t}).{name},')
        for t in sorted(info["types"]):
            reg.append(f'\t"<{t}>": func() *{t} {{ return &{t}{{}} }},')
        (work / pkg / "acm_harness_test.go").write_text(
            tmpl.replace("{{PKG}}", info["name"]).replace("{{REGISTRY}}", "\n".join(reg)), encoding="utf-8")
        tag = re.sub(r"\W", "_", pkg)
        write_jsonl(work / ".acm" / f"cases_{tag}.jsonl", [cases[i] for i in idx])
        script.append(f"go test -c -o /tmp/acm_{tag}.test ./{pkg} 2>&1 || rc_all=1")
        order.append((pkg, tag, idx))
    script.append(f'echo "{BUILD_MARK} $rc_all"')
    offset = 0
    for pkg, tag, idx in order:
        script.append(f"if [ -x /tmp/acm_{tag}.test ]; then export ACM_CASES=/work/.acm/cases_{tag}.jsonl; "
                      + _loop(len(idx), f"/tmp/acm_{tag}.test -test.run '^TestAcmHarness$'", timeout, offset)
                      + "; fi")
        offset += len(idx)
    p = _docker("go121", work, "\n".join(script), 300 + len(cases) * (timeout + 3))
    build_log, _, rest = p.stdout.partition(BUILD_MARK)
    built = rest.strip().startswith("0")
    errors = [{"file": m.group(1), "line": int(m.group(2)), "message": m.group(4)}
              for m in GO_ERROR.finditer(build_log)][:20]
    # results come back per package in order; re-map local indices to the global case list
    results = [None] * len(cases)
    flat = [i for _, _, idx in order for i in idx]
    local = _collect(rest, [cases[i] for i in flat])
    for gi, r in zip(flat, local):
        results[gi] = r
    for i, c in enumerate(cases):
        if results[i] is None:
            results[i] = {"case_id": c["case_id"], "status": "unresolved",
                          "message": f"package {c['call'].get('module')!r} not found"}
    return p, built, errors, results


# ------------------------------------------------------------------------------------------------ Rust
RUST_ERROR = re.compile(r"^(.+?\.rs):(\d+):(\d+): (error.*)$", re.M)


def _chunks(stdout):
    """{case index: (text the case printed, exit code)} from a case loop's output."""
    out, buf = {}, []
    for line in stdout.splitlines():
        if line.startswith(CASE_MARK):
            _, i, rc = line.split()
            out[int(i)] = ("\n".join(buf), int(rc))
            buf = []
        else:
            buf.append(line)
    return out


def _rust_module_file(work, module):
    parts = [p for p in (module or "").split("::") if p]
    if not parts:
        return work / "src" / "lib.rs"
    for cand in (work / "src" / Path(*parts).with_suffix(".rs"), work / "src" / Path(*parts) / "mod.rs"):
        if cand.exists():
            return cand
    return None


def run_rust(work, cases, timeout, d):
    from . import drivers as D
    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', (work / "Cargo.toml").read_text(encoding="utf-8"), re.M) \
        if (work / "Cargo.toml").exists() else None
    crate = (m.group(1) if m else "acm").replace("-", "_")
    sigs = {}
    for i, c in enumerate(cases):
        call = c["call"]
        f = _rust_module_file(work, call.get("module"))
        try:
            if f is None:
                raise D.Unresolved(f"module {call.get('module')} not found")
            text = f.read_text(encoding="utf-8")
            cls = call.get("class") if call["kind"] in ("method", "property", "static", "constructor") else None
            member = call.get("constructor") or "new" if call["kind"] == "constructor" else call["member"]
            sigs[i] = D.rust_signature(text, member, cls)
            if call["kind"] in ("method", "property"):
                sigs[("ctor", i)] = D.rust_signature(text, call.get("constructor") or "new", call["class"])
        except (D.Unresolved, D.Untestable) as e:
            sigs[i] = e
    skipped_all, p, built, errors, outs = {}, None, False, [], {}
    for _ in range(3):
        # cases already known to be inexpressible are passed as their exception, so the driver skips them
        src, spans, skipped = D.rust_driver(cases, {**sigs, **skipped_all}, crate)
        skipped_all.update(skipped)
        (work / "tests").mkdir(exist_ok=True)
        (work / "tests" / "acm_driver.rs").write_text(src, encoding="utf-8")
        n = len(cases)
        script = "\n".join([
            "set -u", "cargo test --offline --no-run --test acm_driver --message-format=short 2>&1; rc=$?",
            f'echo "{BUILD_MARK} $rc"',
            "bin=$(ls /tmp/target/debug/deps/acm_driver-* 2>/dev/null | grep -v '\\.d$' | head -1)",
            'if [ $rc -eq 0 ] && [ -n "$bin" ]; then '
            + _loop(n, '"$bin" acm_case --exact --nocapture --test-threads=1', timeout) + "; fi"])
        p = _docker("rust", work, script, 600 + n * (timeout + 3))
        build_log, _, rest = p.stdout.partition(BUILD_MARK)
        built = rest.strip().startswith("0")
        errors = [{"file": m.group(1), "line": int(m.group(2)), "message": m.group(4)}
                  for m in RUST_ERROR.finditer(build_log)]
        driver_errs = [e for e in errors if e["file"].endswith("acm_driver.rs")]
        if built or not driver_errs or len(driver_errs) < len(errors):
            outs = _chunks(rest)
            errors = [e for e in errors if not e["file"].endswith("acm_driver.rs")][:20]
            break
        bad = {i for e in driver_errs for i, (a, b) in spans.items() if a <= e["line"] <= b}
        if not bad:
            break
        for i in bad:
            skipped_all[i] = D.Untestable("the harness could not express this call: " +
                                          "; ".join(e["message"] for e in driver_errs
                                                    if spans[i][0] <= e["line"] <= spans[i][1])[:300])
    results = []
    for i, c in enumerate(cases):
        if i in skipped_all:
            e = skipped_all[i]
            results.append({"case_id": c["case_id"], "status": "unresolved" if isinstance(e, D.Unresolved)
                            else "untestable", "message": str(e)})
            continue
        text, rc = outs.get(i, ("", -1))
        rec = None
        for line in text.splitlines():
            if line.startswith("__ACMRUST__"):
                rec = json.loads(line[len("__ACMRUST__"):])
        if rec is None:
            results.append({"case_id": c["case_id"], "status": "timeout" if rc in (124, 137) else "crash",
                            "returncode": rc, "stderr": text[-2000:]})
            continue
        body = text.split("__ACMOUT_BEGIN__", 1)[-1].split("__ACMOUT_END__", 1)[0]
        r = D.rust_result(rec, sigs[i])
        r.update(case_id=c["case_id"], stdout=body.strip("\n") + ("\n" if body.strip("\n") else ""), stderr="")
        results.append(r)
    return p, built, errors, results


# ------------------------------------------------------------------------------------------------ C / C++
GCC_ERROR = re.compile(r"^(?:\./)?(.+?):(\d+):(\d+): (?:fatal )?error: (.*)$", re.M)
SOURCES = {"cpp": (".cpp", ".cc", ".cxx", ".c++"), "c": (".c",)}
HEADERS = (".h", ".hpp", ".hh", ".hxx", ".inl")
MAIN = re.compile(r"\bint\s+main\s*\(")


def _excluded_unit(path):
    p = path.as_posix().lower()
    return any(x in p for x in ("/test/", "/tests/", "test_", "_test.", "/example", "/bench", "/demo")) or \
        p.startswith(("test/", "tests/", "examples/", "benchmark"))


def _driver_records(text, rc, c):
    rec = None
    for line in text.splitlines():
        if line.startswith(SENTINEL):
            try:
                rec = json.loads(line[len(SENTINEL):])
            except ValueError:
                pass
    if rec is None:
        return {"case_id": c["case_id"], "status": "timeout" if rc in (124, 137) else "crash", "returncode": rc,
                "stderr": text[-2000:]}
    body = text.split("__ACMOUT_BEGIN__", 1)[-1].split("__ACMOUT_END__", 1)[0]
    rec.update(case_id=c["case_id"], stdout=body.strip("\n") + ("\n" if body.strip("\n") else ""), stderr="")
    return rec


def run_c_family(work, cases, timeout, d, lang="cpp", standalone=False):
    from . import drivers_c as D
    from .drivers import Unresolved, Untestable
    sigs, includes = {}, set()
    for i, c in enumerate(cases):
        call = c["call"]
        f = work / (call.get("module") or "")
        try:
            if not call.get("module") or not f.is_file():
                raise Unresolved(f"file {call.get('module')} not found")
            text = f.read_text(encoding="utf-8", errors="replace")
            cls = call.get("class") if call["kind"] in ("method", "property", "static", "constructor") else None
            member = cls if call["kind"] == "constructor" else call["member"]
            sigs[i] = D.signature(text, member, cls, lang)
            if call["kind"] in ("method", "property"):
                try:
                    sigs[("ctor", i)] = D.signature(text, call["class"], call["class"], lang)
                except Unresolved:
                    sigs[("ctor", i)] = {"params": [], "qualifier": sigs[i]["qualifier"], "returns": ""}
            includes.add(f.relative_to(work).as_posix())
        except (Unresolved, Untestable) as e:
            sigs[i] = e
    srcs = [p for p in sorted(work.rglob("*")) if p.suffix in SOURCES[lang] and p.is_file()
            and ".acm" not in p.parts and p.relative_to(work).as_posix() not in includes]
    units = []
    for p in srcs:
        rel = p.relative_to(work)
        if standalone or _excluded_unit(rel) or MAIN.search(p.read_text(encoding="utf-8", errors="replace")):
            continue
        units.append(rel.as_posix())
    hdr_dirs = sorted({p.parent.relative_to(work).as_posix() for p in work.rglob("*")
                       if p.suffix in HEADERS and ".acm" not in p.parts})
    idirs = " ".join(f"-I'/work/{x}'" for x in ["."] + [h for h in hdr_dirs if h != "."])
    shutil.copy(HARNESS / lang / ("acm_enc.hpp" if lang == "cpp" else "acm_enc.h"), work / ".acm")
    compiler = "g++ -std=c++17" if lang == "cpp" else "gcc -std=gnu11"
    ext = ".cpp" if lang == "cpp" else ".c"
    gen = D.cpp_driver if lang == "cpp" else D.c_driver
    skipped_all, outs, p, built, errors = {}, {}, None, False, []
    for _ in range(3):
        src, spans, skipped = gen(cases, {**sigs, **skipped_all}, [f"../{x}" for x in sorted(includes)])
        skipped_all.update(skipped)
        (work / ".acm" / f"acm_driver{ext}").write_text(src, encoding="utf-8")
        units_s = " ".join(f"'{u}'" for u in units)
        script = "\n".join([
            "set -u",
            f"{compiler} -O0 -w -fpermissive -I/work/.acm {idirs} .acm/acm_driver{ext} {units_s} -o /tmp/acm_drv"
            f"{' -lm' if lang == 'c' else ''} 2>&1 | head -c 200000; rc=${{PIPESTATUS[0]}}",
            f'echo "{BUILD_MARK} $rc"',
            "if [ $rc -eq 0 ]; then " + _loop(len(cases), "/tmp/acm_drv", timeout) + "; fi"])
        if lang == "c":
            script = script.replace(" -fpermissive", "")
        p = _docker("gcc", work, script, 600 + len(cases) * (timeout + 3))
        build_log, _, rest = p.stdout.partition(BUILD_MARK)
        built = rest.strip().startswith("0")
        errors = [{"file": m.group(1), "line": int(m.group(2)), "message": m.group(4)}
                  for m in GCC_ERROR.finditer(build_log)]
        driver_errs = [e for e in errors if e["file"].endswith(f"acm_driver{ext}")]
        if built or not driver_errs:
            outs = _chunks(rest)
            errors = errors[:20]
            break
        bad = {i for e in driver_errs for i, (a, b) in spans.items() if a <= e["line"] <= b}
        if not bad:
            errors = errors[:20]
            break
        for i in bad:
            skipped_all[i] = Untestable("the harness could not express this call: " + "; ".join(
                e["message"] for e in driver_errs if spans[i][0] <= e["line"] <= spans[i][1])[:300])
    results = []
    for i, c in enumerate(cases):
        if i in skipped_all:
            e = skipped_all[i]
            results.append({"case_id": c["case_id"], "status": "unresolved" if isinstance(e, Unresolved)
                            else "untestable", "message": str(e)})
            continue
        text, rc = outs.get(i, ("", -1))
        results.append(_driver_records(text, rc, c))
    return p, built, errors, results


def run_cpp(work, cases, timeout, d, standalone=False):
    return run_c_family(work, cases, timeout, d, "cpp", standalone)


def run_c(work, cases, timeout, d, standalone=False):
    return run_c_family(work, cases, timeout, d, "c", standalone)


# ------------------------------------------------------------------------------------------------ reflective
CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType><TargetFramework>net7.0</TargetFramework><Nullable>disable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings><AssemblyName>acm</AssemblyName><StartupObject>AcmHarness</StartupObject>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems><TreatWarningsAsErrors>false</TreatWarningsAsErrors>
    <NoWarn>$(NoWarn);CS8632;CS0168;CS0219;CS1998;CS0162;CS0649;CS0414</NoWarn>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo><SatelliteResourceLanguages>en</SatelliteResourceLanguages>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="**/*.cs" Exclude="**/obj/**;**/bin/**;**/*Test.cs;**/*Tests.cs;**/*Tests/**;**/*Test/**;**/TestResults/**" />
  </ItemGroup>
</Project>
"""
CS_ERROR = re.compile(r"^(?:/work/)?(.+?\.cs)\((\d+),(\d+)\): error (\w+): (.*?)(?: \[.*\])?$", re.M)


CS_PROJECT_FILES = ("*.csproj", "*.sln", "global.json", "Directory.Build.props", "Directory.Build.targets",
                    "Directory.Packages.props", "NuGet.Config", "nuget.config")


def _set_aside_cs(work):
    """The project's own build files would pick another SDK (global.json), packages or settings: our csproj
    builds its sources with the sandbox's .NET 7."""
    for pattern in CS_PROJECT_FILES:
        for p in list(work.rglob(pattern)):
            p.rename(p.with_name(p.name + ".orig"))


def run_csharp(work, cases, timeout, d):
    _set_aside_cs(work)
    shutil.copy(HARNESS / "csharp" / "AcmHarness.cs", work / ".acm" / "AcmHarness.cs")
    (work / "acm.csproj").write_text(CSPROJ, encoding="utf-8")
    write_jsonl(work / ".acm" / "cases.jsonl", cases)
    script = "\n".join([
        "set -u", "export ACM_CASES=/work/.acm/cases.jsonl",
        "dotnet build acm.csproj -c Release -o /tmp/acmout --nologo -v q -clp:NoSummary 2>&1 | head -c 200000; "
        "rc=${PIPESTATUS[0]}", f'echo "{BUILD_MARK} $rc"',
        "if [ $rc -eq 0 ]; then " + _loop(len(cases), "dotnet /tmp/acmout/acm.dll", timeout + 5) + "; fi"])
    p = _docker("dotnet7", work, script, 900 + len(cases) * (timeout + 8), env={"DOTNET_CLI_HOME": "/tmp"})
    build_log, _, rest = p.stdout.partition(BUILD_MARK)
    built = rest.strip().startswith("0")
    errors = []
    for m in CS_ERROR.finditer(build_log):
        e = {"file": m.group(1), "line": int(m.group(2)), "message": f"{m.group(4)}: {m.group(5)}"}
        if e not in errors:
            errors.append(e)
    return p, built, errors[:20], _collect(rest, cases)


def run_node(work, cases, timeout, d):
    shutil.copy(HARNESS / "node" / "acm_harness.js", work / ".acm" / "acm_harness.js")
    write_jsonl(work / ".acm" / "cases.jsonl", cases)
    script = "\n".join(["set -u", "export ACM_CASES=/work/.acm/cases.jsonl", f'echo "{BUILD_MARK} 0"',
                        _loop(len(cases), "node /work/.acm/acm_harness.js", timeout)])
    p = _docker("node18", work, script, 120 + len(cases) * (timeout + 3))
    return p, True, [], _collect(p.stdout.partition(BUILD_MARK)[2], cases)


def _octave_args(v):
    """Bytes as a list of ints (Octave cannot decode base64 portably)."""
    if isinstance(v, dict) and v.get("t") == "bytes":
        import base64
        return {"t": "u8", "v": list(base64.b64decode(v["v"]))}
    if isinstance(v, dict) and isinstance(v.get("v"), list):
        return {**v, "v": [_octave_args(x) for x in v["v"]]}
    return v


def run_octave(work, cases, timeout, d):
    shutil.copy(HARNESS / "octave" / "acm_harness.m", work / ".acm" / "acm_harness.m")
    conv = [{**c, "call": {**c["call"], "args": [_octave_args(a) for a in c["call"].get("args", [])]}} for c in cases]
    write_jsonl(work / ".acm" / "cases.jsonl", conv)
    script = "\n".join([
        "set -u", "export ACM_CASES=/work/.acm/cases.jsonl", f'echo "{BUILD_MARK} 0"',
        _loop(len(cases), "octave --no-gui --no-window-system --norc -q --eval "
                          "\"addpath(genpath('/work')); cd /work; source('/work/.acm/acm_harness.m')\"", timeout + 5)])
    p = _docker("octave", work, script, 120 + len(cases) * (timeout + 8))
    return p, True, [], _collect(p.stdout.partition(BUILD_MARK)[2], cases)


# ------------------------------------------------------------------------------------------------ dispatch
ADAPTERS = {"go": run_go, "rust": run_rust, "cpp": run_cpp, "c": run_c, "csharp": run_csharp, "node": run_node,
            "octave": run_octave}


def run_cases(language, code_dir, cases, log_dir=None, per_case_timeout=10, log_name="run", sources=None):
    """Run neutral cases on the code in `code_dir` written in `language` (a langs id or alias). `sources`: the
    files under test (Java: compile only them and what they reference; C / C++: if the project's other units do
    not build, retry with only these)."""
    from langs import get
    lang = get(language)
    d = run_dir(log_dir, "cases", f"{lang.id}-{Path(code_dir).name}")
    if lang.harness == "python":
        image = lang.toolchain["image"]
        res = run_python(image, code_dir, [to_python_case(c) for c in cases], per_case_timeout, log_dir=d,
                         log_name=log_name)
        return {"built": True, "compiled": True, "errors": [], "results": res, "log_dir": str(d)}
    if lang.harness == "java":
        r = run_java_cases(code_dir, [to_java_case(c) for c in cases], per_case_timeout, log_dir=d, log_name=log_name,
                           sources=sources)
        return {**r, "built": r["compiled"]}
    adapter = ADAPTERS.get(lang.harness)
    if adapter is None:
        raise NotImplementedError(f"no differential harness for {lang.name} yet")
    work = _scratch(code_dir)
    try:
        start = time.time()
        p, built, errors, results = adapter(work, cases, per_case_timeout, d)
        if not built and lang.harness in ("c", "cpp") and errors and not any(
                e["file"].endswith(".acm/acm_driver.c" if lang.harness == "c" else ".acm/acm_driver.cpp")
                or any(e["file"].endswith(c["call"].get("module") or "\0") for c in cases) for e in errors):
            # the project's other translation units do not build here (missing libraries): the files under test alone
            p, built, errors, results = adapter(work, cases, per_case_timeout, d, standalone=True)
        out = _finish(d, log_name, p, built, errors, results)
        write_json(d / f"{log_name}.summary.json", {"language": lang.id, "built": built, "cases": len(cases),
                                                    "seconds": round(time.time() - start, 1)})
        return out
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)


def to_python_case(c):
    """Neutral case -> runner.py's format."""
    call = c["call"]
    if "python" in c:
        return c["python"]
    target = call["member"] if call["kind"] in ("function",) else \
        call["class"] if call["kind"] == "constructor" else f"{call['class']}.{call['member']}"
    py = {"case_id": c["case_id"], "module": call["module"], "callable": target, "args": call.get("args", []),
          "kwargs": call.get("kwargs", {})}
    if call["kind"] in ("method", "property"):
        py["init"] = {"args": call.get("init", []), "kwargs": call.get("init_kwargs", {})}
    if call["kind"] == "property":
        py["attr"], py["args"], py["kwargs"] = True, [], {}
    if c.get("trace"):
        py["trace"] = c["trace"]
    return py


def to_java_case(c):
    if "java" in c and "call" not in c:
        return c
    call = c["call"]
    kind = {"function": "static", "static": "static", "constructor": "new"}.get(call["kind"], "instance")
    j = {"class": call["class"], "kind": kind, "member": call.get("member"), "args": call.get("args", []),
         "init": call.get("init", [])}
    if call["kind"] == "property":
        j["args"] = []
    return {"case_id": c["case_id"], "java": j}


# ------------------------------------------------------------------------------------------------ build check
PY_ERROR = re.compile(r"^ACMPY (.+?):(\d+): (.*)$", re.M)
NODE_ERROR = re.compile(r"^(?:/work/)?(.+?\.js):(\d+)\n(?:.*\n){0,3}?(\w*Error: .*)$", re.M)
CSPROJ_LIB = CSPROJ.replace("<OutputType>Exe</OutputType>", "<OutputType>Library</OutputType>") \
    .replace("<StartupObject>AcmHarness</StartupObject>", "")
_INC = "INC=$(find . -name '*.h' -o -name '*.hpp' | grep -v '^./.acm' | xargs -r -n1 dirname | sort -u | sed 's/^/-I/' | tr '\n' ' '); "
BUILD_SCRIPTS = {
    "python": "python - <<'EOF'\nimport pathlib\nfor p in sorted(pathlib.Path('.').rglob('*.py')):\n    try:\n"
              "        compile(p.read_bytes(), str(p), 'exec')\n    except SyntaxError as e:\n"
              "        print(f'ACMPY {p.as_posix()}:{e.lineno or 0}: {e.msg}')\nEOF\n(exit 0)",
    "go": "export GOCACHE=/opt/gocache GOFLAGS=-mod=mod; go build ./... 2>&1 && go vet ./... 2>&1",
    "rust": "cargo build --offline --message-format=short 2>&1",
    "cpp": _INC + "rc=0; for f in $(find . -name '*.hpp' -o -name '*.h' -o -name '*.cpp' -o -name '*.cc' | "
                  "grep -v '^./.acm' | sort); do g++ -std=c++17 -fsyntax-only -x c++ $INC \"$f\" 2>&1 || rc=1; done; "
                  "(exit $rc)",
    "c": _INC + "rc=0; for f in $(find . -name '*.c' | grep -v '^./.acm' | sort); do gcc -std=gnu11 -fsyntax-only "
                "$INC \"$f\" 2>&1 || rc=1; done; (exit $rc)",
    "csharp": "dotnet build acm.csproj -c Release -o /tmp/acmout --nologo -v q -clp:NoSummary 2>&1",
    "node": "rc=0; for f in $(find . -name '*.js' -not -path './node_modules/*' | sort); do node --check \"$f\" 2>&1 "
            "|| rc=1; done; (exit $rc)",
}
BUILD_IMAGE = {"python": "py3", "go": "go121", "rust": "rust", "cpp": "gcc", "c": "gcc", "csharp": "dotnet7",
               "node": "node18"}


def build_tree(language, code_dir, log_dir=None, timeout=600):
    """Does the code in `code_dir` (a target tree in `language`) build? Compiles it with the language's toolchain in
    the network-less sandbox, without running anything. -> {"built", "errors": [{file, line, message}],
    "tool", "log_dir"}. Matlab / Octave has no compile step (built = True)."""
    from langs import get
    lang = get(language)
    d = run_dir(log_dir, "build", f"{lang.id}-{Path(code_dir).name}")
    if lang.harness == "java":
        r = run_java_cases(code_dir, [], log_dir=d, log_name="build")
        return {"built": r["compiled"], "errors": r["errors"][:20], "tool": "javac 17", "log_dir": str(d)}
    kind = lang.harness
    if kind == "octave":
        return {"built": True, "errors": [], "tool": None, "log_dir": str(d), "note": "no compile step"}
    work = _scratch(code_dir)
    try:
        if kind == "csharp":
            _set_aside_cs(work)
            (work / "acm.csproj").write_text(CSPROJ_LIB, encoding="utf-8")
        script = BUILD_SCRIPTS[kind] + f"\necho \"{BUILD_MARK} $?\""
        start = time.time()
        p = _docker(BUILD_IMAGE[kind], work, script, timeout, env={"DOTNET_CLI_HOME": "/tmp"} if kind == "csharp" else None)
        log, _, rest = p.stdout.partition(BUILD_MARK)
        pattern = {"python": PY_ERROR, "go": GO_ERROR, "rust": RUST_ERROR, "cpp": GCC_ERROR, "c": GCC_ERROR,
                   "csharp": CS_ERROR, "node": NODE_ERROR}[kind]
        errors = []
        for m in pattern.finditer(log):
            if kind == "go":
                e = {"file": m.group(1), "line": int(m.group(2)), "message": m.group(4)}
            elif kind == "csharp":
                e = {"file": m.group(1), "line": int(m.group(2)), "message": f"{m.group(4)}: {m.group(5)}"}
            elif kind in ("python", "node"):
                e = {"file": m.group(1).lstrip("./"), "line": int(m.group(2)), "message": m.group(3)}
            else:
                e = {"file": m.group(1).lstrip("./") if m.group(1).startswith("./") else m.group(1),
                     "line": int(m.group(2)), "message": m.group(4)}
            if e not in errors:
                errors.append(e)
        built = rest.strip().startswith("0") and not (kind == "python" and errors)
        write_text(d / "build.log", log)
        write_text(d / "build.container.log", p.stderr[-20000:])
        write_json(d / "build.summary.json", {"language": lang.id, "built": built, "errors": len(errors),
                                              "seconds": round(time.time() - start, 1)})
        return {"built": built, "errors": errors[:20], "tool": BUILD_IMAGE[kind], "log_dir": str(d),
                "log_tail": log[-3000:] if not built and not errors else ""}
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)
