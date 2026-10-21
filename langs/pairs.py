"""Feature-mapping rules for a translation direction (cf. Oxidizer's feature mapping, RustMap's checklist, Syzygy's
C->Rust rules; docs/prompting-survey.md). Rules come from two places:
  derived    from the two profiles' properties (typing, error idiom, paradigm, integer semantics)
  specific   written for a (source, target) pair of the benchmark
Each rule is one line of prompt text; the Translator and the Repair agent receive all of them, the Analyzer the
derived ones (to flag hazards)."""

DERIVED = [
    # (condition on (source, target), rule)
    (lambda s, t: s.typing == "dynamic" and t.typing == "static",
     "Dynamic -> static typing: give every parameter, field and result the concrete type the tests and the "
     "contract show; when a value really has several types, use the target's `any` type and branch on the "
     "runtime type. Never guess a narrower type than the tests use."),
    (lambda s, t: s.typing == "dynamic" and t.id in ("rust", "go", "cpp", "c"),
     "A parameter that receives both integers and floats is the target's floating-point type (f64 / float64 / "
     "double), converted back to an integer type where the source returns an int. Parameters are concrete types: "
     "never a generic, a trait / interface bound or a JSON value type, which the tests cannot pass."),
    (lambda s, t: s.typing == "static" and t.typing == "dynamic",
     "Static -> dynamic typing: keep the behaviour the types implied (integer vs floating-point arithmetic, "
     "fixed-width overflow, char vs string) explicitly in code."),
    (lambda s, t: s.id.startswith("python") and t.id not in ("python3", "python2"),
     "Python ints are unbounded and `//` / `%` floor toward negative infinity: reproduce both (wider types or "
     "big integers; floor division helpers) wherever values can be large or negative."),
    (lambda s, t: t.id.startswith("python") and s.id in ("java", "csharp", "go", "rust", "cpp", "c"),
     "Fixed-width integers: where the source wraps (or truncates in division), emulate it in Python "
     "(e.g. `(x + y) & 0xFFFFFFFF` then sign-adjust; `int(a / b)` truncation via math.trunc or `-(-a // b)`)."),
    (lambda s, t: t.id == "go" and s.paradigm == "oop",
     "Exceptions -> Go: a function whose source can raise returns (T, error) (or just error); every caller "
     "checks it and returns it upward. Classes -> structs with pointer-receiver methods and NewX constructors; "
     "inheritance -> embedding plus an interface for the overridden methods."),
    (lambda s, t: t.id == "rust",
     "Errors -> Rust: a function whose source can fail returns Result<T, E>; define one error enum per module "
     "(or use Box<dyn std::error::Error>) and propagate with `?`. Null / None -> Option<T>."),
    (lambda s, t: t.id in ("java", "csharp") and s.id in ("python3", "python2", "javascript", "matlab"),
     "Default / keyword arguments -> overloads (Java) or optional parameters (C#); *args -> varargs; "
     "module-level functions -> static methods of the module's container class."),
    (lambda s, t: s.id in ("java", "csharp", "cpp") and t.id in ("python3", "go", "rust"),
     "Overloaded methods (same name, different parameter types): the target has no overloading (or none of "
     "this kind): use one function that dispatches on the arguments, or distinct names as the skeleton gives."),
    (lambda s, t: s.id in ("c", "cpp") and t.id in ("python3", "rust", "go", "java", "csharp"),
     "Pointers and out-parameters: an argument the source writes through becomes a return value (or a mutable "
     "reference); an array with a separate length becomes one sequence; NULL -> None / Option / nil."),
]

SPECIFIC = {
    ("python3", "java"): [],
    ("python2", "python3"): [
        "Keep the code Python 2-equivalent in behaviour: `/` on ints -> `//`; text vs bytes made explicit; "
        "dict.keys()/items()/values(), map, filter, zip, range -> list(...) where the result is indexed, "
        "stored or compared; `print` statement -> print(); `except E, e` -> `except E as e`.",
    ],
    ("python3", "cpp"): [
        "Python lists of mixed types -> std::vector<std::variant<...>> or a small struct; dict -> std::map "
        "(ordered) unless insertion order is observable, then a vector of pairs.",
        "Python exceptions -> C++ exceptions of the mapped std:: type; project exception classes derive from "
        "std::runtime_error and keep their names.",
    ],
    ("python3", "rust"): [
        "Python lists of mixed types -> a Vec of an enum; dict with insertion order observable -> Vec<(K, V)> "
        "or BTreeMap when sorted order is acceptable.",
        "Methods that mutate self take &mut self; methods that only read take &self.",
    ],
    ("python3", "go"): [
        "Python's dict iteration order is insertion order, Go's map order is random: keep a slice of keys when "
        "order is observable.",
    ],
    ("java", "python3"): [
        "Java int/long arithmetic wraps on overflow and `/` truncates toward zero: emulate where reachable.",
        "Static members -> module-level functions or @staticmethod; interfaces -> abc.ABC or duck typing; "
        "getters/setters may stay methods (keep names in snake_case as the skeleton gives).",
    ],
    ("java", "csharp"): [
        "Nearly one-to-one: getX()/setX() pairs may become properties only where the skeleton names them so; "
        "checked exceptions disappear; Java generics' wildcards -> C# variance or concrete types.",
        "java.util collections -> System.Collections.Generic (ArrayList -> List, HashMap -> Dictionary); "
        "String.format -> string.Format / interpolation; `final` -> readonly / sealed.",
    ],
    ("java", "go"): [
        "Java exceptions -> returned error values; class hierarchies -> structs + interfaces; static fields -> "
        "package-level variables; synchronized -> sync.Mutex.",
    ],
    ("csharp", "java"): [
        "Properties -> getX()/setX() methods; events/delegates -> functional interfaces; LINQ -> streams; "
        "out/ref parameters -> return a small result object; structs -> final classes with value equality.",
        "Unsigned and decimal types have no Java primitive: use long / BigInteger / BigDecimal.",
    ],
    ("c", "rust"): [
        "Safe Rust: arrays and buffers -> Vec<T> or slices (&[T], &mut [T]); keep the C length variables only "
        "where the logic needs them; function pointers -> closures or fn types; void * -> generics or enums; "
        "no `unsafe` unless the C code is inherently unsafe.",
        "C integer types map by width (int -> i32, long -> i64, size_t -> usize, unsigned char -> u8); "
        "unsigned overflow wraps in C: use wrapping_* there.",
    ],
    ("c", "python3"): [
        "C strings (char *) -> str (or bytes when the code treats them as bytes); structs -> classes or "
        "dataclasses; error return codes stay return values unless the C caller treats them as exceptional.",
    ],
    ("cpp", "python3"): [
        "Templates -> generic Python functions; operator overloads -> dunder methods; std::map -> dict sorted by "
        "key where iteration order is observable; RAII destructors -> context managers only if behaviour depends "
        "on them.",
    ],
    ("javascript", "python3"): [
        "JavaScript numbers are doubles: integer results that a Python int would make exact (2**53+) must keep "
        "float semantics; `undefined`/`null` -> None; missing object keys read as undefined -> dict.get.",
        "Callbacks and promises -> plain functions (or asyncio when the code is truly concurrent); prototype "
        "classes -> Python classes.",
    ],
    ("matlab", "python3"): [
        "Use numpy: matrices -> numpy arrays (column-major semantics kept where reshape/indexing order is "
        "observable); 1-based indices -> 0-based; a:b inclusive -> range(a-1, b) / slices; `end` -> -1 / len.",
        "Element-wise ops (.*, ./, .^) -> numpy element-wise; `*` on matrices -> @; multiple outputs -> a "
        "tuple; nargin defaults -> default arguments.",
    ],
}


def pair_rules(s, t):
    rules = [r for cond, r in DERIVED if cond(s, t)]
    key = (s.id, t.id)
    rules += SPECIFIC.get(key, [])
    return tuple(rules)
