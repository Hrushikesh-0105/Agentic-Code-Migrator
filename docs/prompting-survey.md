# How published LLM code-translation systems prompt their agents

Survey made on 2026-10-10 to design the language-generic prompts of Layer 2 (`langs/`, `layer2/agents/`).

**Legend:** **[V]** means verbatim text, read in a repo or a paper appendix. **[P]** means a paraphrase of the paper, because the prompt text was not available.

## Systems

### RepoTransBench / RepoTransAgent (Wang et al., arXiv 2412.17744)
Links: https://arxiv.org/html/2412.17744v2 and https://github.com/DeepSoftwareAnalytics/RepoTransBench

- **Translator.** One ReAct agent with five actions: `CreateFile`, `ReadFile`, `ExecuteCommand`, `SearchContent`, `Finished`.
- **Translator prompt [V]**, from `RepoTransAgent/prompts/system_prompt.py`:
  - opening: "You are a code translator. Translate {source_language} to {target_language} for project {project_name}."
  - sections: GOAL ("Make all tests pass"), PROJECT STRUCTURE (the target layout), SOURCE FILES (5 sample files), PUBLIC TESTS, TEST RUNNER SCRIPT, then WORKFLOW (generate → test → fix);
  - rules: "Implement COMPLETE functionality (no stubs)" and "Use public tests as your specification reference".
- **Feedback.** After every action the tests run again. The agent gets the compile status (first 500 characters), "Tests: x/y passed", the first 3 failing tests, and a per-module pass/fail list.
- **Per-language info.** The benchmark's test-translator agent has a `{language_specific_info}` block per target language: framework, build tool, layout, commands, naming, assertion idioms. For example, Rust: "`assert_eq!()`… `#[should_panic]`… `Result<(), Box<dyn Error>>`".
- **Findings.**
  - Static→dynamic pairs reach 45–63%; dynamic→static pairs mostly score below 10%.
  - Error taxonomy: build configuration; limited understanding (redeclarations, wrong argument types from dynamic typing); incomplete generation (truncation, missing imports); language-feature violations; encoding.

### RepoTransAgent (arXiv 2508.17720, C#→Java)
Link: https://arxiv.org/html/2508.17720v1

- **Agents.** A RAG agent finds similar target-repo functions. A Context agent queries tools that return JSON class, import and method info. A Refine agent loops generate → test → reflect → regenerate.
- **Ablation (pass rate).**

| Configuration | Pass rate |
|---|---|
| Full | 45.8% |
| Without Context agent | 32.2% |
| Without Refine agent | 26.5% |
| No agent | 18.6% |

- Without context the model "replicate[s] the implementation of the source function and disregard[s] the implementation differences".

### TransAgent (Yuan et al., arXiv 2409.19894)
Link: https://arxiv.org/html/2409.19894v5

- **Roles.** Translator plus test generator, Syntax Error Fixer, Code Aligner, Semantic Error Fixer.
- **Test generation [V].** "Please generate five inputs for the given source program"; the outputs come from executing the source.
- **Syntax fixer [P].** Two steps: compiler message → a concrete fixing suggestion → patch. It stops when the same error recurs at the same place.
- **Semantic fixer [P].** Source and target are mapped block by block over the control-flow graph; the first divergent block is localized. The prompt is cloze-style and optionally carries runtime values ("value-aware" mode).
- **Ablation (Py→Java).** 49.5% → 82.4% with the syntax fixer → 89.5% for the full system.

### UniTrans (Yang et al., FSE 2024)
Links: https://arxiv.org/html/2404.14646v1 and https://github.com/yz1019117968/FSE-24-UniTrans

The prompts below are [V]:

- **Test inputs:** "Please generate ten groups of differentiated valid inputs for the above focal method of ${pl_src} language". Outputs come from running the source.
- **Translate:** "Given ${pl_src} code… Given test cases:${TC_tar} Translate given ${pl_src} code to ${pl_tar} code, and ensure the translated ${pl_tar} code can pass all given test cases."
- **Repair:** "Given buggy ${pl_tar} code… Given test case… Error message: ${err_msg} Fix the buggy line (marked ${com_sym_tar} <Buggy Line>)." The failing line is marked with a target-language comment.

Findings:
- Logic errors are 38.5% of failures; I/O type mismatches from dynamic↔static typing are 14.9%.
- Tests in the prompt gave up to +23.8%; repair gave +2–4%, with a useful budget of 1–2 rounds.

### Lost in Translation (Pan et al., ICSE 2024)
Link: https://github.com/Intelligent-CAT-Lab/PLTranslationEmpirical/blob/main/prompts/README.md

- **Repair prompts [V].**
  - "You were asked to translate the following $SOURCE_LANG code… Your response was… Executing your generated code gives the following error… do not change the method signature… Make sure your generated code is syntactically correct."
  - The wrong-output variant gives the test input, the expected output and the actual output.
- **Findings.** The most common bugs are violating target-language requirements (24.3%) and data/type/format issues (33.5%). Repair helps in round 1 (+12.3% for GPT-4) and much less in round 2 (+1.7%).

### AlphaTrans (Ibrahimzada et al., FSE 2025; Java→Python)
Links: https://arxiv.org/html/2410.24117v5 and https://github.com/Intelligent-CAT-Lab/AlphaTrans

- **Pipeline.** Symbolic refactoring (overloads renamed; constructors become factories), CodeQL decomposition, a type-translation prompt, a skeleton, then fragments translated in reverse call order.
- **Fragment prompt [V].** "Translate the following {from_lang} {fragment_type} to {to_lang}… You only need to translate the "{name}" {fragment_type}. All necessary dependencies are available in partial {to_lang} translation". Then the source, then the partial translation (imports, class, used fields, superclasses, translated callees). The response is **prefilled with the skeleton signature**.
- **Feedback [V].** "Incorrect … translation", "Execution feedback:" (trace or assertion), and "You only need to correct the "{name}"… do not add anything else."
- **Findings.**
  - Without the overloading refactor, equivalence drops from 25.1% to 3.05%.
  - GPT-4o "adds unnecessary error handling".

### Syzygy (arXiv 2412.14234; C→safe Rust)
Link: https://arxiv.org/abs/2412.14234

- **Prompt [V].** "You have already translated the following C code to Rust… Now you will translate… Do NOT modify or rewrite any existing Rust code… Precisely follow the program logic… MINIMIZE the differences… Do NOT leave placeholder code". It adds C→Rust rules: arrays → `Vec`, function pointers → closures, keep length variables, keep nullability and aliasing.
- **Annotations.** Dynamic-analysis annotations: nullability, allocation sizes, aliasing.
- **Tests.** C inputs and outputs are captured as JSON; equivalence tests compare the return value *and* argument state after the call.
- **Finding.** "LLMs eagerly modify the C program's logic (usually… optimizing or simplifying)."

### SACTOR (arXiv 2503.12511), C2SaferRust (arXiv 2501.14257), RustMap (arXiv 2503.17741)
- **Two stages.** Unidiomatic (unsafe allowed) first, then idiomatic.
- **Compile feedback [V].** "It failed to compile with the following error message… think about the possible reasons, and try to avoid this error."
- **RustMap checklist [P].** `mut`, `&`/`&mut`, lifetimes, numeric conversions, bounds, overflow. Units are the call graph's strongly connected components.

### Oxidizer (PLDI 2025; Go→Rust)
Link: https://arxiv.org/html/2412.08035

- **Feature mapping rules.** Each is a pattern + a description injected into the prompt + a static check. Examples: `(T, error)` → `Result`; structural interfaces → traits.
- **Approach.** Types are translated first; dependencies are summarized as signatures only; repair freezes signatures.
- **Ablation.** Without the rules, those constructs don't compile.

### FLOURINE (arXiv 2405.11514)
Link: https://arxiv.org/html/2405.11514v3

- Cross-language differential fuzzing with JSON serialization.
- Restarting with positive and negative I/O examples in the prompt beat conversational repair; random counterexamples are hard for models.

### Self-Debugging (Chen et al., ICLR 2024)
Link: https://arxiv.org/abs/2304.05128

- **Feedback [V].** "The Python translation does not do the same thing as the C++ code. These are the results of failed unit tests… Failed: assert f(9) == 1 Actual Result: 1.8 Correct the translation."
- **Results.** Unit-test feedback took accuracy from 77.3 to 88.8; adding an explanation gave 90.4. Over 30% of fixes addressed cross-language differences such as integer division.

### SpecTra (arXiv 2405.18574), K3Trans (arXiv 2503.18305), CodeTransOcean (arXiv 2310.04951)
- **SpecTra.** I/O specifications helped most; adding specifications one at a time beat all at once.
- **K3Trans.** *Dependency usage examples* were the largest gain (49.1% → 62.9%).
- **CodeTransOcean.**
  - Role-play, politeness, usage clarifications and chain-of-thought each lowered the executable rate.
  - A same-pair one-shot example helped.
  - Self-debugging plateaued after about 2 rounds.

## What Layer 2 takes from this

| principle | evidence | where |
|---|---|---|
| short, plain instructions; substance in structured context | CodeTransOcean, RepoTransAgent ablation, K3Trans | `layer2/agents/prompts.py` |
| translate in dependency order; give translated callee signatures and the target file; lock signatures | AlphaTrans, Oxidizer, Syzygy, Hong & Ryu | Translator: skeleton + required signatures; Repair keeps signatures |
| "do not modify existing code / do not simplify or optimize / no stubs" | Syzygy, RepoTransBench | rules in every generation prompt |
| tests: neutral JSON inputs, outputs from executing the source; compare return value and post-call argument state | UniTrans, TransAgent, Syzygy, Oxidizer, FLOURINE | Test-Gen + harnesses |
| tests shown in the translation prompt (I/O examples) | UniTrans (+23.8%), SpecTra, FLOURINE | Translator "BEHAVIOUR TO REPRODUCE" |
| repair: failing input / expected / actual; compile error at the marked line (target comment syntax); state the cause before the patch; ≤2 rounds; stop on a repeated error | UniTrans, Pan, TransAgent, Self-Debugging | Verdict record + Repair |
| per-language knowledge as data: type map, error idiom, naming, layout, toolchain, ownership/memory notes, pair feature rules | Oxidizer, RepoTransBench, Syzygy, RustMap, AlphaTrans | `langs/` |
| dynamic→static: infer types from tests and traces first | RepoTransBench direction gap, Syzygy | Analyzer contract + Test-Gen observed types |
