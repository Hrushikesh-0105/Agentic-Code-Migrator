"""The language profiles. Data only; the meaning of each field is in model.py.

Notes are short, factual and about *behaviour*: they are injected into prompts, and published work shows that
substance (type maps, error idioms, pitfalls) helps and verbose instruction does not (docs/prompting-survey.md).
Toolchain versions follow RepoTransBench's docker/Dockerfile (Ubuntu 22.04, Python 3.11, OpenJDK 17, .NET 7,
Node 18, Go 1.21, Rust stable, GCC 11) plus Python 2.7 and GNU Octave for legacy sides.
"""
from .model import Language

PY_ERRORS = {
    "value": ["ValueError"], "type": ["TypeError"], "key": ["KeyError"], "index": ["IndexError"],
    "attribute": ["AttributeError"], "not_implemented": ["NotImplementedError"],
    "zero_division": ["ZeroDivisionError"], "overflow": ["OverflowError"], "io": ["OSError", "IOError"],
    "assertion": ["AssertionError"], "stop_iteration": ["StopIteration"], "recursion": ["RecursionError"],
    "memory": ["MemoryError"], "unicode": ["UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError"],
    "runtime": ["RuntimeError"], "generic": ["Exception"], "null": [],
}
PY_DUNDERS = {"__init__": "<constructor>", "__str__": "__str__", "__repr__": "__repr__", "__eq__": "__eq__",
              "__hash__": "__hash__", "__len__": "__len__", "__iter__": "__iter__"}

PYTHON3 = Language(
    id="python3", name="Python", aliases=("python", "py", "py3", "python3.11"), extensions=(".py",),
    line_comment="#", typing="dynamic", paradigm="oop", grammar="python",
    naming={"function": "snake_case", "method": "snake_case", "class": "PascalCase", "constant": "UPPER_SNAKE",
            "field": "snake_case", "module": "snake_case", "package": "snake_case"},
    types={"int": "int", "float": "float", "bool": "bool", "str": "str", "bytes": "bytes", "list": "list[{0}]",
           "dict": "dict[{0}, {1}]", "set": "set[{0}]", "tuple": "tuple", "optional": "{0} | None",
           "any": "Any", "none": "None", "char": "str", "callable": "Callable"},
    error_idiom="exceptions: `raise ValueError(...)`; callers use try/except",
    errors=PY_ERRORS, layout="module_files", assembly="compose",
    file_layout={"path": "{module_path}.py", "container": None, "methods": "slot",
                 "class": "{declaration}:\n{fields}\n{slot}", "package_init": "__init__.py"},
    target_notes=(
        "ints are unbounded; `/` is true division (float), `//` floors toward negative infinity, `%` takes the "
        "sign of the divisor",
        "str is Unicode text, bytes is binary; never mix them",
        "dicts keep insertion order; sets do not have an order",
        "None is the only null; empty containers and 0 are falsy",
        "use only the standard library unless the project already depends on a package",
        "keep module, class and function names in snake_case / PascalCase as given",
    ),
    source_notes=(
        "dynamic typing: one parameter may receive several types (infer them from the code and the tests)",
        "ints are unbounded (no overflow); `/` is true division; `//` floors toward negative infinity",
        "str (text) and bytes are distinct types",
        "default arguments, keyword arguments, *args and **kwargs",
        "truthiness: empty containers, 0, '' and None are false",
        "generators and iterators are lazy",
    ),
    toolchain={"image": "py3", "compile": "python -m compileall -q .", "test": "python -m pytest -q",
               "run_file": "python {file}"},
    harness="python", special_methods=PY_DUNDERS,
)

PYTHON2 = Language(
    id="python2", name="Python 2", aliases=("py2", "python2.7"), extensions=(".py",),
    line_comment="#", typing="dynamic", paradigm="oop", grammar="python",
    naming=PYTHON3.naming, types={**PYTHON3.types, "str": "str (bytes) / unicode", "int": "int / long"},
    error_idiom="exceptions: `raise ValueError, msg` or `raise ValueError(msg)`",
    errors=PY_ERRORS, layout="module_files", assembly="splice",
    file_layout=PYTHON3.file_layout,
    source_notes=(
        "`/` on two ints is FLOOR division (Python 3 makes it true division)",
        "the native str is bytes; text is `unicode`; implicit ASCII conversions between them",
        "dict.keys()/values()/items(), map, filter, zip and range return LISTS (Python 3: views / iterators)",
        "print is a statement; `except E, e`; `raise E, msg`; long integers with an L suffix",
        "dict iteration order is arbitrary (hash order)",
    ),
    toolchain={"image": "py27", "test": "python -m pytest -q"},
    harness="python", special_methods={**PY_DUNDERS, "__nonzero__": "__bool__", "__unicode__": "__str__"},
)

JAVA = Language(
    id="java", name="Java", aliases=("java17",), extensions=(".java",), line_comment="//", typing="static",
    paradigm="oop", grammar="java",
    naming={"function": "camelCase", "method": "camelCase", "class": "PascalCase", "constant": "UPPER_SNAKE",
            "field": "camelCase", "module": "PascalCase", "package": "lowercase"},
    types={"int": "long", "float": "double", "bool": "boolean", "str": "String", "bytes": "byte[]",
           "list": "List<{0}>", "dict": "Map<{0}, {1}>", "set": "Set<{0}>", "tuple": "List<Object>",
           "optional": "{0} (nullable)", "any": "Object", "none": "void", "char": "char",
           "callable": "java.util.function.Function<...>"},
    error_idiom="unchecked exceptions: `throw new IllegalArgumentException(...)`; project errors extend "
                "RuntimeException",
    errors={"value": ["IllegalArgumentException", "NumberFormatException"],
            "type": ["IllegalArgumentException", "ClassCastException"], "key": ["NoSuchElementException"],
            "index": ["IndexOutOfBoundsException", "ArrayIndexOutOfBoundsException",
                      "StringIndexOutOfBoundsException"],
            "attribute": ["NullPointerException"], "not_implemented": ["UnsupportedOperationException"],
            "zero_division": ["ArithmeticException"], "overflow": ["ArithmeticException"],
            "io": ["UncheckedIOException", "IOException"], "assertion": ["AssertionError"],
            "stop_iteration": ["NoSuchElementException"], "recursion": ["StackOverflowError"],
            "memory": ["OutOfMemoryError"], "null": ["NullPointerException"],
            "unicode": ["IllegalArgumentException", "CharacterCodingException"],
            "runtime": ["IllegalStateException", "RuntimeException"], "generic": ["RuntimeException", "Exception"]},
    layout="class_files", assembly="compose",
    file_layout={"path": "src/main/java/{package_path}/{name}.java", "container": "{Module}Module",
                 "package_container": "{Module}Package", "methods": "slot",
                 "class": "{declaration} {{\n{fields}\n{slot}\n}}", "nested_class": "static"},
    target_notes=(
        "int is 32-bit and long 64-bit and both overflow silently: use long, or BigInteger for unbounded values",
        "`/` and `%` on integers truncate toward zero (Python floors): use Math.floorDiv / Math.floorMod when "
        "operands can be negative",
        "no default or keyword arguments: write overloads for the shorter argument lists; *args -> varargs",
        "compare objects with equals(), strings with equals(); == is identity",
        "String is UTF-16 text, byte[] is binary; String.getBytes(StandardCharsets.UTF_8)",
        "LinkedHashMap / LinkedHashSet keep insertion order (HashMap does not)",
        "Java does not inherit constructors: a subclass declares its own and calls super(...)",
    ),
    source_notes=(
        "int (32-bit) and long (64-bit) overflow silently; integer `/` and `%` truncate toward zero",
        "method overloading (same name, different parameter types) and constructor overloading",
        "checked exceptions, inheritance, interfaces, generics with type erasure, static members",
        "== compares references for objects; equals() compares values",
        "char is a UTF-16 code unit; String is immutable",
    ),
    toolchain={"image": "java17", "build": "javac", "test": "mvn -o -B -q test"},
    harness="java",
    special_methods={"__init__": "<constructor>", "__str__": "toString", "__repr__": "repr", "__eq__": "equals",
                     "__hash__": "hashCode", "__len__": "size", "__iter__": "iterator", "__call__": "call",
                     "__contains__": "contains", "__getitem__": "get", "__lt__": "compareTo"},
    keywords=frozenset("""abstract assert boolean break byte case catch char class const continue default do double
else enum extends final finally float for goto if implements import instanceof int interface long native new package
private protected public return short static strictfp super switch synchronized this throw throws transient try void
volatile while true false null var record yield sealed permits""".split()),
)

CSHARP = Language(
    id="csharp", name="C#", aliases=("c#", "cs", "dotnet"), extensions=(".cs",), line_comment="//",
    typing="static", paradigm="oop", grammar="csharp",
    naming={"function": "PascalCase", "method": "PascalCase", "class": "PascalCase", "constant": "PascalCase",
            "field": "camelCase", "module": "PascalCase", "package": "PascalCase"},
    types={"int": "long", "float": "double", "bool": "bool", "str": "string", "bytes": "byte[]",
           "list": "List<{0}>", "dict": "Dictionary<{0}, {1}>", "set": "HashSet<{0}>", "tuple": "List<object>",
           "optional": "{0}?", "any": "object", "none": "void", "char": "char", "callable": "Func<...>"},
    error_idiom="exceptions: `throw new ArgumentException(...)`; project errors derive from Exception",
    errors={"value": ["ArgumentException", "FormatException"], "type": ["InvalidCastException", "ArgumentException"],
            "key": ["KeyNotFoundException"], "index": ["IndexOutOfRangeException", "ArgumentOutOfRangeException"],
            "attribute": ["NullReferenceException"], "not_implemented": ["NotImplementedException",
                                                                         "NotSupportedException"],
            "zero_division": ["DivideByZeroException"], "overflow": ["OverflowException"], "io": ["IOException"],
            "assertion": ["InvalidOperationException"], "stop_iteration": ["InvalidOperationException"],
            "recursion": ["StackOverflowException"], "memory": ["OutOfMemoryException"],
            "null": ["NullReferenceException", "ArgumentNullException"], "unicode": ["DecoderFallbackException"],
            "runtime": ["InvalidOperationException"], "generic": ["Exception"]},
    layout="class_files", assembly="compose",
    file_layout={"path": "src/{package_path}/{name}.cs", "container": "{Module}Module",
                 "package_container": "{Module}Package", "methods": "slot",
                 "class": "{declaration}\n{{\n{fields}\n{slot}\n}}", "nested_class": "plain"},
    target_notes=(
        "methods, properties and public constants are PascalCase; use properties for simple getters",
        "integer `/` and `%` truncate toward zero; `checked` arithmetic throws OverflowException",
        "optional parameters with default values exist (`int x = 0`); params arrays for varargs",
        "string is UTF-16 text; byte[] binary; Encoding.UTF8.GetBytes",
        "Dictionary enumeration order is not guaranteed after removals; use SortedDictionary or a list when "
        "order matters",
        "out / ref parameters return extra values; structs are value types",
    ),
    source_notes=(
        "properties (get/set), events, delegates and LINQ",
        "value types (struct) are copied; reference types are shared",
        "out / ref parameters, optional and named arguments, overloading",
        "nullable reference and value types (`T?`), the null-conditional `?.` and `??` operators",
        "integer overflow wraps unless in a `checked` context",
    ),
    toolchain={"image": "dotnet7", "build": "dotnet build", "test": "dotnet test"},
    harness="csharp",
    special_methods={"__init__": "<constructor>", "__str__": "ToString", "__repr__": "Repr", "__eq__": "Equals",
                     "__hash__": "GetHashCode", "__len__": "Count", "__iter__": "GetEnumerator"},
    keywords=frozenset("""abstract as base bool break byte case catch char checked class const continue decimal
default delegate do double else enum event explicit extern false finally fixed float for foreach goto if implicit in
int interface internal is lock long namespace new null object operator out override params private protected public
readonly ref return sbyte sealed short sizeof stackalloc static string struct switch this throw true try typeof uint
ulong unchecked unsafe ushort using virtual void volatile while""".split()),
)

GO = Language(
    id="go", name="Go", aliases=("golang",), extensions=(".go",), line_comment="//", typing="static",
    paradigm="structs", grammar="go",
    naming={"function": "PascalCase (exported) / camelCase", "method": "PascalCase", "class": "PascalCase",
            "constant": "PascalCase", "field": "PascalCase", "module": "snake_case", "package": "lowercase"},
    types={"int": "int64", "float": "float64", "bool": "bool", "str": "string", "bytes": "[]byte",
           "list": "[]{0}", "dict": "map[{0}]{1}", "set": "map[{0}]bool", "tuple": "[]any",
           "optional": "*{0}", "any": "any", "none": "", "char": "rune", "callable": "func(...)"},
    error_idiom="errors are values: a function that can fail returns (T, error) with `errors.New` / "
                "`fmt.Errorf`; panic only for programming bugs",
    errors={c: ["error"] for c in ("value", "type", "key", "index", "attribute", "not_implemented",
                                   "zero_division", "overflow", "io", "assertion", "stop_iteration", "recursion",
                                   "memory", "null", "unicode", "runtime", "generic")},
    layout="package_files", assembly="compose",
    file_layout={"path": "{package_path}/{module}.go", "container": None, "methods": "top",
                 "class": "{declaration} {{\n{fields}\n}}", "constructor": "New{Name}",
                 "project": {"go.mod": "module {go_module}\n\ngo 1.21\n"}},
    target_notes=(
        "a function that can fail returns (value, error); the caller checks `if err != nil`; never panic for "
        "expected failures",
        "classes become structs; methods have receivers `func (s *Name) Method()`; constructors are "
        "`func NewName(...) *Name`; inheritance becomes embedding plus interfaces",
        "exported names start with an upper-case letter",
        "no default or keyword arguments, no overloading: use distinct function names or variadic parameters",
        "integer `/` and `%` truncate toward zero; int64 overflow wraps silently",
        "map iteration order is RANDOM: sort the keys when the order is observable",
        "string is UTF-8 bytes; iterate runes with `for _, r := range s`",
        "zero values: 0, \"\", nil, false; nil maps panic on write",
    ),
    source_notes=(
        "multiple return values and (T, error) results",
        "structs, methods with value or pointer receivers, embedding, implicit interfaces",
        "map iteration order is random; goroutines and channels",
        "integer overflow wraps; integer division truncates toward zero",
    ),
    toolchain={"image": "go121", "build": "go build ./... && go vet ./...", "test": "go test ./..."},
    harness="go",
    special_methods={"__init__": "New{Name}", "__str__": "String", "__eq__": "Equal", "__len__": "Len"},
    keywords=frozenset("""break case chan const continue default defer else fallthrough for func go goto if import
interface map package range return select struct switch type var""".split()),
)

RUST = Language(
    id="rust", name="Rust", aliases=("rs",), extensions=(".rs",), line_comment="//", typing="static",
    paradigm="structs", grammar="rust",
    naming={"function": "snake_case", "method": "snake_case", "class": "PascalCase", "constant": "UPPER_SNAKE",
            "field": "snake_case", "module": "snake_case", "package": "snake_case"},
    types={"int": "i64", "float": "f64", "bool": "bool", "str": "String", "bytes": "Vec<u8>", "list": "Vec<{0}>",
           "dict": "HashMap<{0}, {1}>", "set": "HashSet<{0}>", "tuple": "({0}, ...)",
           "optional": "Option<{0}>", "any": "f64 for a number that may be int or float, else an enum you define (no crates)", "none": "()",
           "char": "char", "callable": "Box<dyn Fn(...)>"},
    error_idiom="no exceptions: a function that can fail returns Result<T, E> (a project error enum or "
                "Box<dyn std::error::Error>); panic! only for bugs",
    errors={c: ["Err", "panic"] for c in ("value", "type", "key", "index", "attribute", "not_implemented",
                                          "zero_division", "overflow", "io", "assertion", "stop_iteration",
                                          "recursion", "memory", "null", "unicode", "runtime", "generic")},
    layout="package_files", assembly="compose",
    file_layout={"path": "src/{module_path}.rs", "container": None, "methods": "slot",
                 "class": "{declaration} {{\n{fields}\n}}\n\nimpl {name} {{\n{slot}\n}}", "constructor": "new",
                 "project": {"Cargo.toml": "[package]\nname = \"{crate}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n"
                                           "\n[dependencies]\n"}},
    target_notes=(
        "ownership and borrowing: take &T / &mut T parameters, return owned values; clone only when needed; "
        "no mutable aliasing (use Rc<RefCell<T>> only for genuinely shared mutable state)",
        "failures return Result<T, E>; use `?` to propagate; no exceptions and no null (Option<T>)",
        "classes become structs plus `impl` blocks; constructors are `fn new(...) -> Self`; inheritance becomes "
        "traits (or composition)",
        "no default or keyword arguments, no overloading: use distinct names, Option parameters or builders",
        "integer overflow panics in debug builds: use i64/i128, checked_*/wrapping_* deliberately; `/` and `%` "
        "truncate toward zero (use div_euclid / rem_euclid for floor semantics)",
        "HashMap iteration order is random: use BTreeMap or a Vec of pairs when the order is observable",
        "String is UTF-8; index by chars().nth or bytes, never s[i]; Vec<u8> for binary",
        "the standard library only (no crates are available offline); no `unsafe` unless unavoidable",
    ),
    source_notes=(
        "ownership, borrowing and lifetimes; Result / Option instead of exceptions and null",
        "traits and generics; pattern matching; integer overflow panics in debug builds",
    ),
    toolchain={"image": "rust", "build": "cargo build --offline -q", "test": "cargo test --offline -q"},
    harness="rust",
    special_methods={"__init__": "new", "__str__": "fmt (impl Display)", "__eq__": "eq (derive PartialEq)",
                     "__len__": "len", "__iter__": "iter"},
    keywords=frozenset("""as break const continue crate else enum extern false fn for if impl in let loop match mod
move mut pub ref return self Self static struct super trait true type unsafe use where while async await dyn
abstract become box do final macro override priv typeof unsized virtual yield try""".split()),
)

CPP = Language(
    id="cpp", name="C++", aliases=("c++", "cxx", "cplusplus"), extensions=(".cpp", ".hpp", ".cc", ".h", ".cxx"),
    line_comment="//", typing="static", paradigm="oop", grammar="cpp",
    naming={"function": "snake_case", "method": "snake_case", "class": "PascalCase", "constant": "kPascalCase",
            "field": "snake_case_", "module": "snake_case", "package": "snake_case"},
    types={"int": "long long", "float": "double", "bool": "bool", "str": "std::string",
           "bytes": "std::vector<uint8_t>", "list": "std::vector<{0}>", "dict": "std::map<{0}, {1}>",
           "set": "std::set<{0}>", "tuple": "std::tuple<...>", "optional": "std::optional<{0}>",
           "any": "double for a number that may be int or float, else a std::variant of the possible types", "none": "void", "char": "char",
           "callable": "std::function<...>"},
    error_idiom="exceptions: `throw std::invalid_argument(...)`; project errors derive from std::runtime_error",
    errors={"value": ["std::invalid_argument", "invalid_argument", "std::domain_error"],
            "type": ["std::invalid_argument", "std::bad_cast", "bad_cast"], "key": ["std::out_of_range", "out_of_range"],
            "index": ["std::out_of_range", "out_of_range"], "attribute": ["std::runtime_error"],
            "not_implemented": ["std::logic_error", "logic_error"], "zero_division": ["std::domain_error", "domain_error"],
            "overflow": ["std::overflow_error", "overflow_error"], "io": ["std::ios_base::failure", "failure"],
            "assertion": ["std::logic_error"], "stop_iteration": ["std::out_of_range"],
            "recursion": ["std::runtime_error"], "memory": ["std::bad_alloc", "bad_alloc"],
            "null": ["std::invalid_argument"], "unicode": ["std::range_error", "range_error"],
            "runtime": ["std::runtime_error", "runtime_error"], "generic": ["std::exception", "exception"]},
    layout="package_files", assembly="compose",
    file_layout={"path": "include/{module_path}.hpp", "container": "namespace", "methods": "slot",
                 "class": "{declaration} {{\npublic:\n{fields}\n{slot}\n}};", "header": "#pragma once",
                 "project": {"CMakeLists.txt": "cmake_minimum_required(VERSION 3.16)\nproject({crate} CXX)\n"
                                               "set(CMAKE_CXX_STANDARD 17)\n"}},
    target_notes=(
        "header-only: functions defined in headers are `inline`; include what you use (<string>, <vector>, "
        "<map>, <stdexcept>, <cstdint>, <optional>)",
        "RAII and value semantics: std::string, std::vector, std::map, smart pointers; no raw new/delete",
        "signed integer overflow is undefined behaviour: use long long / int64_t, or a checked computation",
        "integer `/` and `%` truncate toward zero (Python floors)",
        "std::map is ordered by key, std::unordered_map has no order: choose to match the observable order",
        "std::string holds bytes; there is no built-in Unicode text type",
        "default arguments and overloading exist; constructors are not virtual",
    ),
    source_notes=(
        "templates, overloading, operator overloading, default arguments",
        "pointers, references, value vs reference semantics, copy / move constructors, RAII destructors",
        "signed overflow is undefined; integer division truncates; implicit numeric conversions",
        "std::map is ordered; std::unordered_map is not",
    ),
    toolchain={"image": "gcc", "build": "g++ -std=c++17 -fsyntax-only", "test": "cmake .. && make && ./test_runner"},
    harness="cpp",
    special_methods={"__init__": "<constructor>", "__str__": "to_string", "__eq__": "operator==",
                     "__lt__": "operator<", "__len__": "size", "__getitem__": "operator[]"},
)

C = Language(
    id="c", name="C", aliases=("c99", "gnu99"), extensions=(".c", ".h"), line_comment="//", typing="static",
    paradigm="procedural", grammar="c",
    naming={"function": "snake_case", "method": "snake_case", "class": "snake_case", "constant": "UPPER_SNAKE",
            "field": "snake_case", "module": "snake_case", "package": "snake_case"},
    types={"int": "int64_t", "float": "double", "bool": "bool", "str": "char * (NUL-terminated)",
           "bytes": "uint8_t * + size_t length", "list": "{0} * + size_t length", "dict": "a struct / hash table",
           "set": "an array or hash table", "tuple": "struct", "optional": "pointer (NULL = none)",
           "any": "void *", "none": "void", "char": "char", "callable": "function pointer"},
    error_idiom="return codes (0 / -1, NULL) and errno; no exceptions",
    errors={c: ["return code", "errno", "abort"] for c in ("value", "type", "key", "index", "attribute",
                                                           "not_implemented", "zero_division", "overflow", "io",
                                                           "assertion", "stop_iteration", "recursion", "memory",
                                                           "null", "unicode", "runtime", "generic")},
    layout="package_files", assembly="compose",
    file_layout={"path": "{module_path}.c", "container": None, "methods": "top",
                 "class": "typedef struct {name} {{\n{fields}\n}} {name};"},
    target_notes=("manual memory management; every malloc has one free", "check every return code"),
    source_notes=(
        "manual memory: malloc/free, pointer arithmetic, arrays decay to pointers (their length travels "
        "separately)",
        "strings are NUL-terminated char arrays; char may be signed",
        "fixed-width integers wrap (unsigned) or overflow undefined (signed); integer division truncates",
        "errors are return codes / NULL / errno; output parameters through pointers",
        "macros and conditional compilation; global and static state",
    ),
    toolchain={"image": "gcc", "build": "gcc -std=gnu99 -fsyntax-only", "test": "make test"},
    harness="c",
)

JAVASCRIPT = Language(
    id="javascript", name="JavaScript", aliases=("js", "node", "nodejs", "ecmascript"), extensions=(".js", ".mjs",
                                                                                                  ".cjs"),
    line_comment="//", typing="dynamic", paradigm="oop", grammar="javascript",
    naming={"function": "camelCase", "method": "camelCase", "class": "PascalCase", "constant": "UPPER_SNAKE",
            "field": "camelCase", "module": "camelCase", "package": "kebab-case"},
    types={"int": "number", "float": "number", "bool": "boolean", "str": "string", "bytes": "Uint8Array",
           "list": "Array", "dict": "Object / Map", "set": "Set", "tuple": "Array", "optional": "{0} | null",
           "any": "any", "none": "undefined", "char": "string", "callable": "function"},
    error_idiom="exceptions: `throw new TypeError(...)`; project errors extend Error",
    errors={"value": ["Error", "RangeError"], "type": ["TypeError"], "key": ["Error"], "index": ["RangeError"],
            "attribute": ["TypeError"], "not_implemented": ["Error"], "zero_division": ["RangeError"],
            "overflow": ["RangeError"], "io": ["Error"], "assertion": ["AssertionError"], "recursion": ["RangeError"],
            "null": ["TypeError"], "runtime": ["Error"], "generic": ["Error"]},
    layout="module_files", assembly="compose",
    file_layout={"path": "{module_path}.js", "container": None, "methods": "slot",
                 "class": "{declaration} {{\n{fields}\n{slot}\n}}"},
    target_notes=("use ES modules or CommonJS consistently", "=== for comparisons"),
    source_notes=(
        "every number is a 64-bit float: integers are exact only up to 2**53; `/` is float division; "
        "BigInt is separate",
        "undefined vs null; loose == coerces types; truthiness (0, '', NaN, null, undefined are false)",
        "objects are string-keyed dictionaries whose integer-like keys iterate first in numeric order",
        "Array.prototype.sort sorts as strings by default",
        "prototype-based classes, closures, `this` binding, callbacks, promises and async/await",
    ),
    toolchain={"image": "node18", "test": "npx jest"},
    harness="node",
    special_methods={"constructor": "<constructor>", "toString": "__str__"},
)

MATLAB = Language(
    id="matlab", name="Matlab", aliases=("octave", "m"), extensions=(".m",), line_comment="%", typing="dynamic",
    paradigm="functions", grammar="matlab",
    naming={"function": "camelCase", "method": "camelCase", "class": "PascalCase", "constant": "UPPER_SNAKE",
            "field": "camelCase", "module": "camelCase", "package": "+package"},
    types={"int": "double (integer-valued)", "float": "double", "bool": "logical", "str": "char row vector / string",
           "bytes": "uint8 array", "list": "row vector or cell array", "dict": "struct / containers.Map",
           "set": "unique() array", "tuple": "cell array", "optional": "[] (empty)", "any": "any",
           "none": "no output", "char": "char", "callable": "function handle"},
    error_idiom="`error('pkg:id', 'message')`; caught with try/catch as MException",
    # error('pkg:id', ...) carries no category of its own: any MException may stand for any source exception
    errors={c: ["MException", "error"] for c in ("value", "type", "key", "index", "attribute", "not_implemented",
                                                 "zero_division", "overflow", "io", "assertion", "stop_iteration",
                                                 "recursion", "memory", "null", "unicode", "runtime", "generic")},
    layout="module_files", assembly="compose",
    file_layout={"path": "{module_path}.m", "container": None, "methods": "top"},
    source_notes=(
        "1-based indexing; ranges `a:b` include both ends; `end` is the last index",
        "everything is a matrix (column-major); scalars are 1x1; `*` is matrix product, `.*` element-wise",
        "default numeric type is double; integer types saturate instead of wrapping",
        "functions can return several outputs ([a, b] = f(x)); nargin/nargout change behaviour",
        "char arrays vs strings; cell arrays and structs",
    ),
    toolchain={"image": "octave", "test": "octave --no-gui --eval \"runtests('tests')\""},
    harness="octave",
)

LANGUAGES = (PYTHON3, PYTHON2, JAVA, CSHARP, GO, RUST, CPP, C, JAVASCRIPT, MATLAB)
