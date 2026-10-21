# logs/

Everything the sandbox runs leaves a record here. The folder is created automatically: each command makes its own timestamped sub-folder (`YYYYmmdd-HHMMSS`), so runs never overwrite each other. To write logs somewhere else, set `ACM_LOG_DIR=/some/path`.

## Layout

```
logs/
├── build/
│   ├── <ts>-base/                   python -m sandbox.build_images
│   │   ├── py27.log  py3.log  java17.log     full `docker build` output
│   │   └── summary.json                      per image: ok, returncode, seconds, log
│   └── projects/
│       └── <project>-py3-<hash>.log          one per per-project image build (pass or fail);
│                                             the name matches the image tag acm-proj/<project>:py3-<hash>
├── acceptance/<ts>/                 python -m sandbox.acceptance
│   ├── acceptance.log                        console transcript
│   ├── summary.json                          per check: ok, seconds, detail
│   ├── differential-py3_naive/  ...          raw results each check was judged on
│   ├── isolation-py27/  determinism/  java-offline/
│   └── rtb/<project>/                        suite logs for the regression projects
├── rtb_smoke/<ts>/                  python -m sandbox.rtb_smoke N
│   ├── summary.json                          per project: green, counts, cause, image, build_log, suite_log
│   └── projects/<project>/                   that project's suite logs (see suite/ below)
├── suite/<ts>-<project>/            run_suite(...) called directly
│   ├── output.log                            full stdout+stderr of the test run
│   ├── junit/                                pytest / Surefire JUnit XML
│   ├── tests.json                            per test: id, outcome, message
│   └── summary.json                          image, command, returncode, counts, seconds
├── differential/<ts>-<legacy>-vs-<migrated>/     differential(...) called directly
│   ├── cases.jsonl                           the inputs
│   ├── legacy.results.jsonl                  raw harness results, legacy side
│   ├── migrated.results.jsonl                raw harness results, migrated side
│   ├── legacy.container.log  migrated.container.log   container stderr
│   ├── verdicts.jsonl                        compare_results output, one per case
│   └── summary.json                          counts + ids of inequivalent cases
├── cases/<ts>-<image>-<dir>/        run_cases(...) called directly
│   ├── <image>.results.jsonl
│   └── <image>.container.log
├── analysis/<ts>-check/             python -m analysis.check_tools
│   ├── verify/                               every tool on the built-in fixture: <tool>.json, verify.json
│   ├── sample-<repo>/                        every tool on the sample repo:
│   │   ├── <tool>.json                       raw output (libcst, pyan3, pyright, staticfg, ...)
│   │   ├── tools.json                        per tool: ok, version, seconds, stats; py2 / unconvertible files
│   │   └── console.log
│   └── summary.json                          per tool: version, verified, ran on sample, stats
└── layer0/                          python -m layer0 [REPO ...]   (see layer0/README.md)
    ├── <ts>-<repo> | <ts>-<n>repos/
    │   ├── <repo>/                           the bundle = INPUT TO LAYER 1:
    │   │   ├── manifest.json  fragments.jsonl  dependency_graph.json  migration_order.json
    │   │   ├── validation.json  audit.json  report.md      contract checks, graph quality, readable view
    │   │   └── raw/                          each analysis tool's raw output
    │   ├── job.json  console.log
    │   └── summary.json                      per repo: ok, regime, stats, validation, graph quality
    └── <ts>-verify/                 python -m layer0 --verify
        ├── verify/{fixture/, bundle/, verify.json}   fixture sources, its bundle, the 17 known-answer checks
        └── summary.json                      fixture and contract check results
└── layer1/                          python -m layer1 [REPO ...]   (see layer1/README.md)
    ├── <ts>-<repo> | <ts>-<n>repos/
    │   ├── <repo>/
    │   │   ├── run.json  plan.json  events.jsonl         options, the units in order, every transition
    │   │   ├── units/<unit>/attempt-<n>/             request.json  verdict.json  evaluation.json
    │   │   ├── state.json  translations.jsonl  escalations.json   final statuses, committed code, escalations
    │   │   ├── migrated/                         THE MIGRATED CODEBASE
    │   │   ├── validation.json  build_check/  report.md     run contract, does it compile, readable view
    │   │   ├── report_summary.json  llm/calls.jsonl  units/<unit>/brief.json   LLM-written prose and its calls
    │   │   ├── checkpoints.sqlite                only while a run is unfinished (crashed / paused / quota): --resume
    │   │   ├── units/<unit>/attempt-<n>/layer2/   --layer2 agents: contract, translation-<r> (pieces, repair diffs),
    │   │   │                                     execution-<r>, failure-<r> / verdict-<r>, verdict.json, sandbox/
    │   │   ├── layer2/                           llm_calls.jsonl, mcp_calls.jsonl, config.json, servers/*.log,
    │   │   │                                     units/<unit>/{contract,tests,attempts}.json + legacy/ (oracle runs),
    │   │   │                                     workspace/ (committed.json, candidate trees)
    │   │   └── layer2_summary.json               agents: verified / compile-only units, tests, repairs, tokens
    │   ├── graph.mmd  console.log
    │   └── summary.json                      per repo: ok, unit statuses, Layer 2 calls, contract, build
    └── <ts>-verify/                 python -m layer1 --verify
        ├── layer0/{verify,python3}/          the Layer 0 fixture bundles it starts from
        ├── all-pass/  scripted/  hitl/  resume/  ...   one Layer 1 run per scenario
        └── summary.json                      the 30 known-answer checks
└── layer2/<ts>-verify/               python -m layer2 --verify: layer0/ (fixture), agents/ and quota/ (Layer 1 runs
                                     with the agents and a scripted LLM), summary.json (the known-answer checks)
└── llm_bench/<ts>/                  python -m layer1.llm_bench: bench.json, bench.md (texts side by side), calls.jsonl
```

In summaries, paths to other logs are relative to `logs/`.

## What is committed

Only the run history goes into git: the `summary.json` directly under each `build/<ts>-base/`, `acceptance/<ts>/`, `rtb_smoke/<ts>/`, `analysis/<ts>-check/`, `layer0/<ts>*/`, `layer1/<ts>*/` and `layer2/<ts>*/`, plus this README. Layer 0 bundles are reproducible: the same command gives byte-identical fragments, graph and order. Layer 1 runs are too, given the same Layer 2. Everything else is git-ignored. Raw logs are large and machine-specific, and `suite/`, `differential/` and `cases/` will fill up once the agents call the sandbox in a loop. To keep a particular raw log, force-add it with `git add -f <path>`.

The two committed summaries answer:
- **"Is the sandbox healthy?"** Read the latest `acceptance/*/summary.json`.
- **"How much of RepoTransBench runs?"** Read the latest `rtb_smoke/*/summary.json`, or compare two of them to see whether a change helped.

## Housekeeping

Delete whatever you don't need. Nothing reads old logs, and a missing `logs/` folder is recreated. Cached per-project images keep working without their build log. Their `build_log` in summaries is then `null`.
