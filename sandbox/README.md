# Execution sandbox (Phase 1)

Dockerized runners for legacy and migrated code, plus a behavioural comparator. This is the **Executor & Sandbox** component from the proposal. The Test-Generation, Verdict and Repair agents (Phases 2–3) call it to run code safely and get back structured results.

The environment is derived from RepoTransBench's `docker/Dockerfile` (Ubuntu 22.04 + OpenJDK 17 + Maven + Python 3.11). The differential protocol is inspired by TransAgent's execution alignment. The per-project container pattern comes from ReCodeAgent/MatchFixAgent.

## Contents

| file | role |
|---|---|
| `executor.py` | Host side. Starts containers with `docker run`, feeds them test cases, and collects results. Entry points: `run_cases`, `differential`, `build_project_image`, `run_suite`. |
| `harness/runner.py` | Runs **inside** the container. Imports the code under test, calls one function per case and encodes the result as type-tagged JSON. It is valid Python 2.7 and 3.x. |
| `compare.py` | Host side. `compare_results` diffs a legacy result against a migrated result and returns a verdict. |
| `images/{py27,py3,java17}/Dockerfile` | The three base images. |
| `images/java17/warmup/` | A tiny Maven project, built once at image build time so JUnit 5 and Mockito are cached for offline builds. |
| `examples/calc/` | A Python 2 module and two Python 3 migrations of it (`py3_naive` has the classic bugs, `py3_fixed` doesn't), plus test cases. |
| `examples/probes/` | Deliberately hostile functions (fork bomb, memory hog, network access, writing to `/code`) used to prove the isolation holds. |
| `acceptance.py` | 8 end-to-end checks. This is the "is the sandbox working?" test. |
| `rtb_smoke.py` | Runs N RepoTransBench projects' own test suites and reports which run green. |
| `logs.py` | Creates the timestamped run folders under `../logs/` that every command above writes to. |

| image | purpose |
|---|---|
| `acm-sandbox/py27` | legacy side: CPython 2.7.18, pytest 4.6 |
| `acm-sandbox/py3` | migrated side: CPython 3.11, pytest 8, pytest-mock, pytest-asyncio, uv |
| `acm-sandbox/java17` | Python→Java side: OpenJDK 17, Maven 3.6.3, pre-warmed `~/.m2` (JUnit 5, Mockito) for offline builds |
| `acm-proj/<project>:<base>-<hash>` | built on demand: a base image plus one project's third-party deps |
| `acm-sandbox/analysis` | Layer 0 static-analysis toolchain (LibCST, tree-sitter, pyan3, pyright, ...). Built by `build_images` from `analysis/image/`; see [../analysis/README.md](../analysis/README.md) |

## 1. Set up and verify

### Prerequisites

- Docker Desktop, with the engine running. Check with `docker version`: it must print a **Server** section.
- Python 3.8+ on the host, standard library only. `executor.py` needs only the `docker` CLI.
- About 1.2 GB of disk for the three base images, plus about 300 MB–750 MB for each per-project image.
- Run every command from the **repository root** (the directory containing `sandbox/`), because the modules are run with `python -m sandbox.<name>`.

### Build the images

```bash
python -m sandbox.build_images            # all three; takes ~5–10 min the first time
python -m sandbox.build_images py3        # or just one
```

Network access is only needed here (and when building per-project images). No container that runs code under test has network access.

### Get the benchmark (needed for check 8 and `rtb_smoke`)

`benchmarks/` is git-ignored, so each machine needs its own copy:

```bash
mkdir -p benchmarks/repotransbench
wget https://github.com/DeepSoftwareAnalytics/RepoTransBench/releases/download/v1.0/projects.tar.gz
tar -xzf projects.tar.gz -C benchmarks/repotransbench \
    'source_projects/Python' 'target_projects/Python/Java' 'target_projects/projects_summary.jsonl'
```

Expected result: `benchmarks/repotransbench/source_projects/Python/` (171 projects) and `benchmarks/repotransbench/target_projects/Python/Java/` (169 projects).

### Run the acceptance suite

```bash
python -m sandbox.acceptance              # ~2 min once images exist; exits non-zero on any failure
python -m sandbox.acceptance --skip-rtb   # without the dataset
```

A healthy sandbox prints:

```
[PASS] images: real Py2.7, Py3.11, JDK17+Maven
[PASS] differential: naive Py3 migration flags hazards
       verdicts={'avg-1': False, 'avg-empty': True, 'shout-1': True, 'evens-1': False, ...}
[PASS] differential: correct Py3 migration is equivalent
[PASS] isolation: py27 sandbox
       all 8 properties hold
[PASS] isolation: py3 sandbox
[PASS] determinism: repeated run is identical
[PASS] suite mode: JUnit5+Mockito offline in java17
[PASS] suite mode: RepoTransBench regression projects green in py3
       ok  alastair_python-musicbrainzngs: {'passed': 135}
       ok  alingse_jsoncsv: {'passed': 41}
       ok  7ossam81_EvoloPy: {'passed': 78, 'skipped': 1}

8/8 checks passed
```

What each check proves:

| # | check | proves |
|---|---|---|
| 1 | images | Each image runs the real interpreter: `1/2` is `0` on 2.7 and `0.5` on 3.11, and Java is 17 with Maven present. |
| 2 | naive migration | The comparator **catches** real Py2→3 bugs. `average` (`/` now returns a float) and `evens` (`filter` now returns an iterator) are flagged as inequivalent. |
| 3 | fixed migration | The comparator produces **no false alarms** on a correct migration. `shout` also passes: `'HI!'` in Python 2 bytes matches `'HI!'` in Python 3 text, and the match is logged as a hazard note. |
| 4–5 | isolation | No network, read-only code, a PID limit, a memory limit (`MemoryError`), a fresh interpreter and cwd per case, and recovery after a fork bomb or out-of-memory. Checked on both Python images. |
| 6 | determinism | Running the same cases twice gives byte-identical results. Required before a mismatch can be trusted. |
| 7 | Java offline | `mvn -o test` runs JUnit 5 and Mockito tests with no network. |
| 8 | RepoTransBench | Three real benchmark projects pass their own test suites inside the sandbox. |

### Optional: benchmark coverage

```bash
python -m sandbox.rtb_smoke 30            # first 30 projects -> logs/rtb_smoke/<timestamp>/summary.json
```

The report records each project's image and the paths to its build log and suite logs. Past reports are in `logs/rtb_smoke/`. The first recorded run (2026-09-24) had **16/30 green**. The red projects come from environment gaps, not sandbox bugs: some scripts create their own venv, some install packages at test time, some need `poetry`/`tox`, some tests need network, some dependencies are missing. Each red row is labelled with its cause. A red project in this report does not mean the sandbox is broken.

### Where the results go

Every command writes a timestamped folder under `logs/` at the repository root and prints its path at the end:
- `build_images` → `logs/build/<ts>-base/`, one `.log` per image plus `summary.json`. Per-project image builds go to `logs/build/projects/<project>-py3-<hash>.log`, whether they pass or fail.
- `acceptance` → `logs/acceptance/<ts>/`: the console transcript, `summary.json`, and each check's raw results.
- `rtb_smoke` → `logs/rtb_smoke/<ts>/`: `summary.json`, plus each project's full test output.
- Calling `run_suite`, `differential` or `run_cases` from Python logs to `logs/suite/`, `logs/differential/` or `logs/cases/`. Pass `log_dir=...` to put a call's logs somewhere specific. `run_suite` returns the folder it used as `result["log_dir"]`.

For the full layout and which files are committed to git, see [../logs/README.md](../logs/README.md).

## 2. How it works

### What a container run looks like

Every run is a throwaway container: `docker run --rm` with hardening flags. Nothing persists between runs.

```
host: executor.py                                container (acm-sandbox/py27 or py3)
─────────────────                                ──────────────────────────────────────
cases (JSONL) ──stdin──▶ python /harness/runner.py
                           for each case:
                             fork fresh interpreter, fresh cwd in /tmp
                             import <module> from /code (read-only mount)
                             call <callable>(*args, **kwargs) under timeout + RLIMIT_AS
                             encode return value, exception, mutated args, self, stdout
          ◀──stdout── __HARNESS_RESULT__{json}   (one line per case)
compare.py: legacy result vs migrated result ──▶ verdict
```

### Two execution modes

**Differential** (`differential`, `run_cases`) runs identical test cases against the legacy code and the migrated code, then compares their behaviour. This is how the proposal's equivalence verification works.

```python
from sandbox import differential
verdicts = differential("legacy/pkg", "migrated/pkg", cases)  # py27 vs py3, profile "py2to3"
```

Test cases are JSON objects, typically produced by the Test-Generation agent:

```json
{"case_id": "avg-1", "module": "calc", "callable": "average", "args": [[1, 2, 3, 4]]}
{"case_id": "c-1", "module": "calc", "callable": "Counter.bump", "init": {"args": [10]}, "kwargs": {"by": 5}}
{"case_id": "b-1", "module": "codec", "callable": "decode", "args": [{"t": "bytes", "v": "aGk="}]}
```

- `module` is imported from the code directory. `callable` is `func` or `Class.method`, and with `Class.method` the `init` field builds a fresh instance. `seed` (default 0) seeds `random`.
- A plain JSON string becomes the interpreter's native `str`: a byte string on Python 2, text on Python 3.
- To force a type, tag the value, e.g. `{"t": "bytes" | "text" | "tuple" | "set" | "int" | ...}`.

Each case produces a result with:
- `status`: `ok`, `exception`, `timeout` or `crash`;
- a type-tagged return `value` (so a Python 2 `str` shows up as `bytes`, and a Python 3 `map` shows up as an `iterator`);
- `exception` (type, message and traceback);
- `args_after` and `self_after`, which capture in-place mutation;
- `stdout` and `stderr`.

`compare_results` turns a legacy/migrated pair of results into:

```json
{"equivalent": false, "diffs": [{"path": "return", "kind": "type", "legacy": {"t": "int", "v": "2"},
  "migrated": {"t": "float", "v": 2.5}}], "hazard_notes": [], "migrated_traceback": null}
```

Two cases are equivalent only when all of these match:
- the status;
- the exception type (not the message);
- the return value;
- the arguments after the call;
- the instance state after the call;
- stdout.

`stderr` and timing are ignored.

The comparator has two profiles:
- **`py2to3`:**
  - A Python 2 `str` matches Python 3 text if it decodes as UTF-8, and the match is recorded in `hazard_notes`.
  - A list vs an iterator (`map`/`filter`/`zip`/`range`) is a mismatch.
  - `int` and `long` are the same type.
  - Dicts and sets are compared regardless of order.
  - Floats use `isclose(rel_tol=1e-9)`.
- **`strict`:** types must match exactly.

**Suite** (`run_suite`, `build_project_image`) runs a project's own test suite and parses its JUnit XML (from pytest or Surefire). This is how RepoTransBench itself grades a translation, and it gives the "Legacy Test Pass Rate" metric.
- Each run uses a pristine copy of the project, so an agent can't edit graded tests.
- For RepoTransBench, dependencies are resolved as of each project's `test_summary.json` timestamp (`uv --exclude-newer`), so unpinned requirements don't drift.
- Dependencies are installed **at image build time, with network access**. Tests then run with `--network none`.

## 3. Verifying your own codebase

### Differential: legacy repo vs its migrated equivalent

You need three things:

```
my_legacy/            # importable Python 2 code, e.g. my_legacy/mypkg/__init__.py ...
my_migrated/          # the Python 3 migration, with the same module paths
cases.jsonl           # one case per line (format above)
```

```python
import json
from sandbox import differential

cases = [json.loads(l) for l in open("cases.jsonl") if l.strip()]
for v in differential("my_legacy", "my_migrated", cases):
    print(v["case_id"], "OK" if v["equivalent"] else v["diffs"])
```

What happens inside:
1. A `py27` container starts, and `my_legacy/` is mounted read-only at `/code`.
2. Each case runs in its own subprocess, and the results are streamed back.
3. The container is removed.
4. Steps 1–3 are repeated with a `py3` container and `my_migrated/`.
5. The two lists of results are compared pairwise on the host.

Requirements and caveats:
- Module names must be identical on both sides, because both sides run the same `module` + `callable`.
- The code must be importable with the base image's packages only, because differential containers have no network and install nothing. If the code needs third-party packages, build an image with them first, add it to `IMAGES` in `executor.py`, and pass that key as `legacy=`/`migrated=`. `build_project_image` automates this for Python 3. Python 2 needs a hand-written Dockerfile `FROM acm-sandbox/py27`.
- Values must be JSON-encodable inputs. Arguments that are objects, file handles or sockets must be constructed inside the code under test (for example, test through a wrapper function).
- The code must be deterministic. `random` is seeded per case. Time, hash order of unsorted output, and so on, must be controlled by the case or they will show up as diffs.

### Suite: run a project's own tests

```python
from sandbox.executor import SUITE_CMDS, build_project_image, run_suite

img = build_project_image("py3", "path/to/project")          # reads requirements.txt / setup.py / pyproject.toml
r = run_suite(img, "path/to/project", command=SUITE_CMDS["py3"] + " tests")  # a full tag needs an explicit command
print(r["summary"], r["returncode"])                         # e.g. {'passed': 41}
```

Name the test directory explicitly. Plain `pytest` also collects RepoTransBench's `public_tests/`, and some of those fail even against the original source: `alingse_jsoncsv` gives 41/41 on `tests/` but 4 failures in `public_tests/`. For benchmark projects, also pass `exclude_newer=snapshot_date(path)` (from `sandbox.rtb_smoke`) so dependency versions match the benchmark's.

For Java, the project directory needs a `pom.xml`: use `images/java17/warmup/pom.xml` as the template. Tests go under `src/test/java`. Then call `run_suite("java17", "path/to/java_project")`, which runs `mvn -o -B -q test`. Only dependencies already in the image's `~/.m2` (JUnit 5, Mockito) resolve offline. A project needing others (e.g. Jackson) needs them added to the warm-up pom and the image rebuilt.

## 4. The RepoTransBench layout

```
benchmarks/repotransbench/
├── source_projects/Python/<owner>_<repo>/       # real GitHub repo, pruned to be runnable
│   ├── <package>/                               # the code to translate
│   ├── tests/          + run_tests.sh           # original + LLM-augmented tests (pytest, with coverage)
│   ├── public_tests/   + run_public_tests.sh    # a second test set, the "public" one
│   ├── requirements.txt / setup.py              # dependencies
│   └── test_summary.json                        # coverage, test list, validation timestamp
└── target_projects/
    ├── projects_summary.jsonl                   # one row per (project, source→target) pair, all languages
    └── Python/Java/<owner>_<repo>/              # the SAME project, Python→Java
        ├── src/test/java/.../original/          # tests/ translated to JUnit 5
        ├── src/test/java/.../public_tests/      # public_tests/ translated to JUnit 5
        ├── run_tests.sh                         # `mvn clean test`
        └── project_structure.txt                # expected final tree (incl. pom.xml)
```

- Each target project is the same repository as the source project with the same name. The target is **not** a finished translation: it contains only the translated tests. `src/main/java` and `pom.xml` are what the migration system has to produce. The translation counts as correct when `run_tests.sh` passes.
- The source projects are Python 3 code, validated under Python 3.11. The dataset has no Python 2 code. Our `py27` image and the `calc` example are there for the Py2→3 regime, whose dataset comes from outside RepoTransBench (see the proposal, §5).
- Some translated Java tests reimplement the logic inside the test class instead of calling `src/main`, which is a known benchmark quality issue. Treat target pass rates with that in mind.

## Isolation guarantees (verified by `sandbox.acceptance`)

- `--network none`, `--cap-drop ALL`, `no-new-privileges`, non-root user (UID 1000).
- Memory, CPU and PID limits. `--init` reaps orphaned processes, so leftovers can't exhaust the PID limit.
- Differential mode also has:
  - a read-only root filesystem and a read-only `/code` mount;
  - a fresh interpreter and fresh working directory for every case;
  - a per-case `RLIMIT_AS`, so over-allocation raises `MemoryError`;
  - a `SIGALRM` timeout with a process-group kill as backstop.

## Known limits

- Dependency installation from `setup.py`/`pyproject.toml` needs the `py3` base, which has uv. Legacy Python 2 projects with dependencies need a requirements-based image.
- RepoTransBench projects that need Poetry, tox or make, or Python < 3.11, are not handled yet. `rtb_smoke` reports these by cause.
- Python→Java differential testing (a Java-side harness that emits the same tagged JSON) is Phase 3 work. For now, Java is covered by suite mode.
- The java17 image caches only JUnit 5 and Mockito. RepoTransBench Java tests that import other libraries (e.g. `com.fasterxml.jackson`) will not build offline until those libraries are added to the warm-up pom.

## Troubleshooting

| symptom | fix |
|---|---|
| `error during connect` / `Cannot connect to the Docker daemon` | Start Docker Desktop and wait for "Engine running". |
| `No module named sandbox` | Run from the repository root, not from inside `sandbox/`. |
| `/bin/bash^M: bad interpreter` inside a container | A `.sh` file was checked out with CRLF line endings. `.gitattributes` forces LF: run `git add --renormalize .` and check out again. |
| `sandbox returned 0/N results` | The container died before the harness ran. Usually the image is missing (`docker images acm-sandbox/*`) or the code directory path is wrong. The error includes the container's stderr. |
| Check 8 fails with `dataset not found` | Extract the benchmark (above) or run with `--skip-rtb`. |
| Disk filling up | Remove per-project images with `docker images "acm-proj/*" -q \| xargs docker rmi`. They are rebuilt on demand. |
