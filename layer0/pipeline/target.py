# Runs INSIDE acm-sandbox/analysis (Python 3.12). For Python -> Java: the API the translated JUnit tests expect.
# RepoTransBench targets ship only tests (no src/main), so the classes, constructors and methods the tests call
# are the contract the translation has to satisfy. Fragments are linked to that API by normalised name; the
# links are hints for the Translator agent, not resolved symbols.
import re
from collections import defaultdict
from pathlib import Path

import tree_sitter_java
from tree_sitter import Language, Parser

JAVA_STD = set("""String Object Integer Long Double Float Boolean Character Byte Short Math System List Map Set
HashMap ArrayList HashSet LinkedHashMap LinkedHashSet LinkedList TreeMap Arrays Collections Optional Objects
Exception RuntimeException IllegalArgumentException IllegalStateException UnsupportedOperationException
NullPointerException IndexOutOfBoundsException ArithmeticException ClassCastException IOException Throwable Error
Iterator Iterable StringBuilder Thread Class Void Number BigInteger BigDecimal Base64 StandardCharsets Instant
Duration Pattern Matcher UUID Stream Collectors Function Supplier Consumer Arrays Assertions Assert
Assumptions Mockito ArgumentMatchers TimeUnit File Files Path Paths Charset
Reader Writer StringReader StringWriter InputStream OutputStream ByteArrayInputStream ByteArrayOutputStream
PrintStream PrintWriter BufferedReader BufferedWriter InputStreamReader OutputStreamWriter Scanner
UncheckedIOException Random Date Calendar TimeZone LocalDate LocalTime LocalDateTime ZonedDateTime OffsetDateTime
ZoneId ZoneOffset ChronoUnit DateTimeFormatter Properties Collection Deque ArrayDeque Queue PriorityQueue Stack
Vector Hashtable TreeSet SortedMap NavigableMap EnumMap Entry AbstractMap AtomicInteger AtomicLong
AtomicBoolean Callable Runnable Executors ExecutorService Future CompletableFuture Predicate BiFunction
BiConsumer Comparator CharSequence Locale Iterable Override Deprecated SuppressWarnings FunctionalInterface
Field Method Constructor Modifier StandardOpenOption MessageDigest Mac SecretKeySpec URI URL URLEncoder
URLDecoder IntStream LongStream DoubleStream NumberFormatException NoSuchElementException
ConcurrentModificationException InterruptedException CloneNotSupportedException""".split())
NON_PROJECT = ("java.", "javax.", "org.junit", "org.mockito", "org.hamcrest", "org.assertj")
TEST_DIRS = {"original", "publictests", "public_tests", "public", "tests", "test"}
TEST_CLASS = re.compile(r"^Test|Tests?$|TestCase$|Runner$|IT$")


def _norm(name):
    return name.lower().replace("_", "")


def _type_name(text):
    return re.sub(r"<.*>|\[\]", "", text).split(".")[-1].strip()


def java_target(root):
    root = Path(root)
    parser = Parser(Language(tree_sitter_java.language()))
    packages, imports, defined, tests, files = set(), {}, set(), 0, []
    stubs = {}  # non-test classes the test files declare themselves (stand-ins for the code under test)
    api = defaultdict(lambda: {"constructed": 0, "methods": set(), "static_methods": set(), "files": set()})
    for p in sorted(root.rglob("*.java")):
        rel = p.relative_to(root).as_posix()
        files.append(rel)
        var_types = {}

        def walk(n, cls=None):
            nonlocal tests
            t = n.type
            if t == "package_declaration":
                packages.add(n.text.decode().replace("package", "", 1).strip(" ;\n"))
            elif t == "import_declaration":
                full = n.text.decode().replace("import", "", 1).replace("static ", "").strip(" ;\n")
                imports[full.split(".")[-1]] = full
            elif t in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
                cls = n.child_by_field_name("name").text.decode()
                defined.add(cls)
                if not TEST_CLASS.search(cls):
                    stubs.setdefault(cls, {"methods": set(), "file": rel})
            elif t == "method_declaration":
                mods = next((c for c in n.children if c.type == "modifiers"), None)
                tests += bool(mods is not None and b"@Test" in mods.text)
                if cls in stubs:
                    stubs[cls]["methods"].add(n.child_by_field_name("name").text.decode())
            elif t in ("local_variable_declaration", "field_declaration", "formal_parameter"):
                ty = n.child_by_field_name("type")
                if ty is not None:
                    names = [n.child_by_field_name("name")] if t == "formal_parameter" else \
                        [d.child_by_field_name("name") for d in n.children if d.type == "variable_declarator"]
                    for nm in names:
                        if nm is not None:
                            var_types[nm.text.decode()] = _type_name(ty.text.decode())
            elif t == "object_creation_expression":
                ty = n.child_by_field_name("type")
                if ty is not None:
                    cls = _type_name(ty.text.decode())
                    api[cls]["constructed"] += 1
                    api[cls]["files"].add(rel)
            elif t == "method_invocation":
                obj, name = n.child_by_field_name("object"), n.child_by_field_name("name").text.decode()
                if obj is not None and obj.type == "identifier":
                    o = obj.text.decode()
                    if o in var_types:
                        api[var_types[o]]["methods"].add(name)
                        api[var_types[o]]["files"].add(rel)
                    elif o[:1].isupper():
                        api[o]["static_methods"].add(name)
                        api[o]["files"].add(rel)
            for c in n.children:
                walk(c, cls)
        walk(parser.parse(p.read_bytes()).root_node)

    package = _common_package(packages)
    expected, third_party = {}, sorted({v for v in imports.values() if not v.startswith(NON_PROJECT)
                                        and not (package and v.startswith(package))})
    for cls, u in sorted(api.items()):
        full = imports.get(cls)
        if cls in defined or cls in JAVA_STD or (full and not (package and full.startswith(package))):
            continue  # a test class, a JDK/JUnit class, or a third-party library class
        expected[cls] = {"qualified": full or (f"{package}.{cls}" if package else cls),
                         "constructed": u["constructed"], "methods": sorted(u["methods"]),
                         "static_methods": sorted(u["static_methods"]), "used_in": sorted(u["files"])}
    return {"language": "java", "package": package, "test_files": files, "test_methods": tests,
            "expected_api": expected, "third_party_imports": third_party,
            "stubbed_in_tests": {c: {"methods": sorted(s["methods"]), "file": s["file"]}
                                 for c, s in sorted(stubs.items())}}


def _common_package(packages):
    if not packages:
        return None
    split = [p.split(".") for p in packages]
    common = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    while common and common[-1] in TEST_DIRS:
        common.pop()
    return ".".join(common) or None


def link(fragments, target):
    """Name-based hints from each fragment to the Java API the tests expect (ClassName#member)."""
    api, stubs = target.get("expected_api", {}), target.get("stubbed_in_tests", {})
    # match = "name": the tests use this API; "stub_in_tests": the tests declare a stand-in with this name
    classes = {_norm(c): (c, "name") for c in api}
    for c in stubs:
        classes.setdefault(_norm(c), (c, "stub_in_tests"))
    members = defaultdict(list)
    for c, u in api.items():
        for m in [*u["methods"], *u["static_methods"]]:
            members[_norm(m)].append((f"{c}#{m}", "name"))
    for c, s in stubs.items():
        for m in s["methods"]:
            members[_norm(m)].append((f"{c}#{m}", "stub_in_tests"))
    hints = {}
    for f in fragments:
        leaf = f["qualname"].rsplit(".", 1)[-1]
        found = []
        if f["kind"] in ("class", "module_header", "module_init") and _norm(leaf) in classes:
            found.append(classes[_norm(leaf)])
        elif f["kind"] == "method" and leaf == "__init__":
            owner = classes.get(_norm(f["qualname"].rsplit(".", 2)[-2]))
            if owner and (owner[1] == "stub_in_tests" or api[owner[0]]["constructed"]):
                found.append((f"{owner[0]}#<init>", owner[1]))
        elif f["kind"] in ("function", "method"):
            cands = members.get(_norm(leaf.lstrip("_")), [])
            owner = _norm(f["qualname"].rsplit(".", 2)[-2])  # class (methods) or module (functions)
            preferred = [c for c in cands if _norm(c[0].split("#")[0]) == owner]
            # a method belongs to its class; a module function may land in any (utility) class
            found.extend(preferred if f["kind"] == "method" else preferred or cands)
        if found:
            hints[f["id"]] = [{"java": h, "match": m} for h, m in found]
    return hints
