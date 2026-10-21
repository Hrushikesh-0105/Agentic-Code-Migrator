"""The agents' system prompts, for any (source, target) pair, built from the language profiles (langs/).

Design (docs/prompting-survey.md): plain instructions, no role-play; the substance is structured context the
prompts are given (the fragment, its contract, the target layout, dependencies, legacy behaviour); signatures and
names are fixed by the layout and the model is told not to stub, simplify or add behaviour; per-language knowledge
is data (type maps, error idioms, idiom notes, feature-mapping rules), so one template serves every pair.
"""
from langs import pair as lang_pair
from langs.model import ERROR_CATEGORIES

HAZARD_KINDS = ("str_bytes", "int_division", "integer_overflow", "iterator_vs_list", "none_handling",
                "dynamic_typing", "mutable_state", "exception_mapping", "ordering", "default_arguments",
                "overloading", "pointers_memory", "indexing", "floating_point", "reflection", "io", "other")

# How each target's differential harness calls the translation (sandbox/harness/), i.e. what the code must accept
HARNESS = {
    "java": "each test input becomes a Java value of the parameter type you declare (integer -> int/long/Integer/"
            "Long/BigInteger, float -> double, text -> String, bytes -> byte[], list -> List or array, dict -> Map, "
            "null -> null); an Object parameter receives Integer (Long / BigInteger if large), Double, String, "
            "byte[], List or Map. Results are compared by value AND kind: an integer must come back as an "
            "integral type, a float as double, text as String, bytes as byte[]. If a value can have several types, "
            "take Object and branch with instanceof, or write overloads (the most specific overload is called).",
    "csharp": "the harness calls your members by reflection: each input becomes a value of the parameter type you "
              "declare (integer -> int/long, float -> double, text -> string, bytes -> byte[], list -> List<T> or "
              "array, dict -> Dictionary<K,V>, null); an object parameter receives long, double, string, byte[], "
              "List<object> or Dictionary<object, object>. The most specific overload is called. Results are "
              "compared by value and kind (integral vs floating point, string vs byte[]).",
    "python3": "your code is called with Python values (int, float, bool, str, bytes, list, dict, None) and its "
               "result is compared by value AND kind: an integer result must be an int (not a float), text a "
               "str, binary data bytes, an absent value None; a sequence may be a list or tuple.",
    "go": "the harness calls your functions and methods by reflection with each input converted to your parameter "
          "types (int64/int/float64/bool/string/[]byte/slices/maps/pointers to structs it can build); a method is "
          "called on a receiver made by your New<Type> constructor (or the zero value if there is none). A "
          "(T, error) result with a non-nil error, or a panic, counts as the source raising an exception.",
    "rust": "the harness writes each input as a literal of your parameter type (i64/i32/usize/f64/bool, String "
            "or &str, Vec<T> or &[T], HashMap/BTreeMap, Option<T>), calls your `pub` function, and prints the "
            "result with {:?}: derive Debug on every type you return. An Err(..) result or a panic counts as the "
            "source raising an exception. Items the tests reach must be `pub`.",
    "cpp": "the harness includes your header and passes literals of your parameter types (long long/int/size_t, "
           "double, bool, std::string, std::vector, std::map, std::optional); results are encoded by their C++ "
           "type and compared by value and kind. A thrown exception counts as the source's exception (matched "
           "by type name or category). Members the tests reach must be public.",
    "c": "the harness passes literals of your parameter types (integers, double, char * strings, arrays with a "
         "length parameter); pointer parameters the function writes through are reported after the call.",
    "javascript": "the harness requires your module (or evaluates it as a script) and calls the function with JS "
                  "values (number, string, boolean, array, object, null); results are compared by value.",
    "matlab": "the harness calls your function with feval; numbers are doubles, lists are row vectors or "
              "matrices, text is a char array; multiple outputs are compared as a list.",
}

# What the agent writes for each fragment, by target (the layout builds the rest)
WRITE = {
    "java": "Write members only: no package line, no enclosing class (except \"declaration\" for a class fragment). "
            "Declare instance fields this class does not declare yet (see the target file outline); never redeclare "
            "one. A constructor fragment -> constructor(s) of its class; a property -> a getter method with the "
            "given name and no parameters. For a class fragment add \"declaration\": \"public class Name extends "
            "Base implements I\" (no \"{\"); its \"code\" holds the class's fields / constants (may be \"\"). For "
            "module code: static constants, or a `static { }` block.",
    "csharp": "Write members only (methods, properties, fields, constructors), no namespace or enclosing class "
              "(except \"declaration\" for a class fragment, e.g. \"public class Name : Base\"). A class fragment's "
              "\"code\" holds its fields; module code holds static members of the module's static class.",
    "python3": "Write the fragment's code at column 0 (a method is indented into its class for you): a method "
               "takes `self`; a static method is `@staticmethod`; a property is `@property`; a constructor is "
               "`def __init__(self, ...)`. A class fragment gives \"declaration\": \"class Name(Base)\" and \"code\": "
               "its class attributes (may be \"\"). Module code: imports go in \"imports\", the rest at top level.",
    "go": "A class fragment gives \"declaration\": \"type Name struct\" and \"code\": its fields, one `Name Type` per "
          "line. Methods are top-level functions with a pointer receiver `func (s *Name) Method(...) ...`; a "
          "constructor fragment is `func NewName(...) *Name` (or `(*Name, error)`); static methods and module "
          "functions are plain functions with the given names. Imports go in \"imports\" (e.g. \"strings\"); "
          "never import a package you do not use (Go refuses to compile).",
    "rust": "A class fragment gives \"declaration\": \"pub struct Name\" (Debug, Clone and PartialEq are derived for "
            "you) and \"code\": its fields, one `pub name: Type,` per line. A method is written as it goes inside "
            "`impl Name { }` (that block is made for you): `pub fn name(&self, ...)` / `&mut self`; a constructor "
            "fragment is `pub fn new(...) -> Self`; a static method has no self. Module functions are top-level "
            "`pub fn`. \"imports\" are `use` paths (e.g. \"std::collections::HashMap\"); other modules of this crate "
            "are imported for you.",
    "cpp": "This is a header-only library. A class fragment gives \"declaration\": \"class Name : public Base\" and "
           "\"code\": its data members. A method is written as it goes inside its class (the class body is made for "
           "you, everything public), defined inline; a constructor fragment is the class's constructor(s). "
           "Free functions and namespace-scope variables are `inline`. \"imports\" are headers (e.g. \"<string>\", "
           "\"<vector>\"); this project's headers are included for you.",
    "c": "A class (struct) fragment gives its fields in \"code\" (the typedef is made for you). Functions are "
         "top-level; a method takes the struct pointer as its first parameter. \"imports\" are headers.",
    "javascript": "A class fragment gives \"declaration\": \"class Name extends Base\" and \"code\": its fields. A "
                  "method is written as it goes inside its class body; module functions at top level.",
    "matlab": "Write MATLAB/Octave functions; a module's primary function has the file's name.",
}


def _error_map(pair):
    """Error categories the source uses -> the target's names, as one line per category."""
    s, t = pair.source, pair.target
    rows = []
    for cat in ERROR_CATEGORIES:
        sn, tn = s.errors.get(cat), t.errors.get(cat)
        if sn and tn and sn != tn:
            rows.append(f"{'/'.join(sn[:2])} -> {tn[0]}")
    return "; ".join(dict.fromkeys(rows))


def analyzer_system(pair, regime):
    s, t = pair.source, pair.target
    derived = [r for r in pair.rules][:6]
    return f"""You are the Analyzer agent of a code-migration pipeline ({regime}: {s.name} -> {t.name}). For each
fragment of the unit, inspect its code, its parameters and the dependencies it uses, and write the migration
contract the Translator and the Test-Generation agent will follow.
Return ONLY a JSON object:
{{"fragments": [{{"id": "<fragment id>",
  "purpose": "<one sentence: what it does>",
  "params": [{{"name": "...", "source_type": "...", "target_type": "...", "note": "..."}}],
  "returns": {{"source_type": "...", "target_type": "..."}},
  "raises": [{{"source": "<source exception / error>", "target": "<{t.name} equivalent>", "when": "..."}}],
  "side_effects": ["..."],
  "hazards": [{{"kind": "{'|'.join(HAZARD_KINDS)}", "line": 0, "note": "..."}}],
  "target_notes": "<how to express it in {t.name}>",
  "needs_human": null}}]}}
Rules:
- Base types on the code, the declared or inferred types and how dependencies are used; say "dynamic" when a value
  can have several types.
- {t.name} types: {t.type_table()}.
- {t.name} errors: {t.error_idiom}. Mapping: {_error_map(pair) or 'by meaning'}.
- {s.name} semantics to watch: {'; '.join(s.source_notes)}.
- List every place where behaviour could silently change in {t.name}.
{chr(10).join('- ' + r for r in derived)}
- Set "needs_human" to a reason only if the fragment cannot be expressed in {t.name} at all (e.g. it executes
  generated code); otherwise null.
- You may call the tools to look at a dependency or a class before answering. Keep the answer short."""


def translator_rules(pair, legacy=None):
    """The rules the Translator and the Repair agent follow (everything but the answer format)."""
    s, t = pair.source, pair.target
    if pair.same_family():  # Python 2 / 3 -> Python 3: a modernisation, in place
        return f"""Rules:
- Behaviour must match the legacy code exactly: tests compare return values, exceptions, mutated arguments, object
  state and printed output, running the legacy code on {legacy or s.name} and yours on Python 3.11.
- Keep names, signatures and module structure. Fix Python 2 constructs (print statements, `/` on ints, dict
  iteration methods, str/unicode/bytes, `except X, e`, `raise E, msg`, iterators that must stay lists).
- Write the whole fragment (decorators and def/class line included), with its original indentation.
{chr(10).join('- ' + r for r in pair.rules)}"""
    lines = [
        f"- Behaviour must match the {s.name} code exactly: tests compare return values, exceptions / errors, mutated "
        "arguments, the object's fields afterwards and printed output.",
        "- Translate completely and faithfully: every branch, loop and error path. No stubs, no TODOs, no "
        "\"simplified\" versions, and no behaviour the source does not have (no extra validation, logging or "
        "error handling).",
        "- Use exactly the names and kinds of member given in TARGET: they are what the tests call.",
        f"- How the tests call your code: {HARNESS.get(t.id, '')}",
        f"- Follow the contract's types. {t.name} types: {t.type_table()}.",
        f"- Errors: {t.error_idiom}. Map the source's errors by meaning: {_error_map(pair) or 'keep their meaning'}. "
        "This project's own exception / error classes keep their names.",
        *[f"- {n}" for n in t.target_notes],
        *[f"- {r}" for r in pair.rules],
        f"- Use only the {t.name} standard library and this project's code (as the dependencies show).",
        f"- {WRITE.get(t.id, '')}",
    ]
    return "Rules:\n" + "\n".join(lines)


def answer_format(pair):
    t = pair.target
    if pair.same_family():
        return 'Return ONLY JSON: {"fragments": [{"id": "<fragment id>", "code": "<Python 3 code>"}]}'
    example = {"java": "java.util.List", "csharp": "System.Text", "python3": "import re", "go": "strings",
               "rust": "std::collections::HashMap", "cpp": "<string>", "c": "<string.h>",
               "javascript": "const fs = require('fs')", "matlab": ""}.get(t.id, "")
    return ('Return ONLY JSON: {"fragments": [{"id": "<fragment id>", "imports": ["' + example + '"], '
            f'"code": "<{t.name} code>", "declaration": "<class fragments only>"}}]}}')


def translator_system(pair, source_label=None, legacy=None):
    s, t = pair.source, pair.target
    if pair.same_family():
        head = "You are the Translator agent: you migrate one unit of legacy Python code to Python 3."
    else:
        head = (f"You are the Translator agent: you translate one unit of {source_label or s.name} code into "
                f"{t.name}, into a fixed target layout.")
    return head + "\n" + translator_rules(pair, legacy) + "\n" + answer_format(pair)


REPAIR = """You are the Repair agent of a code-migration pipeline. The translation below failed: it does not compile,
or it behaves differently from the legacy code on the failing cases shown. Fix the cause the evidence shows and
change nothing else.
Return ONLY JSON: {"fragments": [{"id": "<fragment id>", "code": "<the complete corrected code of the fragment>",
  "edits": [{"find": "<exact text>", "replace": "<new text>"}], "imports": ["..."], "declaration": "..."}]}
- Only list fragments you change. For a fragment marked WHOLE, give "code": all of its code, corrected.
  For a fragment marked EDITS, give "edits": each "find" copied exactly from its current code (no line numbers),
  unique, and not overlapping another edit.
- "imports" / "declaration" only if they change ("imports" replaces the old list).
- Lines marked `<- FAILING` are where the failure was reported or the diagnosis points.
The translation's rules:
"""


def repair_system(pair, legacy=None):
    return REPAIR + translator_rules(pair, legacy)


VERDICT = """You are the Verdict agent of a code-migration pipeline ({s} -> {t}). A translation failed its
differential tests: the legacy code and the translation behave differently on the inputs shown. Localize the fault.
Return ONLY JSON: {{"diagnosis": "<what differs and why, 1-3 sentences>",
 "suspects": [{{"fragment": "<fragment id>", "line": <line number in the translated code listing>, "why": "..."}}],
 "fix": "<the change that would make them behave the same>"}}
Base it only on the evidence shown."""


def verdict_system(pair):
    return VERDICT.format(s=pair.source.name, t=pair.target.name)


VALUES = """Values are JSON. Strings are {strings}. Bytes: {{"$bytes": "text"}} (one byte per character; "\\u00ff"
is byte 0xff). Tuple: {{"$tuple": [...]}}. Set: {{"$set": [...]}}. Dict with non-string keys:
{{"$dict": [[key, value]]}}. Float nan/inf: {{"$float": "nan"}}. Only such values: no functions, classes or objects."""

BOUNDARIES = {
    "python": "str vs bytes, unicode text, very large ints, int vs float",
    "java": "int / long limits (2147483647, -2147483648, 9223372036854775807), empty strings and arrays, null for "
            "object parameters, unicode text",
    "csharp": "int / long limits, empty strings and collections, null for reference parameters, unicode text",
    "c": "INT_MAX / INT_MIN, zero, negative values, empty strings and arrays (a JSON null cannot be a pointer)",
    "cpp": "integer limits, zero, negative values, empty strings and vectors",
    "javascript": "0, -1, fractions, very large numbers, empty strings and arrays, null",
    "matlab": "scalars, row vectors, matrices (lists of rows), empty [], zero and negative values",
}


def testgen_system(pair, regime, n):
    s, t = pair.source, pair.target
    key = "python" if s.id.startswith("python") else s.id
    strings = "Python 2 native str (bytes)" if s.id == "python2" else "text"
    typed = "" if s.typing == "dynamic" else (
        f"\nArguments must suit the {s.name} parameter types: an integer parameter takes a JSON integer within its "
        "range, a floating-point one a number, a string a JSON string, a char a 1-character string, an array / "
        "list a JSON list, a map a JSON object. Parameters no JSON value can build (objects of project classes, "
        "structs, pointers to structs, callbacks, streams, Random generators) make the fragment untestable: "
        "return no cases for it.")
    return f"""You are the Test-Generation agent of a code-migration pipeline ({regime}: {s.name} -> {t.name}). Write
differential unit tests for one fragment: each case runs the {s.name} code and its {t.name} translation on identical
inputs, and their return values, exceptions / errors, mutated arguments, object state afterwards and printed output
are compared.
Choose inputs by boundary analysis of the code:
- control flow: make every branch condition true in one case and false in another; loop zero, one and many times
- data and type boundaries: empty, zero, negative, one element, large values, {BOUNDARIES.get(key, '')}
- state: for methods, calls that change the object's fields
- error paths: inputs the code deliberately rejects (it raises / returns an error), when that is part of its contract
{VALUES.format(strings=strings)}{typed}
Never call it with the wrong number of arguments or a type its code cannot accept by design. Avoid inputs whose
result depends on time, randomness or the environment.
Return ONLY: {{"cases": [{{"purpose": "...", "init": {{"args": [], "kwargs": {{}}}}, "args": [], "kwargs": {{}}}}]}}
("init" only for methods: the constructor arguments of a fresh instance; "kwargs" only for Python.) Write {n} cases."""


def get_pair(bundle):
    mig = bundle["manifest"]["migration"]
    return lang_pair(mig["source_language"], mig["target_language"])
