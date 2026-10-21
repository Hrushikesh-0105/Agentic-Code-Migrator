# Layer 2: multi-agent translation and verification

Layer 1 sends Layer 2 one unit at a time. A unit is one step of Layer 0's migration order: a fragment, or a set of fragments migrated together (a dependency cycle, or a class together with its constructor). Layer 2 translates the unit, tests whether the translation behaves like the legacy code, repairs it if it doesn't, and answers with a verdict:

```python
class Layer2(Protocol):
    name: str
    def process(self, request: dict) -> dict: ...   # TranslationRequest -> Verdict
```

The request and verdict formats, and how Layer 1 checks a verdict, are in [layer1/README.md](../layer1/README.md#the-layer-1--layer-2-protocol).

**Any language pair.** Nothing in Layer 2 is specific to one pair. Everything that depends on a language is data in [`langs/`](../langs/), which holds 10 language profiles plus feature-mapping rules per direction:
- the target layout ([`layout.py`](layout.py));
- the prompts ([`prompts.py`](prompts.py));
- the harness that runs the legacy code and the one that runs the translation ([`sandbox/polyglot.py`](../sandbox/polyglot.py));
- the toolchain that builds the translation;
- how results compare across the two languages ([`sandbox/compare.py`](../sandbox/compare.py)).

All 13 directions of RepoTransBench are covered: JS, C++, C, Java and Matlab → Python; Python → C++, Java, Rust and Go; Java → C# and Go; C → Rust; C# → Java.

There are two implementations:

| | |
|---|---|
| [`agentic.py`](agentic.py) `AgentLayer2` | **the real Layer 2**: six agents, MCP servers, the Docker sandbox, LLMs on Groq. `python -m layer1 --layer2 agents` |
| [`dummy.py`](dummy.py) `DummyLayer2` | passes every unit without translating (echo / stubs); scriptable failures. Used to test Layer 1 |

## The agents

```
request ─► Analyzer ─► Test-Generation ─► Translator ─► Executor & Sandbox ─► Verdict ──Equivalent──► pass
              │              │                 ▲               ▲                 │
              │       legacy run (oracle)      │               └──── Repair ◄────┘ Inequivalent (max 2 rounds)
              └──────── contract ──────────────┘                                  └─ still failing ─► fail (+ failure record)
```

| agent | model (Groq) | does |
|---|---|---|
| **Analyzer** ([`agents/analyzer.py`](agents/analyzer.py)) | `openai/gpt-oss-20b`: lightweight | Inspects the fragment, its parameters and dependencies, and writes a **migration contract**: source and target types for parameters and return, errors and their target equivalents, side effects, and **hazards** such as str/bytes, integer division and overflow, iterators, null, ordering, default arguments, overloading and pointers. It may call Layer 0's MCP tools (e.g. `get_dependencies`) before answering. Cached for the unit's later attempts. |
| **Test-Generation** ([`agents/testgen.py`](agents/testgen.py)) | `qwen/qwen3.8-27b` | Writes **differential test cases** by boundary analysis (see below for how they are checked). |
| **Translator** ([`agents/translator.py`](agents/translator.py)) | `openai/gpt-oss-120b`: heavy, open-weight | Writes the unit into the **target layout** following the contract. It sees: the target file as it stands (an outline from the workspace server); the translated API of every dependency; a few legacy results to reproduce; on later attempts, why the earlier ones failed; and any human guidance. For a target without overloading, an overloaded source name becomes separate functions, and the first one dispatches to the others. |
| **Executor & Sandbox** ([`agents/executor.py`](agents/executor.py)) | none | Assembles the **candidate tree**: the whole committed codebase plus this unit, through the workspace server. It builds it with the target's toolchain (javac, go build + vet, cargo build, g++ / gcc, dotnet build, node --check, Python compile) and runs the cases on the translation in that language's harness in the Docker sandbox. The legacy side already ran. Each compile error is attached to the fragment whose code holds the failing line. |
| **Verdict** ([`agents/verdict.py`](agents/verdict.py)) | `openai/gpt-oss-20b`: diagnosis only | The **decision is deterministic** ([`sandbox/compare.py`](../sandbox/compare.py)). It compares status, exception type, return value, mutated arguments, object state and stdout. On a mismatch it writes the **structured failure record** (see below); the LLM only adds a diagnosis of the suspect lines. |
| **Repair** ([`agents/repair.py`](agents/repair.py)) | `openai/gpt-oss-120b` | Patches the translation from the failure record. A fragment of up to 80 lines comes back whole and corrected; a longer one as find/replace edits, applied all-or-nothing. Every change is logged as a unified diff. Then the Executor and Verdict run again. |

**How the Test-Generation agent's cases are checked.** It targets:
- every branch condition Layer 0 found (staticfg), both ways;
- data and type boundaries: empty, zero, negative, large, None, str vs bytes, unicode;
- state mutations: methods run on a fresh instance, and the object's state afterwards is compared.

The cases run on the **legacy code first**, through the sandbox MCP server, in the source language's own harness (Python 2.7 / 3.11, javac + the Java harness, gcc / g++, dotnet, node, Octave):
- each case runs twice, and one whose runs differ (time, randomness) is dropped;
- a case that calls the code wrongly (arity, unknown name), that the source harness cannot express (e.g. a struct argument in C), or that crashes the harness is dropped;
- for Python sources, executed lines are traced, and if statements were never executed, the agent gets one more round with those lines (coverage-guided). The other harnesses do not trace lines yet.

The surviving cases and their legacy results are the oracle for every attempt.

**What the structured failure record contains.** For each mismatch:
- the failing inputs;
- the outcome on each side;
- the value and state diffs;
- the translation's stack trace;
- compile errors, with the fragment and line they belong to.

The Verdict LLM's diagnosis of the suspect lines is added to it. The Repair agent sees the lines that the compile errors and the diagnosis point at marked `<- FAILING` in the current code, as UniTrans does.

**Outcome for Layer 1.** Layer 2 tries up to `--max-repairs` repair rounds (default 2) per attempt. Layer 1's retries are the outer loop: 3 attempts per unit, each with the failure fed back.

| outcome | when |
|---|---|
| `pass` | it compiles and every differential test is equivalent. A unit with nothing callable (a header, a class shell) only needs to compile |
| `fail` | still inequivalent or not compiling after the repair rounds; the failure record goes back as evidence |
| `needs_human` | it failed, and the Analyzer said the code cannot be expressed in the target |
| `error` | the LLM's output or the infrastructure (MCP, Docker) failed |

**How a failure is fed back.**
- The Layer 1 verdict carries the evidence: tests generated, passed and failed, the failing case ids, and a one-line note (coverage, dropped cases, comparison notes).
- The full failure record is in `units/<unit>/attempt-<n>/layer2/`.
- The next attempt's Translator is given the failure summary and the code that failed.

## MCP: how the agents reach tools

Agents never run shell commands or open files. Every tool interaction is a JSON-RPC call to an **MCP server** (official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.3, stdio transport, protocol `2026-07-28`). The servers ([`servers/`](servers/)) are separate processes started by [`mcp_client.py`](mcp_client.py); each touches only the directories it was started with.

| server | architecture box | tools |
|---|---|---|
| `acm-analysis` ([`servers/analysis.py`](servers/analysis.py)) | Code Analysis Tools | `get_fragment`, `get_dependencies`, `get_dependents`, `find_fragments`, `get_module_header`, `get_class_members`, `get_target_location`, `get_expected_api`: read-only queries over the Layer 0 bundle |
| `acm-workspace` ([`servers/workspace.py`](servers/workspace.py)) | File System | `sync_committed`, `record_pass`, `get_pieces`, `assemble_candidate`, `read_file`, `outline_file`, `list_files`: the migrated project under construction |
| `acm-sandbox` ([`servers/sandbox.py`](servers/sandbox.py)) | Docker Sandbox, Test Frameworks | `run_python_cases`, `run_java_cases`, `run_cases` (any language), `build_tree` (any language's toolchain), `compare_results` (also across languages), `run_test_suite`: the hardened sandbox |

Logging & Reporting: the client logs every call (server, tool, caller, arguments, result size, seconds) to `layer2/mcp_calls.jsonl`.

There are two ways agents use the tools:
- **LLM-driven tool calls.** The Analyzer (Layer 0 tools) and Repair (workspace and Layer 0 tools) are offered MCP tools in OpenAI function-calling form, and the model chooses whether to call them (bounded rounds).
- **Agent code.** The Test-Gen, Translator, Executor and Verdict call the tools from their own code, which keeps prompts within Groq's per-minute token limit.

## The target layout

The layout decides, deterministically, where every fragment goes and what it is called. The Translator writes only member code. The layout's piece builder wraps it into Layer 1's compose pieces, so a model cannot put code in the wrong file or class. The piece builder is [`pieces.py`](pieces.py) for Java and [`layout_code.py`](layout_code.py) for other targets. It adds:
- file headers and imports, including imports of the project's own modules;
- class declarations with method slots;
- the scaffolding: `Cargo.toml` + `src/lib.rs`, `go.mod`, `__init__.py` and the C# namespace block.

Python → Python stays in place (splice assembly).

**Targets other than Java** ([`layout.py`](layout.py)):

| target | layout |
|---|---|
| Python, JavaScript, Matlab | One file per source module; a C/C++ header joins its source file. A class keeps its methods in an indented slot; functions and module code go at top level. |
| Go | One package. A struct, then its methods at top level with pointer receivers; constructor `NewX`; exported PascalCase names. |
| Rust | A crate: `src/<module>.rs` with `pub mod` lines in `lib.rs`. A struct (deriving `Debug`, `Clone`, `PartialEq`) plus an `impl` block slot; constructor `new`. |
| C++ | Header-only `include/<module>.hpp`. Classes have their methods inline and everything public; free functions are `inline`. |
| C | `src/<module>.c`. Structs, and functions at top level. |
| C# | One file per class in one namespace. Module-level code goes in a static `<Module>Module` class. |

How names are chosen:
- **The target tests' spelling first.** The benchmark target's tests and `project_structure.txt` are read by [`expected.py`](expected.py), and their spelling wins. For example, `from src.linkedlist import ...` places C's `list.c` at `src/linkedlist.py`, and `toStr` stays `toStr`.
- **Then the language's forced convention:** Go is exported PascalCase, C# is PascalCase.
- **Otherwise the source name is kept.**

Overloads get `_2`, `_3` and so on in languages without overloading.

**Java target** ([`skeleton.py`](skeleton.py), for Python and C# sources):

| Python | Java |
|---|---|
| class `C` | class `C`; a nested class becomes a static nested class |
| module functions, constants, init code | static members of one container class per module. Its name is the class the target tests expect (e.g. `EncodingUtils`) when Layer 0 linked one, else `<Module>Module` |
| method `want_bytes` | `wantBytes` (or the name the target tests expect) |
| special methods | `__init__` → constructors, `__str__` → `toString`, `__eq__` → `equals`, `__hash__` → `hashCode`; a property becomes a getter |
| package | the expected API's package, else the target package plus the Python package path |

A Java class does not compile without its constructors, so for Java, C# and C++ targets Layer 0 puts a class and its own constructor in one unit (edge kind `init`). For a C# source, constructors, statics and properties are found by C#'s own rules ([`source.py`](source.py)).

## The prompts

[`prompts.py`](prompts.py) builds every agent's system prompt from the two language profiles and the pair's rules. The design follows the survey in [docs/prompting-survey.md](../docs/prompting-survey.md):
- plain instructions with the substance in structured context;
- names and kinds of members are fixed by the layout;
- no stubs, no "simplified" versions, no added behaviour;
- legacy input/output examples go into the Translator's prompt;
- repair works from failing input, expected and actual output, with the failing lines marked.

Parts of a prompt:

| part | from |
|---|---|
| types | the target's neutral → target type map (e.g. `list -> Vec<T>`) |
| errors | the target's error idiom (exceptions, `(T, error)`, `Result`) and a source → target error mapping by category |
| how the tests call the code | per target harness (reflection with type conversion, typed literals from the signature, ...) |
| what to write | per target layout (members in a slot, receivers at top level, header-only inline code, ...) |
| idioms and pitfalls | the target's notes and the source's semantics to watch (overflow, floor vs truncating division, ordering, ownership) |
| feature mapping | derived rules (dynamic → static typing, exceptions → Go errors / Rust `Result`, pointers → values) plus rules specific to the 12 benchmark pairs |
| test values | JSON with markers for bytes, tuples, sets and non-string keys, and the boundary values typical of the source language |

## The cross-language harness

A test case runs on both sides, each in its own language's harness, and the results are compared in one type-tagged format. The Python and Java harnesses are described below. Go, Rust, C++, C, C#, JavaScript and Octave are in [`sandbox/polyglot.py`](../sandbox/polyglot.py) and [`sandbox/harness/`](../sandbox/harness/):
- reflective harnesses for C#, JavaScript and Octave;
- a generated registry plus reflection for Go;
- generated drivers with literals typed from the parsed signature for Rust, C++ and C.

**Python side** ([`sandbox/harness/runner.py`](../sandbox/harness/runner.py)):
- one fresh interpreter per case;
- records the exception class hierarchy, so exceptions can be matched across languages;
- `attr` reads a property; `trace` records the executed lines.

**Java side** ([`sandbox/harness/AcmHarness.java`](../sandbox/harness/AcmHarness.java)), compiled with the translation:
- calls the static method, constructor or instance method by reflection;
- converts the arguments to the declared parameter types, choosing the most specific overload as Java would (an int goes to `long` before `double`);
- runs every case in a fresh class loader, so static state cannot leak between cases, with a timeout;
- encodes results in the same type-tagged JSON;
- a missing class or method, or a call no overload accepts, is `unresolved`: an API defect, not a behaviour.

**Comparison profiles** ([`sandbox/compare.py`](../sandbox/compare.py)): `cross:<source>-><target>` for any pair. `python-java` and `python2-java` are names for the Python → Java profiles; under `python2-java`, a native `str` may match a Java `String`.
- Text and bytes stay distinct.
- Sequences compare by items, whatever the container.
- Objects compare by class name and by the fields both sides have, with names normalised (`_secret_key` ~ `secretKey`).
- Exceptions match by name for project exceptions, or by error category for built-in ones (`ValueError` ~ `IllegalArgumentException` ~ `std::invalid_argument` ~ `ArgumentException`). A Go `error`, a Rust `Err` or panic, or a Matlab `MException` stands for any category.
- Numbers compare by value across int and float when JavaScript or Matlab is involved, since those languages have one number type.

## LLMs

All agents use Groq ([`llm.py`](llm.py)). Each agent has a chain of models: its intended tier first, the others when that one's daily quota is spent. Set `ACM_L2_MODEL_<AGENT>` to override a chain.

**Limits of this key.** Every model allows 8,000 tokens per minute and 1,000 requests per day, and a request's prompt plus `max_tokens` must fit in the minute's limit. So:
- calls are paced per model;
- prompts are built to a budget, with the least important sections shortened first;
- agents on different models proceed in parallel budgets.

**When the quota runs out.** When every model of a chain is out of daily quota, the run **stops** (it does not fail units), checkpointed. `python -m layer1 --resume <run>/<repo>` continues it.

Every call is logged to `layer2/llm_calls.jsonl`: agent, model, messages, answer, tool calls, usage.

## Running it

```bash
.venv/Scripts/python -m layer2 --verify                     # known-answer checks (no API key needed)
.venv/Scripts/python -m layer1 --layer2 agents --scope itsdangerous.encoding pallets_itsdangerous
.venv/Scripts/python -m layer1 --layer2 agents --bundles logs/layer0/<run> --scope jmespath.lexer
```

`--scope` migrates the named fragments, modules or classes and everything they depend on; the rest is `out_of_scope`. A whole repository costs more than one day of this key's free quota.

**Output**, under `logs/layer1/<run>/<repo>/`:
- `units/<unit>/attempt-<n>/layer2/`:
  - the contract;
  - each translation round, with its pieces and repair diffs;
  - execution reports;
  - failure records;
  - Layer 2's verdict;
  - sandbox logs.
- `layer2/`:
  - `llm_calls.jsonl`, `mcp_calls.jsonl`;
  - `units/<unit>/{contract,tests,attempts}.json`, kept across attempts;
  - `workspace/` with the committed pieces and candidate trees;
  - the servers' stderr logs.
- `layer2_summary.json`, and the "Layer 2 verification" section of `report.md`.

## Testing

`python -m layer2 --verify` runs Layer 0 → Layer 1 → Layer 2 on the Layer 0 fixture (a Python 2 package, Java target) through the real MCP servers and Docker sandbox, with a **scripted LLM**, so every outcome is known in advance. See the docstring of [`verify.py`](verify.py) for the cases.

`python -m layer2 --verify-pairs [SRC->TGT ...]` does the same for the other 12 benchmark directions. It uses one small fixture per source language: `twice`, `safe_div` and a `Counter` class. Each pair is translated, built with the target toolchain, and tested in both languages' harnesses. Bugs are planted and must be caught and repaired:
- Python → Rust: truncating division;
- C → Python: truncating division;
- Python → Go: an unused import that fails to compile.

See [`verify_pairs.py`](verify_pairs.py).

## Results (2026-10-10): every language pair

**Known-answer verification:**
- `python -m layer2 --verify-pairs` → **17/17**, with **99/99** differential tests equivalent over the 12 pairs. All three planted bugs were caught and repaired:
  - Python → Rust: truncating division;
  - C → Python: truncating division;
  - Python → Go: an unused import that fails to compile.
- `python -m layer2 --verify` (Python 2 → Java) → **16/16**.
- Regressions:
  - Layer 1 `--verify`: 30/30;
  - Layer 0 `--verify`: 17/17 + 10/10;
  - `sandbox.polyglot_verify`: 11/11.
- A layout smoke test assembled jmespath for Go, Rust, C++, C#, JavaScript, C and Python with placeholder code, and every one built with its toolchain.

**Live runs on Groq**, one slice of a real RepoTransBench project per pair:

| pair | project, scope | units | differential tests | what happened |
|---|---|---:|---:|---|
| Java → Python | `whwlsfb_JDumpSpider`, `cn.wanghw.utils.Base64` | 8/9 migrated | **64/64** on 6 units | `removeWhiteSpace` mutates a `char[]`, compared as an argument afterwards. `decode` escalated on a prompt-budget overflow, since fixed. |
| Python → Rust | `asciimoo_drawille`: `normalize`, `get_pos`, `line`, `polygon` | 4/5 | **30/30** on 3 units | `normalize` emulates Python's banker's rounding and `get_pos` its floor division, both on the first try. `polygon` escalated: default arguments, `IntoIter` results and mixed int/float output, all three handled since. |
| C# → Java | `bolorundurowb_shortid`: `GenerationOptions`, `SetCharacters` | 10/10 | **17/17** on 2 units | C# default parameters became overloads. A difference in the constructor's `length = null` path was caught and fixed on attempt 3. |
| C → Rust | `cesanta_slre`: `hextoi`, `get_op_len`, `is_quantifier`, `is_metacharacter` + deps | 8/9 | **24/24** on 3 units | C's `int` booleans (`1`) vs Rust `true` were caught and repaired. `get_op_len` escalated on a C-harness defect, since fixed. |
| JavaScript → Python | `codemeasandwich_graphql-query-builder`: `getGraphQLValue`, `objectToString` | 1/2 | 19/20 | Escalated on a real difference that 3 attempts did not fix: JS returns the boolean `true` unchanged, the translation returned `'true'`. |

Together with the 2026-10-09 Python → Java runs, 6 of the 13 directions have run live. The other 7 have run only in `--verify-pairs`.

**Found and fixed during the live runs** (all generic, none specific to a project):
- Layout:
  - a class declaration written with its body (`class X:\n    pass`) is cut to the declaration;
  - member code is dedented before it is placed in a slot.
- Prompts:
  - token budgets count the real system prompt and leave room for tool results;
  - a parameter that takes both ints and floats becomes the target's float type, never a generic, trait or JSON-value type;
  - Rust's `any` no longer suggests `serde_json`.
- Comparator:
  - scalar arguments are not compared afterwards for sources that cannot write through parameters;
  - a legacy `TypeError` on a value the target's types cannot receive is not applicable, with a note;
  - a dynamic source's mixed int/float sequence compares by value.
- Test-Generation: Go, Rust and C calls spell out the source's literal defaults.
- Harnesses:
  - Rust iterator wrappers (`IntoIter([...])`) read as their items;
  - C `const unsigned char *` takes strings;
  - C arguments afterwards stay aligned with the other language.
- Legacy builds:
  - a Java legacy project compiles only the files under test, with `-sourcepath`, so missing jars elsewhere do not matter;
  - C/C++ falls back to the files under test alone;
  - C# sets aside `global.json` and `Directory.*` files, which pinned a newer SDK.
- Repair: Groq's "Failed to validate JSON" counts as a bad answer and is retried. A repair that breaks off ends the attempt as `fail` with its evidence kept, not as an infrastructure error.

**Limits**
- **Quota:** these five runs used about 170k tokens of `gpt-oss-120b`'s 200k daily limit, after which qwen took over.
- **Line coverage:** only the Python harness traces executed lines, so the coverage-guided round runs for Python sources only.
- **Untestable signatures:** structs, pointers to structs, callbacks and streams cannot be built from JSON. Such fragments are compile-checked only.
- **RepoTransBench's own target tests** have still not been run against the migrated code. Many need third-party packages (testify, serde_json, gtest) that the offline sandbox lacks.

## Results (2026-10-09)

**Known-answer verification:** `python -m layer2 --verify` → **16/16** (`logs/layer2/20261009-201629-verify/`). The regression suites still pass after the Layer 0/1 changes: Layer 0 `--verify` 17/17 + 10/10, Layer 1 `--verify` 30/30.

**Live runs on Groq** (Layer 0 → Layer 1 → agents; the free quota allows a slice of a repository per day):

| run | scope | units | outcome | differential tests | repairs | LLM tokens |
|---|---|---:|---|---:|---:|---:|
| `20261009-192237-pallets_itsdangerous` | `itsdangerous.encoding`, `itsdangerous.exc` | 14 | 12 migrated, 2 escalated; contract 10/10; 8 Java files compile | 65/65 equivalent on the 8 tested units | 6 units | ~240k |
| `20261009-201617-jmespath_jmespath.py` | `jmespath.ast` | 22 | 22 migrated; contract 10/10; compiles | **170/170** equivalent, all 22 units tested | 2 units | ~137k |

**itsdangerous: bugs the tests caught, and the agents repaired**
- `bytes_to_int`: 8 bytes of `0xff` gave `-1` in Java (signed `long`). Python's result is unsigned (18446744073709551615); the repair uses `BigInteger`.
- `want_bytes`: the `errors="ignore"/"replace"` modes were dropped. Tests with `latin-1`, `ascii` and unicode inputs caught it.
- `BadSignature` and the other exception classes: field state (`message`, `payload`) compared after construction.

**itsdangerous: escalated with an LLM brief**
- `base64_decode`: CPython's non-strict `urlsafe_b64decode` silently drops invalid characters, except when the padding is wrong. Every repair flipped one side of that rule; that is a faithful-emulation problem for a human.
- `int_to_bytes`: it raises `struct.error`, which has no Java counterpart. The comparator then only accepted same-named exceptions. Fixed during the run (library exceptions now match any unchecked Java exception, with a note); the unit had already used its attempts.

**Found and fixed during the live runs:**
- overload choice in the Java harness (now the most specific overload);
- scaffold de-duplication (pieces normalised to the protocol form);
- half-applied repair edits (now all-or-nothing, with whole-fragment rewrites for short fragments);
- gpt-oss tool-call quirks: calling a nonexistent `json` tool, and calling a tool when told not to (the answer turn now offers no tools);
- guessed import packages (imports are now checked against the skeleton);
- constructor arguments placed under `init`;
- a class's constructor declared twice;
- classes that cannot compile without their constructor (Layer 0's `init` edge).

**Limits**
- **Quota:** `gpt-oss-120b` allows 200,000 tokens/day on this key. Translation and repair fall back to `qwen3.8-27b`, and a run stops resumably when every model is spent. Repairs are the costliest part.
- **Coverage of the code paths:**
  - (2026-10-09) Only Java targets had been exercised then; see the 2026-10-10 results above for the other pairs.
  - Units run one at a time.
  - RepoTransBench's own Java tests have not been run against the migrated code.
- **Testability:** a unit whose class's constructor is not yet migrated, or with nothing callable (headers, class shells), is only compile-checked. The report lists these units separately.
