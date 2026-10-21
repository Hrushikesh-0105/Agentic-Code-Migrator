# Static analysis toolchain (Layer 0)

The Layer 0 tools from the proposal, plus the extra tools the later agents need, all pinned in one Docker image, `acm-sandbox/analysis`. This README covers:
- which tools are included, and why;
- how to set them up and verify them;
- what each tool outputs, using the RepoTransBench project `alingse_jsoncsv` as the example.

Combining these outputs into the per-repo **static analysis report** is the next step (see [Next](#next-the-static-analysis-report)).

## Why a Docker image

- **Same versions for everyone.** Every tool is pinned. Several of the tools only work on specific Python versions, and a teammate's host Python (e.g. 3.14) won't match.
- **Same isolation as the sandbox.** The repo is mounted read-only, with no network. The tools only *read* the code; nothing in it is imported or run.
- **One place to expose from.** The Phase 1 MCP wrappers can serve the tools from this image.

## The toolchain

| need (proposal, Layer 0 / Layer 2) | tool | version | output used for |
|---|---|---|---|
| Python CST and symbol table | **LibCST** (proposal) | 1.9.0 | every class/function/method with its fully qualified name, span, parameters and defaults, resolved calls, locals and free variables; later, codemods and the skeleton |
| Multi-language parsing | **tree-sitter** (proposal) with `tree-sitter-python`, `tree-sitter-java` | 0.26.0 / 0.25.0 / 0.23.5 | error-tolerant definition spans (lines and bytes) for cutting fragments; parses **Python 2** syntax and the **Java** target |
| Python 2 parsing and 2to3 hazards | **fissix** (maintained lib2to3 fork; lib2to3 was removed in Python 3.13) | 24.4.24 | which 2to3 fixer would change which line; the rule-based baseline; converting Python 2 files so the Python-3-only tools can read them |
| Hazards 2to3 ignores | **python-modernize** | 0.8.0 | classic `/` division, `zip`/`map` iterator changes, `unicode` type |
| Call graph #1 | **pyan3** | 2.8.1 | uses/defines edges between modules, classes, functions and methods, including `super()` calls |
| Call graph #2 | **JARVIS** (`jarviscg`) | 0.1.0rc7 | function-level edges, including calls into builtins, stdlib and third-party APIs (needed for Python→Java API mapping) |
| Module dependencies | **grimp** | 3.17 | import graph, internal and external |
| Bottom-up translation order | **networkx** | 3.7 | union of both call graphs, then strongly connected components, then reverse topological order (callees first; mutual recursion grouped) |
| Types for the Analyzer's contracts | **pyright** (Node bundled) | 1.1.414 | inferred return types (`--createstub`) and type diagnostics (`--outputjson`) |
| Control flow for the Test-Generation agent | **staticfg** | 0.9.5 | CFG per function: blocks, edges, **branch conditions** (the boundaries tests should straddle) |
| Size and complexity, both languages | **lizard** | 1.24.0 | per-function NLOC, cyclomatic complexity, tokens and parameters, for Python (including Python 2) and Java |
| Python metrics | **radon** | 6.0.1 | LOC/SLOC/comments and maintainability index per file |
| Import classification | **stdlib-list** | 0.12.0 | internal / stdlib / **Python-2-only stdlib** (e.g. `StringIO`, `urllib2`) / third-party |

The image runs Python **3.12**, the newest version every pinned tool supports.

### Considered and rejected

| tool | why not |
|---|---|
| PyCG | Archived in 2023, and doesn't run on Python 3.12+. pyan3 and JARVIS replace it. |
| Scalpel | Depends on `typed-ast`, which doesn't build on 3.12. |
| python-graphs (Google) | On 3.12, function CFGs fail ("No frame exists") and program graphs crash. |
| pytype | Google has sunset it, and 3.12 was its last supported version. pyright covers types. |
| CodeQL | AlphaTrans (a Phase 4 baseline) used it. It's more precise, but large (~1 GB) and licensed for OSS and research use only. Worth reconsidering if pyan3/JARVIS precision turns out not to be enough. |
| JavaParser | Resolves Java symbols. Not needed until Phase 3 (Java-side harness); tree-sitter-java covers parsing until then. |

### How Python 2 code is handled

LibCST, pyan3, JARVIS, pyright, staticfg and radon only parse Python 3. `run_tools.py` handles this in four steps:
1. It finds every file that isn't valid Python 3.
2. It builds a Python 3 *view* of the repo in which only those files are converted by fissix (2to3). 2to3 edits within lines, so line numbers still match the original.
3. The Python-3-only tools read the view.
4. tree-sitter, fissix, modernize, lizard and stdlib-list read the **original** files.

A file that 2to3 can't turn into valid Python 3 is skipped by the Python-3-only tools, and listed as `not_convertible` in `tools.json`, instead of failing them.

## Setup and verify

From the repository root, with Docker running:

```bash
python -m sandbox.build_images analysis   # ~4 min; also built by a plain `python -m sandbox.build_images`
python -m analysis.check_tools            # verify all 13 tools, then run them on the sample repo
```

`check_tools` has two stages:
1. **verify.** It builds a small fixture repo containing Python 3, Python 2 and Java, runs every tool on it, and checks each tool's output against a known answer. For example, pyan3 must find the edge `demo.core.total → demo.util.double`, and modernize must flag `v / 2`. This proves each tool is installed *and* works in the hardened container, which a version check wouldn't.
2. **sample.** It runs every tool on a real repo:
   - default: RepoTransBench `alingse_jsoncsv` plus its Java target;
   - `--repo PATH [--java PATH]` for another repo;
   - `--skip-sample` for verify only.

Expected output:

```
== verify: each tool on the built-in fixture
[PASS] fissix       24.4.24    2to3 flags print, dict and unicode in legacy.py
[PASS] grimp        3.17       import demo.core -> demo.util
... (13 lines)
== sample: alingse_jsoncsv (+ Java target)
[ok  ] libcst         0.86s  {"classes": 5, "functions": 14, "methods": 20, "internal_calls": 54, ...}
... (13 lines)
13/13 tools verified, 13/13 ran on the sample
```

Everything is logged to `logs/analysis/<timestamp>-check/`:
- `verify/` and `sample-<repo>/`: one `<tool>.json` of raw output per tool, `tools.json` (status, version, seconds and stats per tool, plus which files were Python 2 or not convertible) and `console.log`;
- `summary.json`: committed as run history, like the other top-level summaries.

## What each tool outputs on `alingse_jsoncsv`

The sample has 447 lines in 5 modules. It's dual Python 2/3 code with `PY2` branches, and depends on `click`, `unicodecsv` and `xlwt`. Its Java target has 15 test files.

| tool | result on the sample | example |
|---|---|---|
| LibCST | 5 classes, 14 functions, 20 methods, 54 resolved internal calls | `jsontool.expand` (lines 91–108) has params `origin`, `separator='.'`, `safe=False`; calls `jsoncsv.jsontool.gen_leaf` and `jsoncsv.utils.encode_safe_key`; free vars include **`unicode`**, `PY2` |
| tree-sitter | 39 Python definitions; 82 Java definitions in 15 files, 0 parse errors | `TestDumpTool.test_dumpexcel_csv`, ... |
| fissix | hits in 2 files | `jsontool.py`: `fix_dict` lines 31, 33, 115, 117 (`iteritems`); `fix_map` 79, 83, 98, 100; `fix_unicode` 98; `fix_xrange` 48 (a `range` used as a list) |
| modernize | adds `fix_zip` and `fix_unicode_type` in `jsontool.py` | hit lines include the line where modernize would **insert** an import (13), not only call sites |
| pyan3 | 56 internal uses edges, of which 32 are function→function | `DumpCSV.initialize → DumpExcel.initialize` (resolved `super()`) |
| JARVIS | 22 function→function edges, **all also found by pyan3**; 35 external targets | `DumpExcel.dump_file → json.loads`, `DumpCSV.write_headers → unicodecsv.DictWriter` |
| grimp | 5 modules, 5 internal imports | `jsoncsv.main → click, jsoncsv.dumptool, jsoncsv.jsontool, jsoncsv.utils` |
| networkx | 34 units, 30 edges, 34 steps, **no cycles** | order starts `jsontool.is_array_index`, `utils.decode_safe_key`, `jsontool.from_leaf`, `utils.encode_safe_key`, `jsontool.gen_leaf`, ... |
| pyright | 26 inferred return types, 12 diagnostics | `from_leaf -> list[Any] \| dict[Any, Any]`, `patch_value -> Literal['']`; diagnostics at `jsontool.py:31` (`iteritems`) and `:98` (`unicode`) are **the same hazards fissix flags** |
| staticfg | 34 CFGs, 80 branch conditions | `gen_leaf`: `path is None`, `not isinstance(root, (dict, list)) or not root`, `isinstance(root, dict)`, `PY2`, ... |
| lizard | 34 Python functions (251 NLOC); 67 Java methods | highest CCN: `decode_safe_key` 9, `gen_leaf` 8; Java `TestJsonTool::restore` 9 (the test **reimplements** the logic) |
| radon | 447 LOC / 324 SLOC | lowest maintainability: `dumptool.py` 49.9 |
| stdlib-list | third-party: `click`, `unicodecsv`, `xlwt`; no Python-2-only stdlib imports | |

## Known limits

- **Third-party types.** pyright reports `reportMissingImports` for `click`, `unicodecsv` and `xlwt`, because the analysis image doesn't install project dependencies. Calls into them get no types. Fix later by running pyright against the per-project sandbox image's site-packages.
- **Call graphs are static.** Dynamic dispatch, `getattr` and monkey-patching are missed by both call-graph tools. Taking the union of pyan3 and JARVIS reduces misses but doesn't remove them.
- **CFGs.** staticfg's loop "conditions" are the loop's iterable (e.g. `items`), not a boolean. Its `raise` handling is a stub. `run_tools.py` works around two staticfg problems:
  - it patches a crash on calls like `super().f()`;
  - it records each branch condition at the condition's own line. staticfg's `block.at()` gives the line where the block starts, which is often a docstring.
- **Byte-order marks.** pyan3 and radon reject a UTF-8 BOM, as found in RepoTransBench's json-rpc. The Python 3 view strips it; line numbers are unchanged. The verify fixture includes a BOM file.
- **Speed.** fissix and modernize run each fixer separately per file, to attribute lines to fixers. That's fine for benchmark-sized repos (~5 s here) but will need batching for large ones.
- **JARVIS and pyan3 guess.** JARVIS is a research prototype (0.1.0rc7). Both tools sometimes resolve attributes to the wrong function, for example pyan3 maps the field `self._data` onto the property `data`. Layer 0 keeps an edge that only they report only when the source code supports it.

## Next: Layer 0

These raw outputs are merged per repo by [Layer 0](../layer0/README.md) into the bundle Layer 1 consumes. It contains:
- fragments with spans, signatures, inferred types, complexity, branch conditions and hazards;
- the dependency graph;
- the bottom-up migration order;
- the Java target's expected API.
