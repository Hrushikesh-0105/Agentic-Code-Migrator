"""The shape of a language profile (see catalog.py for the data)."""
from dataclasses import dataclass, field

# The neutral type vocabulary: what Layer 0 / the Analyzer / the Test-Generation agent speak. A language's `types`
# maps each to its spelling; {0}, {1} are element types.
NEUTRAL_TYPES = ("int", "float", "bool", "str", "bytes", "list", "dict", "set", "tuple", "optional", "any", "none",
                 "char", "callable")

# The neutral error categories. A language's `errors` maps each to its own exception / error names; the comparator
# matches a legacy exception and a target exception when they fall in one category.
ERROR_CATEGORIES = ("value", "type", "key", "index", "attribute", "not_implemented", "zero_division", "overflow",
                    "io", "assertion", "stop_iteration", "recursion", "memory", "null", "unicode", "runtime",
                    "generic")

# How fragments are laid out as target files (the skeleton's strategy)
LAYOUTS = {
    "module_files":  "one file per source module, fragments in their original places (Python; splice assembly)",
    "class_files":   "one file per class; module-level functions and constants in a static container class "
                     "(Java, C#)",
    "package_files": "one file per source module inside a package; classes become structs (or classes) whose "
                     "methods sit next to them at file level (Go, Rust, C++, C)",
}


@dataclass(frozen=True)
class Language:
    id: str                       # stable id: python3, python2, java, csharp, go, rust, cpp, c, javascript, matlab
    name: str                     # display / RepoTransBench name: Python, Java, C#, Go, Rust, C++, C, JavaScript, Matlab
    aliases: tuple = ()
    extensions: tuple = ()        # source file extensions, the first is the default
    line_comment: str = "//"
    typing: str = "static"        # static | dynamic
    paradigm: str = "oop"         # oop | structs (Go, Rust) | procedural (C) | functions (Matlab)
    grammar: str = ""             # tree-sitter grammar name (tree_sitter_language_pack)
    # naming conventions: function, method, class, constant, field, module (file), package
    naming: dict = field(default_factory=dict)
    types: dict = field(default_factory=dict)          # neutral type -> spelling
    error_idiom: str = ""         # one line: how this language reports errors
    errors: dict = field(default_factory=dict)         # category -> [names] (first = what a translation should use)
    layout: str = "class_files"
    assembly: str = "compose"     # compose (pieces in slots) | splice (Python: original places)
    file_layout: dict = field(default_factory=dict)    # how module / class / container paths and names are formed
    target_notes: tuple = ()      # idioms to follow when WRITING this language
    source_notes: tuple = ()      # semantics to watch when READING this language
    toolchain: dict = field(default_factory=dict)      # image, build (a tree), test command, project files
    harness: str = ""             # differential harness adapter (sandbox/harness/): python, java, csharp, node,
                                  # octave (reflective) | c, cpp, go, rust (generated driver)
    special_methods: dict = field(default_factory=dict)  # source dunder / special name -> this language's member
    keywords: frozenset = frozenset()

    @property
    def ext(self):
        return self.extensions[0]

    def comment(self, text):
        return f"{self.line_comment} {text}" if self.line_comment else text

    def slot(self, name):
        return self.comment(f"@acm:slot {name}")

    def type_of(self, neutral, *args):
        t = self.types.get(neutral) or self.types.get("any", neutral)
        return t.format(*args) if args else t

    def error_names(self, category):
        return list(self.errors.get(category) or self.errors.get("generic") or [])

    def categories_of(self, name):
        """The error categories an exception / error name of this language belongs to (a name can serve several,
        e.g. Java's ArithmeticException: zero_division and overflow). Namespaces are ignored (std::out_of_range
        ~ out_of_range)."""
        short = str(name).split("::")[-1].split(".")[-1]
        return {cat for cat, names in self.errors.items()
                if name in names or short in {n.split("::")[-1].split(".")[-1] for n in names}}

    def type_table(self):
        """The neutral -> this language type map as prompt text."""
        rows = [f"{n} -> {self.types[n].format('T', 'V') if '{' in self.types[n] else self.types[n]}"
                for n in NEUTRAL_TYPES if n in self.types]
        return "; ".join(rows)

    def error_table(self):
        return "; ".join(f"{c}: {', '.join(v[:2])}" for c, v in self.errors.items() if v)


@dataclass(frozen=True)
class Pair:
    source: Language
    target: Language
    rules: tuple

    @property
    def regime(self):
        return f"{self.source.id}->{self.target.id}"

    def same_family(self):
        return self.source.id.startswith("python") and self.target.id.startswith("python")
