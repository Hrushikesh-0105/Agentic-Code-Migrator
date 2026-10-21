# Layer 0: static analysis and decomposition

Layer 0 turns a legacy repository into the input of Layer 1 (the LangGraph orchestrator), following the proposal's architecture:

| architecture box | what Layer 0 does | output |
|---|---|---|
| Parse code | Runs the pinned 13-tool toolchain ([`analysis/`](../analysis/README.md)): LibCST, tree-sitter, pyan3, JARVIS, pyright, staticfg, fissix, modernize, lizard, radon, stdlib-list, grimp, networkx | `raw/` |
| Extract dependencies and build the call graph | Builds a fragment-level dependency graph. Edges come from a statement-level LibCST reference pass, plus pyan3 and JARVIS call graphs, plus structural edges | `dependency_graph.json` |
| Decompose into fragments | Cuts the repo into functions, methods, class shells, module headers and module init code. Fragment source is taken from the **original** files (Python 2 included) | `fragments.jsonl` |
| Determine the migration order | Condenses strongly connected components, then orders them bottom-up (reverse topological, callees first). Groups the steps into parallelisable waves | `migration_order.json` |
| Output to Layer 1 | Metadata: repo, languages, scope, the Java target's expected API, toolchain versions, stats, warnings | `manifest.json` |

Layer 0 runs in the network-less `acm-sandbox/analysis` container, with the repo mounted read-only. Nothing in the repo is imported or executed. The host side is standard library only.

## Run

From the repository root, with Docker running and the `analysis` image built (`python -m sandbox.build_images analysis`):

```bash
python -m layer0 --verify                       # pipeline on a fixture with known answers (expect 17/17 + 10/10)
python -m layer0                                # the 3 default Python->Java test pairs
python -m layer0 pallets_itsdangerous           # any RepoTransBench project (its Java target is found automatically)
python -m layer0 path/to/repo [--java path/to/java/project] [--target java|python3]
```

Each run writes `logs/layer0/<timestamp>[-label]/`:

```
logs/layer0/<ts>-3repos/
├── <repo>/                      the bundle for one repo = the input to Layer 1
│   ├── manifest.json            metadata (entry point: lists the files below)
│   ├── fragments.jsonl          one fragment per line
│   ├── dependency_graph.json    fragment -> fragment edges, modules, external APIs, cycles
│   ├── migration_order.json     bottom-up steps, each with its wave
│   ├── validation.json          host-side contract checks (below)
│   ├── audit.json               graph-quality measurements (below)
│   ├── report.md                human-readable view of all of the above
│   └── raw/                     each tool's raw output + tools.json
├── job.json  console.log
└── summary.json                 per repo: ok, regime, stats, validation, graph quality (committed)
```

Runs are deterministic. Running twice gives byte-identical `fragments.jsonl`, `dependency_graph.json` and `migration_order.json`. Only the timestamp and timings in `manifest.json` change.

## How it works

### Step 1: dependencies and call graph

**Scope.** The analysed files are the library: the top-level packages (directories with `__init__.py`) and top-level modules. Test files, `setup.py`, `conftest.py` and scripts outside a package (e.g. jmespath's `perf/`) are left out, and each is listed in `manifest.excluded_files` with the reason. **`src/` layouts are detected**, so module names are `itsdangerous.signer`, not `src.itsdangerous.signer`.

**Edges.** An edge `A -> B` means *A depends on B, so B is migrated first*. The edge kinds are:

| kind | meaning | found by |
|---|---|---|
| `call` | A calls B | LibCST (resolved qualified names), JARVIS |
| `reference` | A reads B: a class, a function object, or a module constant (`SCALE`, `REGISTRY`) | LibCST |
| `inherit` | class A derives from B | LibCST (base-class expressions) |
| `decorator` | A is decorated by B | LibCST |
| `constructor` | A instantiates a class whose `__init__` is B. The lookup goes **through internal base classes**: `Square(3)` → `Shape.__init__` | LibCST + inheritance |
| `use` | pyan3's "uses" relation, which resolves `self.method()` and `super()` calls that LibCST cannot | pyan3 |
| `member` | a method (or nested class) lives in class B | structure |
| `module` | A needs its module's header (imports, constants) | structure |
| `init` | Java targets only: class A is declared together with its own `__init__` B. With B's `member` edge back to A, the class and its constructor form **one step**, because a Java class does not compile without its constructors (added 2026-10-09 for Layer 2) | structure |

**Evidence filter.** pyan3 and JARVIS sometimes guess. On json-rpc, for example:
- pyan3 maps the field access `self._data` onto the property `data`, which created a **false 3-method cycle**;
- JARVIS links `self.serialize(...)` to unrelated functions named `*serialize*`.

So an edge reported *only* by them is kept only if the target's name occurs as a whole word in the source fragment's code (`\bdata\b` does not match `self._data`). Dropped edges are listed in `dependency_graph.json` (`dropped_unconfirmed`) and counted in the stats.

### Step 2: fragments

| kind | contents |
|---|---|
| `function` | a top-level `def`. Nested defs, lambdas and classes inside it stay part of it |
| `method` | a `def` directly in a class body |
| `class` | the class **shell**: decorators, header, docstring, class attributes. Its methods are separate fragments |
| `module_header` | module-level imports, constants and compatibility shims that use no project definitions |
| `module_init` | module-level statements that *do* use project definitions (registries like `REGISTRY = {"square": make}`, `if __name__ == "__main__":`), plus anything using a name they bind |

Splitting module-level code into a header and an init part keeps the order acyclic. Every function depends on its module's header, while init code runs after the functions it uses.

**Rules every fragment obeys** (checked by the validator, below):
- Each segment is a byte range of the **original** file. Python 2 source stays verbatim even though the Python-3-only tools read a 2to3 view, because that view keeps line numbers.
- Fragments never overlap.
- Every code line belongs to exactly one fragment.
- A conditional definition such as `if PY2: def f(): ... else: def f(): ...` is **one** fragment with two segments.

### Step 3: migration order

1. Collapse each strongly connected component into one step. Mutually recursive functions, or classes that use each other, must be migrated together.
2. Order the steps bottom-up, deterministically: by wave, then file, then line.
3. A step's **wave** is the length of the longest dependency chain below it. All steps in one wave are independent, so Layer 1 can run them in parallel. `steps_per_wave` gives the available parallelism.

## The bundle format (Layer 0 → Layer 1 contract)

Format `acm-layer0-bundle` version `1.0`. The validator is [`contract.py`](contract.py).

### `manifest.json`

| field | content |
|---|---|
| `format` | `{"name": "acm-layer0-bundle", "version": "1.0"}` |
| `repo` | `name`, `source_root` (`.` or `src`), `packages`, `modules` |
| `migration` | `source_language` (`python2` if any in-scope file is not valid Python 3 syntax), `target_language`, `regime` |
| `files` | per in-scope file: `module`, `lines`, `python2_source`, `vendored`, `maintainability_index` |
| `excluded_files` | `[{file, reason}]` |
| `target` | Java target only (`null` otherwise); see below |
| `toolchain` | image, plus per tool: `ok`, `version`, `seconds` (`error` on failure) |
| `stats` | fragments (total and `by_kind`), edges, steps, waves, cycles, `largest_cycle`, loc, fragments with hazards / target hints, `unconfirmed_edges_dropped`, `flags`, seconds |
| `outputs` | file names of the other bundle files |
| `warnings` | e.g. 2to3 changed a file's line count, a tool failed, the Java tests stub the classes under test |

`target` lists what the Java tests expect. RepoTransBench targets ship **only tests**, so the classes and methods the tests use are the contract the translation must satisfy:
- `package`, `test_files`, `test_methods`;
- `expected_api`: per class, `qualified`, `constructed` (count), `methods`, `static_methods`, `used_in`;
- `stubbed_in_tests`: non-test classes the tests declare themselves, with their methods;
- `third_party_imports`.

### `fragments.jsonl` (one JSON object per line, in migration order)

| field | content |
|---|---|
| `id` | qualified name (`pkg.mod.Class.method`). Module code is `pkg.mod::header` / `pkg.mod::init` |
| `kind`, `qualname`, `module`, `file` | see the table above; `file` is relative to `repo.source_root` |
| `parent`, `children` | class ↔ method links |
| `segments` | `[{start_line, end_line, start_byte, end_byte}]` in the original file (end byte exclusive) |
| `source`, `loc` | the original text of the segments, joined by a `# ...` line; non-blank lines |
| `signature` | functions: `text`, `params` (`name`, `default`, `annotation`, `kind`), `returns`, `decorators`, `is_async`, `inferred_return` (pyright). Classes: `text`, `bases`, `keywords`, `decorators` |
| `depends_on`, `dependents` | fragment ids (same edges as the graph) |
| `external_apis` | by category: `builtin` (incl. `builtins.dict.get`), `stdlib`, `stdlib_py2_only`, `stdlib_py3_only`, `third_party`, `unresolved` |
| `features` | language features needing deliberate handling, with counts: `generator`, `var_keyword`, `dynamic_attribute` (`getattr`…), `runtime_type_check`, `eval_exec`, `mutable_default`, `property`, `metaclass`, `multiple_inheritance`, `dunder_method`, ... |
| `metrics` | lizard: `nloc`, `ccn`, `tokens` |
| `branch_conditions` | staticfg: `[{condition, line}]`. These are the boundaries the Test-Generation agent's tests should straddle |
| `hazards` | `[{tool, rule, lines}]`: fissix (2to3) and modernize fixers, and Python-2-only stdlib imports |
| `diagnostics` | pyright: `[{line, severity, rule, message}]` |
| `order` | `{step, wave, cycle}` |
| `flags` | `recursive` (exact self-call), `in_cycle`, `oversized` (> 150 LOC), `vendored` (e.g. a bundled `six.py`), `python2_source`, `entrypoint` |
| `imports`, `defines` | module fragments only: internal modules and external imports by category; names bound |
| `target_hints` | `[{java: "Class#method", match}]`. `match` is `name` (an API the tests use) or `stub_in_tests` (a stand-in the tests declare). These are name-based hints for the Translator, not resolved symbols |

### `dependency_graph.json`

- `nodes`: `[{id, kind, module, file}]`
- `edges`: `[{source, target, kinds, via}]`
- `modules`: per module, its internal and external imports, `vendored`, `python2_source`
- `cycles`, `recursive`
- `external_apis`: name → number of fragments using it, per category
- `edge_sources`
- `dropped_unconfirmed`

### `migration_order.json`

- `steps`: `[{step, wave, fragments, cycle, loc, kinds}]`, in migration order
- `waves`, `steps_per_wave`
- `strategy`

### How Layer 1 uses it

Implemented in [`layer1/`](../layer1/README.md):

1. Load the bundle and re-validate it against the sources. A bundle that fails any contract check is refused.
2. Walk `migration_order.steps` in order. Waves would allow running steps in parallel, which is a later extension.
3. For each step, send Layer 2:
   - the fragments verbatim;
   - the **already-migrated** translations of their `depends_on`, with the edge kinds;
   - the expected Java API of the classes named in `target_hints`.
4. Treat a `cycle` step as one unit.
5. Skip units whose fragments are all `vendored` (e.g. a bundled `six.py`); they count as satisfied dependencies.
6. Block a step whose dependency was not migrated.

## Verification

Three independent layers:

1. **Fixture with known answers** (`--verify`, 17 checks). A small package containing:
   - a real Python-2-only file (`raise E, "msg"`, `print >>out`, `iteritems`, `import StringIO`);
   - inheritance, an inherited constructor, `super(Cube, self).__init__`;
   - mutual recursion and direct recursion;
   - a module registry and a `__main__` block;
   - a test file and a script that must be excluded;
   - a Java test.

   Each claim is checked against the produced bundle. For example:
   - `is_even`/`is_odd` share one cycle step;
   - `make` gets a constructor edge to `Shape.__init__` (inherited);
   - `fact` is flagged recursive, but `Cube.__init__` (a `super()` call) is not;
   - the Python 2 source survives byte for byte.
2. **Contract validation** (10 checks, `validation.json`), run on the host against the original sources:
   - schema;
   - every segment equals the original bytes;
   - hazard, diagnostic and branch lines fall inside their fragment, and branch conditions sit on control-flow lines;
   - fragments are disjoint, and 100% of code lines are covered;
   - the graph agrees with the fragments;
   - the order partitions the fragments;
   - **every edge points to an earlier step** (or stays within a cycle step);
   - cycle steps are strongly connected, and waves are minimal.
3. **Graph-quality audit** (`audit.json`), measured from the source text rather than from the tools:
   - **edge evidence**: semantic edges whose target is named in the source code;
   - **call recall**: plain calls `name(...)` to a uniquely named project function or class that have an edge.

These checks found real bugs during development, all fixed:
- pyan3 and radon crashed on a UTF-8 byte-order mark (json-rpc's `jsonrpc.py`);
- staticfg reported branch conditions at the *start of the block* (often the docstring) instead of the condition's own line;
- recursion was over-reported for `@x.setter` and `super()` calls;
- the pyan3 false cycle described above;
- JARVIS pseudo-names (`<map>.get`) were classified as third-party APIs;
- a `metaclass=` keyword was treated as a base class, which created a false constructor edge.

## Test pairs

RepoTransBench has no Python 2 code: its source projects are Python 3, validated on 3.11. So the three test pairs are **Python→Java** projects; the Python 2 path is covered by the `--verify` fixture. I ranked all 84 pairs with 2+ files, 250–2500 LOC and Java tests by features that make translation hard:
- inheritance;
- generators;
- `*args`/`**kwargs`;
- decorators;
- dynamic attribute access;
- Python 2/3 compatibility code;
- relative imports.

From the top of that ranking I picked three that stress *different* things:

| pair | why it needs significant changes | fragments | edges | steps / waves | contract | evidence / recall |
|---|---|---:|---:|---:|---:|---:|
| `pavlov99_json-rpc` | Python 2/3 compat code (bundled `six.py`, `basestring`/`unicode`), 25 subclasses, decorators, dynamic attribute access (13 fragments), Django/Flask backends | 166 | 397 | 166 / 13 | 10/10 | 128/128 / 38/38 |
| `pallets_itsdangerous` | bytes/str-heavy signing code (the central Py2→3 hazard; `byte[]` vs `String` in Java), exception hierarchy, `src/` layout | 81 | 231 | 81 / 11 | 10/10 | 104/104 / 35/35 |
| `jmespath_jmespath.py` | recursive-descent parser and interpreter with `getattr` visitor dispatch, generators, metaclass function registry | 206 | 643 | 206 / 12 | 10/10 | 272/272 / 41/41 |

**Findings worth knowing before Layer 1/2:**
- **jmespath's Java tests stub the code under test.** `FunctionsTest.java` declares its own `Jmespath`, `ArityError`, `VariadictArityError` and `JMESPathTypeError`, and `AstTest.java` its own `AST`. They pass without any translation. Layer 0 reports this as a warning and marks the hints `stub_in_tests`. It's the same RepoTransBench quality issue as the Java tests that reimplement logic inline.
- **json-rpc bundles `six.py`** (45 fragments, flagged `vendored`). It has no Java equivalent; Layer 1 should skip it and map its uses.
- **No real cycles in any of the three.** jmespath's parser recursion goes through `getattr(self, '_token_nud_' + type)`, which no static tool can resolve. Its only flagged recursion is `GraphvizVisitor._visit` calling itself. Dynamic dispatch like this is listed under `features.dynamic_attribute`.
- **Hazards are informative where it matters.** In json-rpc, 54 of 62 hazard sites are real Python 2 compatibility code: `unicode`, `basestring`, `iteritems`, `long`, `.next()`. In itsdangerous, all 14 are noise for a Python 3 source: `fix_future` on `from __future__ import annotations`, and `fix_import`. Overall, 29 of 90 sites (32%) are those two rules.

## Known limits

- **Static resolution only.** Dynamic dispatch (`getattr`, registries) produces no edges. The order is then still valid but less constrained, and the fragment is flagged via `features`.
- **Target hints are name-based.** A Java class whose name differs from the Python code isn't linked; the report lists such classes as "no match".
- **No target skeleton yet.** The proposal's skeleton scaffold (e.g. `pom.xml` plus empty Java classes from `expected_api`) is the natural next step, built from this bundle.
- **Large fragments.** Fragments over 150 LOC are flagged `oversized` but not split further.
- **Python 2 detection** means "some in-scope file is not valid Python 3 syntax". Dual 2/3 code (json-rpc) is reported as `python3`, with its hazards still attached.
