"""Known-answer check of every language harness and of the cross-language comparator:
python -m sandbox.polyglot_verify

The same small API is implemented faithfully in each language of langs/ (Python is the reference):
    double(x) -> 2x            divide(a, b) -> a // b, fails on b == 0        greet(name) -> "hello <name>", prints
    count(words) -> {word: n}  push_twice(list, x) mutates its argument       Counter(name, start).bump(by)
Each language implements what it can express (C has no exceptions or dicts; Matlab no mutable arguments). The same
neutral cases run in every language's sandbox; each run is compared with the Python run through
sandbox/compare.py (profile cross:python3-><language>). Known answers: every faithful implementation is
equivalent; two planted bugs are found (a Go double that triples, a C++ divide that throws an exception of the
wrong category); a missing function is "unresolved".
"""
import base64
import json
import shutil
import sys
import time
from pathlib import Path

from .compare import compare_results, profile_for
from .logs import new_run, write_json, write_text
from .polyglot import run_cases

FILES = {
    "python3": {"demo/__init__.py": "", "demo/util.py": '''
def double(x):
    return x * 2


def divide(a, b):
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a // b


def greet(name):
    print("hi " + name)
    return "hello " + name


def count(words):
    m = {}
    for w in words:
        m[w] = m.get(w, 0) + 1
    return m


def push_twice(v, x):
    v.append(x)
    v.append(x)


class Counter:
    def __init__(self, name, start):
        self.name = name
        self.count = start

    def bump(self, by):
        self.count += by
        return self.count
'''},
    "java": {"src/main/java/demo/UtilModule.java": '''package demo;
import java.util.*;
public final class UtilModule {
    public static long dbl(long x) { return x * 2; }
    public static long divide(long a, long b) { if (b == 0) throw new ArithmeticException("division by zero"); return Math.floorDiv(a, b); }
    public static String greet(String name) { System.out.println("hi " + name); return "hello " + name; }
    public static Map<String, Long> count(List<String> words) { Map<String, Long> m = new LinkedHashMap<>(); for (String w : words) m.merge(w, 1L, Long::sum); return m; }
    public static void pushTwice(List<Long> v, long x) { v.add(x); v.add(x); }
}''', "src/main/java/demo/Counter.java": '''package demo;
public class Counter {
    private String name; private long count;
    public Counter(String name, long start) { this.name = name; this.count = start; }
    public long bump(long by) { count += by; return count; }
}'''},
    "csharp": {"src/Util.cs": '''namespace Demo {
public static class UtilModule {
    public static long Double(long x) => x * 2;
    public static long Divide(long a, long b) { if (b == 0) throw new DivideByZeroException("division by zero"); long q = a / b; return (a % b != 0 && ((a < 0) ^ (b < 0))) ? q - 1 : q; }
    public static string Greet(string name) { Console.WriteLine("hi " + name); return "hello " + name; }
    public static Dictionary<string, long> Count(List<string> words) { var m = new Dictionary<string, long>(); foreach (var w in words) m[w] = m.GetValueOrDefault(w) + 1; return m; }
    public static void PushTwice(List<long> v, long x) { v.Add(x); v.Add(x); }
}
public class Counter { public string Name { get; set; } private long count; public Counter(string name, long start) { Name = name; count = start; } public long Bump(long by) { count += by; return count; } }
}'''},
    "go": {"go.mod": "module acm/demo\n\ngo 1.21\n", "util/util.go": '''package util

import ("errors"; "fmt")

func Double(x int64) int64 { return x * 2 }

func Divide(a, b int64) (int64, error) {
	if b == 0 { return 0, errors.New("division by zero") }
	q := a / b
	if (a%b != 0) && ((a < 0) != (b < 0)) { q-- }
	return q, nil
}

func Greet(name string) string { fmt.Println("hi " + name); return "hello " + name }

func Count(words []string) map[string]int64 { m := map[string]int64{}; for _, w := range words { m[w]++ }; return m }

func PushTwice(v *[]int64, x int64) { *v = append(*v, x, x) }

type Counter struct { Name string; Count int64 }

func NewCounter(name string, start int64) *Counter { return &Counter{Name: name, Count: start} }

func (c *Counter) Bump(by int64) int64 { c.Count += by; return c.Count }
'''},
    "rust": {"Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
             "src/lib.rs": "pub mod util;\n", "src/util.rs": '''use std::collections::HashMap;
#[derive(Debug)]
pub enum UtilError { ZeroDivision(String) }
pub fn double(x: i64) -> i64 { x * 2 }
pub fn divide(a: i64, b: i64) -> Result<i64, UtilError> {
    if b == 0 { return Err(UtilError::ZeroDivision(String::from("division by zero"))); }
    Ok(a.div_euclid(b) - if a.rem_euclid(b) != 0 && b < 0 { 1 } else { 0 })
}
pub fn greet(name: &str) -> String { println!("hi {}", name); format!("hello {}", name) }
pub fn count(words: &[String]) -> HashMap<String, i64> { let mut m = HashMap::new(); for w in words { *m.entry(w.clone()).or_insert(0) += 1; } m }
pub fn push_twice(v: &mut Vec<i64>, x: i64) { v.push(x); v.push(x); }
#[derive(Debug, Clone)]
pub struct Counter { pub name: String, count: i64 }
impl Counter {
    pub fn new(name: &str, start: i64) -> Self { Counter { name: name.to_string(), count: start } }
    pub fn bump(&mut self, by: i64) -> i64 { self.count += by; self.count }
}
'''},
    "cpp": {"include/util.hpp": '''#pragma once
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
namespace util {
inline long long double_(long long x) { return x * 2; }
inline long long divide(long long a, long long b) {
    if (b == 0) throw std::domain_error("division by zero");
    long long q = a / b; if ((a % b != 0) && ((a < 0) != (b < 0))) q--; return q;
}
inline std::string greet(const std::string& name) { std::cout << "hi " << name << "\\n"; return "hello " + name; }
inline std::map<std::string, long long> count(const std::vector<std::string>& words) { std::map<std::string, long long> m; for (auto& w : words) m[w]++; return m; }
inline void push_twice(std::vector<long long>& v, long long x) { v.push_back(x); v.push_back(x); }
class Counter { public: Counter(const std::string& name, long long start) : name(name), count(start) {} long long bump(long long by) { count += by; return count; } std::string name; long long count; };
}
'''},
    "c": {"util.c": '''#include <stdio.h>
long long double_it(long long x) { return x * 2; }
const char *greet(const char *name) { static char buf[256]; printf("hi %s\\n", name); snprintf(buf, sizeof buf, "hello %s", name); return buf; }
'''},
    "javascript": {"lib/util.js": '''function double(x) { return x * 2; }
function divide(a, b) { if (b === 0) throw new RangeError("division by zero"); return Math.floor(a / b); }
function greet(name) { console.log("hi " + name); return "hello " + name; }
function count(words) { const m = {}; for (const w of words) m[w] = (m[w] || 0) + 1; return m; }
function pushTwice(v, x) { v.push(x); v.push(x); }
class Counter { constructor(name, start) { this.name = name; this.count = start; } bump(by) { this.count += by; return this.count; } }
module.exports = { double, divide, greet, count, pushTwice, Counter };
'''},
    "matlab": {"double_it.m": "function y = double_it(x)\n  y = x * 2;\nend\n",
               "divide_it.m": "function q = divide_it(a, b)\n  if b == 0\n    error('demo:zerodiv', 'division by zero');\n  end\n  q = floor(a / b);\nend\n",
               "greet.m": "function s = greet(name)\n  printf('hi %s\\n', name);\n  s = ['hello ' name];\nend\n",
               "count_words.m": "function m = count_words(words)\n  m = struct();\n  for i = 1:numel(words)\n    w = words{i};\n    if isfield(m, w), m.(w) = m.(w) + 1; else, m.(w) = 1; end\n  end\nend\n"},
}
# planted bugs: same files with one change
BUGGY = {
    "go": ("util/util.go", "func Double(x int64) int64 { return x * 2 }", "func Double(x int64) int64 { return x * 3 }"),
    "cpp": ("include/util.hpp", "throw std::domain_error(", "throw std::invalid_argument("),
}

T = lambda t, v: {"t": t, "v": v}  # noqa: E731
I = lambda n: T("int", str(n))  # noqa: E731
WORDS = T("list", [T("text", "a"), T("text", "b"), T("text", "a")])
CASES = [  # (case id, neutral arguments, init)
    ("double", [I(21)], None), ("double_neg", [I(-4)], None), ("divide", [I(7), I(2)], None),
    ("divide_floor", [I(-7), I(2)], None), ("divide_zero", [I(1), I(0)], None), ("greet", [T("text", "bob")], None),
    ("count", [WORDS], None), ("push_twice", [T("list", [I(1)]), I(7)], None),
    ("bump", [I(5)], [T("text", "c"), I(10)]), ("missing", [], None),
]
# how each language names things: case id -> call
NAMES = {
    "python3": {"module": "demo.util", "double": "double", "divide": "divide", "greet": "greet", "count": "count",
                "push_twice": "push_twice", "class": "Counter", "bump": "bump", "ctor": None},
    "java": {"class_fn": "demo.UtilModule", "double": "dbl", "divide": "divide", "greet": "greet", "count": "count",
             "push_twice": "pushTwice", "class": "demo.Counter", "bump": "bump"},
    "csharp": {"class_fn": "Demo.UtilModule", "double": "Double", "divide": "Divide", "greet": "Greet", "count": "Count",
               "push_twice": "PushTwice", "class": "Demo.Counter", "bump": "Bump"},
    "go": {"module": "util", "double": "Double", "divide": "Divide", "greet": "Greet", "count": "Count",
           "push_twice": "PushTwice", "class": "Counter", "bump": "Bump", "ctor": "NewCounter"},
    "rust": {"module": "util", "double": "double", "divide": "divide", "greet": "greet", "count": "count",
             "push_twice": "push_twice", "class": "Counter", "bump": "bump", "ctor": "new"},
    "cpp": {"module": "include/util.hpp", "double": "double_", "divide": "divide", "greet": "greet", "count": "count",
            "push_twice": "push_twice", "class": "Counter", "bump": "bump"},
    "c": {"module": "util.c", "double": "double_it", "greet": "greet"},
    "javascript": {"module": "lib/util.js", "double": "double", "divide": "divide", "greet": "greet", "count": "count",
                   "push_twice": "pushTwice", "class": "Counter", "bump": "bump"},
    "matlab": {"double": "double_it", "divide": "divide_it", "greet": "greet", "count": "count_words"},
}
# Go has no list mutation through a value slice: its PushTwice takes *[]int64 (a pointer) -> not expressible by
# the reflective harness; Python's in-place append is matched by the other languages


def call_for(lang, case_id, args, init):
    n = NAMES[lang]
    key = case_id.split("_")[0] if case_id not in ("push_twice",) else "push_twice"
    key = {"divide": "divide", "double": "double"}.get(key, key)
    if case_id == "missing":
        base = {"kind": "function", "member": "no_such_function", "args": []}
        if "class_fn" in n:
            base["class"] = n["class_fn"]
        if "module" in n:
            base["module"] = n["module"]
        return base
    if case_id == "bump":
        if "bump" not in n:
            return None
        c = {"kind": "method", "class": n["class"], "member": n["bump"], "args": args, "init": init}
        if n.get("ctor"):
            c["constructor"] = n["ctor"]
        if "module" in n:
            c["module"] = n["module"]
        return c
    if key not in n:
        return None
    c = {"kind": "function", "member": n[key], "args": args}
    if "class_fn" in n:
        c["class"] = n["class_fn"]
    if "module" in n:
        c["module"] = n["module"]
    return c


def write_tree(root, files):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text.lstrip("\n"), encoding="utf-8")


def run_language(run, lang, files, tag=""):
    tree = run / "trees" / f"{lang}{tag}"
    write_tree(tree, files)
    cases = []
    for cid, args, init in CASES:
        call = call_for(lang, cid, args, init)
        if call is not None and not (lang == "go" and cid == "push_twice"):
            cases.append({"case_id": cid, "call": call})
    start = time.time()
    r = run_cases(lang, tree, cases, log_dir=run / "runs" / f"{lang}{tag}")
    r["seconds"] = round(time.time() - start, 1)
    return {c["case_id"]: x for c, x in zip(cases, r["results"])}, r


def main():
    run = new_run("polyglot", "verify")
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""), flush=True)

    print(f"== reference: Python 3 -> {run}")
    ref, r = run_language(run, "python3", FILES["python3"])
    check("python3 reference run", all(v["status"] in ("ok", "exception", "unresolved") for v in ref.values()),
          f"{len(ref)} cases in {r['seconds']} s")
    for lang in ("java", "csharp", "go", "rust", "cpp", "c", "javascript", "matlab"):
        res, r = run_language(run, lang, FILES[lang])
        prof = profile_for("python3", lang)
        rows, bad = [], []
        for cid, x in res.items():
            if cid == "missing":
                continue
            v = compare_results(ref[cid], {**x, "case_id": cid}, prof)
            rows.append(cid)
            if not v["equivalent"]:
                bad.append(f"{cid}: {v['diffs'][:2]}")
        unresolved = res.get("missing", {}).get("status")
        check(f"{lang}: builds, and every case is equivalent to Python ({prof})",
              r["built"] and not bad and unresolved == "unresolved",
              f"{len(rows)} cases in {r['seconds']} s; missing function -> {unresolved}"
              + (f"; DIFFER: {bad[:3]}" if bad else "") + (f"; build errors {r['errors'][:2]}" if not r["built"] else ""))
        write_json(run / "results" / f"{lang}.json", res)
    for lang, (path, old, new) in BUGGY.items():
        files = dict(FILES[lang])
        files[path] = files[path].replace(old, new)
        res, r = run_language(run, lang, files, "-buggy")
        prof = profile_for("python3", lang)
        found = [cid for cid, x in res.items() if cid != "missing"
                 and not compare_results(ref[cid], {**x, "case_id": cid}, prof)["equivalent"]]
        want = {"go": {"double", "double_neg"}, "cpp": {"divide_zero"}}[lang]
        check(f"{lang}: the planted bug is found ({new.strip()[:50]})", set(found) == want, f"inequivalent: {found}")
    ok = all(c["ok"] for c in checks)
    write_json(run / "summary.json", {"kind": "polyglot-verify", "ok": ok, "checks": checks})
    print(f"\n{sum(c['ok'] for c in checks)}/{len(checks)} checks passed\nlogs: {run}")
    shutil.rmtree(run / "trees", ignore_errors=True)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
