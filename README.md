# agentic-code-migrator

A Robust Agentic System for Migration of Codebases.

The system migrates legacy code in two regimes: **Python 2 → Python 3** and **Python → Java**. It then _verifies_ that the migration is correct, instead of just checking that it compiles:

1. Static analysis breaks the repository into small fragments and orders them bottom-up by call order.
2. LLM agents translate each fragment.
3. A test-generation agent writes targeted differential tests.
4. The legacy and migrated fragments run side by side on identical inputs in a Docker sandbox.
5. A repair agent patches any behavioural divergence.

## Architecture and status

|         | component                                                                                   | status                                                                                                                                                                            |
| ------- | ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Layer 0 | Static analysis & decomposition (LibCST / tree-sitter call graphs, skeleton scaffold)       | **done**: toolchain [`analysis/`](analysis/README.md); dependency graph, fragments, migration order and the Layer 1 bundle [`layer0/`](layer0/README.md). Target skeleton pending |
| Layer 1 | Deterministic orchestration (LangGraph state machine, retry bounds, human-in-the-loop gate) | **done**: [`layer1/`](layer1/README.md): scheduling, verdict checks, retries, escalation, human review, crash-resume, assembly of the migrated codebase                           |
| Layer 2 | Agents: Analyzer, Translator, Test-Generation, Verdict, Repair, on Groq models              | **done**: [`layer2/`](layer2/README.md). `--layer2 agents`; a dummy that passes every unit is still the default for testing Layer 1                                               |
| Layer 2 | **Executor & Sandbox** (Docker harness, differential comparator)                            | **done**: [`sandbox/`](sandbox/README.md), plus a Java-side harness for Python→Java differential tests                                                                            |
| Layer 2 | **MCP integration**: every agent tool call goes through an MCP server                       | **done**: `acm-analysis` (Layer 0), `acm-workspace` (file system), `acm-sandbox` (Docker, test frameworks): [`layer2/servers/`](layer2/servers/)                                  |

| phase | scope                                                                      | status                                                                                                                                                |
| ----- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | Tooling & sandbox setup                                                    | done: sandbox, static-analysis tools, Layer 0 decomposition (3 RepoTransBench pairs), MCP servers, the Java target skeleton (Layer 2's `skeleton.py`) |
| 2     | Agent graph (LangGraph, tiered open-weight models)                         | done: Layer 1 orchestrator and the Layer 2 agents on Groq (`gpt-oss-20b` light, `gpt-oss-120b` heavy, `qwen3.8-27b` tests)                            |
| 3     | Targeted verification loop (test generation, verdicts, trace diffing)      | done: coverage-guided differential tests, deterministic Python↔Java comparison, failure records, repair loop                                          |
| 4     | Benchmarking & ablations (Py2→3 hazard suites, RepoTransBench Python→Java) | —                                                                                                                                                     |

## Repository layout

```
agentic-code-migrator/
├── sandbox/          Phase 1 execution sandbox: Docker images, harness, comparator, acceptance tests
├── analysis/         Layer 0 static-analysis toolchain: pinned image, tool runner, verification
├── layer0/           Layer 0 pipeline: dependency graph, fragments, migration order -> bundle for Layer 1
├── layer1/           Layer 1 orchestrator (LangGraph): schedule -> Layer 2 -> verdict -> commit / retry / escalate
├── layer2/           Layer 2: the agents, their MCP servers and the target skeleton (plus a dummy for testing Layer 1)
├── logs/             run logs for image builds, acceptance runs, benchmark runs, sandbox calls (auto-created)
└── benchmarks/       RepoTransBench dataset, downloaded locally (git-ignored)
```

## Setup

### 1. Prerequisites

- **Git**
- **Python 3.8+**. The sandbox, the analysis toolchain and Layer 0 use only the standard library. Layer 1 needs **Python 3.10+** and the packages in `requirements.txt` (LangGraph), installed into a virtual environment in step 3.
- **Docker Desktop**, with the engine running. `docker version` must show a _Server_ section.
- About **10 GB of free disk** if you run the benchmark: 1.8 GB for the four base images, plus roughly 300–750 MB per benchmark project image.

On Windows, run the commands below in Git Bash. They also work in PowerShell if you swap `mkdir -p` for `mkdir` and download the tarball with a browser.

### 2. Clone

```bash
git clone $linkOfRepo
cd agentic-code-migrator          # run every command from here
```

### 3. Build the images and install Layer 1's packages

```bash
python -m sandbox.build_images    # acm-sandbox/py27, py3, java17, analysis; ~10–15 min the first time
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows; .venv/bin/python on Linux / macOS
```

### 4. Download the benchmark

```bash
mkdir -p benchmarks/repotransbench
curl -LO https://github.com/DeepSoftwareAnalytics/RepoTransBench/releases/download/v1.0/projects.tar.gz
tar -xzf projects.tar.gz -C benchmarks/repotransbench \
    'source_projects/Python' 'target_projects/Python/Java' 'target_projects/projects_summary.jsonl'
```

### 5. Verify

```bash
python -m sandbox.acceptance      # expect "8/8 checks passed"
```

This checks:

- the three interpreters;
- that the differential comparator catches a buggy migration and accepts a correct one;
- the isolation guarantees;
- determinism;
- offline Java builds;
- three RepoTransBench projects.

Use `--skip-rtb` if you skipped step 4.

Then the static-analysis toolchain:

```bash
python -m analysis.check_tools    # expect "13/13 tools verified, 13/13 ran on the sample"
```

This runs each of the 13 tools on a built-in Python 3 + Python 2 + Java fixture and checks its output against a known answer. It then runs them all on the sample repo, RepoTransBench `alingse_jsoncsv`. Use `--skip-sample` if you skipped step 4.

Then Layer 0 itself:

```bash
python -m layer0 --verify         # expect "17/17 fixture checks, 10/10 contract checks"
python -m layer0                  # bundles for the 3 test pairs; each "contract 10/10"
```

`--verify` runs the pipeline on a built-in fixture (Python 2 code, inheritance, recursion, a module registry, a Java test) and checks known answers. The second command decomposes three RepoTransBench Python→Java projects into `logs/layer0/<ts>-3repos/<repo>/`. Each bundle contains `manifest.json`, `fragments.jsonl`, `dependency_graph.json` and `migration_order.json` (the input to Layer 1), plus a readable `report.md`.

Then Layer 1 (it runs Layer 0 first) and Layer 2:

```bash
.venv/Scripts/python -m layer1 --verify   # expect "30/30 known-answer checks passed"
.venv/Scripts/python -m layer1            # Layer 0 + Layer 1 (dummy Layer 2) on the 3 test pairs; each "contract 10/10"
.venv/Scripts/python -m layer2 --verify   # the agents with a scripted LLM, real MCP servers and Docker sandbox
.venv/Scripts/python -m layer1 --layer2 agents --scope itsdangerous.encoding pallets_itsdangerous   # live, needs GROQ_API_KEY
```

`--verify` drives every branch of the state machine on Layer 0's fixture:

- retries with feedback, and malformed verdicts;
- escalation after 3 attempts, and blocking of the dependents;
- human review, and resuming after a crash;
- determinism.

Reports are written without anyone's help. If `GROQ_API_KEY` and/or `GEMINI_API_KEY` is set in the environment or in a `.env` file at the repo root (git-ignored), an LLM writes:

- each report's summary;
- a brief for every escalated unit.

Both are checked against the run's facts. Groq goes first and Gemini is the fallback. Without a usable key, deterministic templates are used. `python -m layer1.llm_bench` compares the models on real report tasks. See [layer1/README.md](layer1/README.md#llm-written-report-text).

With `--layer2 agents`, each unit is analysed, tested on the legacy code, translated, compiled, run on both sides in Docker, compared, and repaired; see [layer2/README.md](layer2/README.md). `--scope` limits a run to part of a repository (and what it depends on), since a whole repository needs more than a day of the free Groq quota.

The second command writes each repo's migrated codebase to `logs/layer1/<ts>-3repos/<repo>/migrated/`, plus the per-attempt requests and verdicts and a `report.md`.

Optional: measure how much of the benchmark runs in the sandbox:

```bash
python -m sandbox.rtb_smoke 30
```

### 6. Logs

You don't need to create anything. Every command above writes to `logs/`, one timestamped folder per run, and prints the path at the end. `build_images` logs to `logs/build/`, `acceptance` to `logs/acceptance/`, `rtb_smoke` to `logs/rtb_smoke/`, `analysis.check_tools` to `logs/analysis/`, `layer0` to `logs/layer0/`, `layer1` to `logs/layer1/` (including the agents' logs), `layer2 --verify` to `logs/layer2/`. Direct `run_suite` / `differential` calls log to `logs/suite/` and `logs/differential/`.

The small `summary.json` of each build, acceptance, benchmark, analysis-check, Layer 0 and Layer 1 run is committed, so the repo keeps a pass/fail history. Raw logs stay local. Layout and git policy: [logs/README.md](logs/README.md).

## Next steps

For details, see [sandbox/README.md](sandbox/README.md), which covers:

- how the containers and verification work;
- how to verify your own legacy/migrated codebase pair;
- the RepoTransBench dataset structure;
- isolation guarantees, known limits and troubleshooting.

See [analysis/README.md](analysis/README.md) for the static-analysis toolchain:

- which tools are included and why, and which were rejected;
- how Python 2 code is handled;
- what each tool outputs on the sample repo.

See [layer0/README.md](layer0/README.md) for Layer 0:

- how the dependency graph, fragments and migration order are built;
- the bundle format, which is the Layer 0 → Layer 1 contract;
- how it's verified;
- results and findings on the three test pairs.

See [layer1/README.md](layer1/README.md) for Layer 1:

- the state machine, and its scheduling, retry and escalation rules;
- the Layer 1 ↔ Layer 2 protocol, which is the interface the agents implement;
- how the migrated codebase is assembled;
- how it's verified, and results on the three test pairs.

See [layer2/README.md](layer2/README.md) for Layer 2:

- the six agents, the models they use, and the repair loop;
- the MCP servers and their tools;
- the target skeleton and the cross-language differential harness;
- how it's verified, and results on real repositories.
