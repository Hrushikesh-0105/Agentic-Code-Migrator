// JavaScript side of the differential harness (sandbox/polyglot.py); run once per case:
//     node acm_harness.js   with ACM_CASES=<JSON-lines file> ACM_CASE=<index>, cwd = the code tree
// The module is loaded with require() (CommonJS) or import() (ES modules). Browser-style scripts that export
// nothing are evaluated in a vm context with minimal `window` / `document` stubs and their globals are used.
// Prints SENTINEL + the result in runner.py's type-tagged format.
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const util = require("util");

const SENTINEL = "__HARNESS_RESULT__";
const MAX = 10000;

function tag(t, v) { return v === undefined && t === "none" ? { t: "none" } : { t, v }; }

function enc(v, depth = 0, seen = new Set()) {
  if (depth > 40) return tag("repr", String(v));
  if (v === null || v === undefined) return { t: "none" };
  switch (typeof v) {
    case "boolean": return tag("bool", v);
    case "number":
      if (Number.isNaN(v)) return tag("float", "nan");
      if (!Number.isFinite(v)) return tag("float", v > 0 ? "inf" : "-inf");
      return Number.isInteger(v) && Math.abs(v) <= Number.MAX_SAFE_INTEGER ? tag("int", String(v)) : tag("float", v);
    case "bigint": return tag("int", v.toString());
    case "string": return tag("text", v);
    case "function": return tag("repr", `<function ${v.name || "anonymous"}>`);
    case "symbol": return tag("repr", v.toString());
  }
  if (Buffer.isBuffer(v) || v instanceof Uint8Array) return tag("bytes", Buffer.from(v).toString("base64"));
  if (seen.has(v)) return tag("repr", "<cycle>");
  seen.add(v);
  try {
    if (Array.isArray(v) || ArrayBuffer.isView(v)) return tag("list", Array.from(v).slice(0, MAX).map(x => enc(x, depth + 1, seen)));
    if (v instanceof Map) return tag("dict", [...v.entries()].map(([k, x]) => [enc(k, depth + 1, seen), enc(x, depth + 1, seen)]));
    if (v instanceof Set) return tag("set", [...v].map(x => enc(x, depth + 1, seen)));
    if (v instanceof Date) return tag("repr", v.toISOString());
    if (v instanceof RegExp) return tag("repr", String(v));
    if (v instanceof Promise) return tag("repr", "<Promise>");
    if (typeof v[Symbol.iterator] === "function" && !(v instanceof String)) {
      const items = [];
      for (const x of v) { if (items.length >= MAX) break; items.push(enc(x, depth + 1, seen)); }
      return { t: "iterator", kind: v.constructor ? v.constructor.name : "iterator", v: items };
    }
    const proto = Object.getPrototypeOf(v);
    const fields = Object.keys(v).map(k => [tag("text", k), enc(v[k], depth + 1, seen)]);
    if (proto === Object.prototype || proto === null) return tag("dict", fields);
    return { t: "object", class: (v.constructor && v.constructor.name) || "Object", v: tag("dict", fields) };
  } finally { seen.delete(v); }
}

function dec(v) {
  if (v === null || typeof v !== "object") return v;
  if (Array.isArray(v)) return v.map(dec);
  if (!("t" in v)) { const o = {}; for (const k of Object.keys(v)) o[k] = dec(v[k]); return o; }
  const x = v.v;
  switch (v.t) {
    case "none": return null;
    case "bool": return Boolean(x);
    case "int": { const n = Number(x); return Number.isSafeInteger(n) ? n : BigInt(x); }
    case "float": return x === "nan" ? NaN : x === "inf" ? Infinity : x === "-inf" ? -Infinity : Number(x);
    case "text": case "str": return String(x);
    case "bytes": return Buffer.from(x, "base64");
    case "list": case "tuple": return x.map(dec);
    case "set": return new Set(x.map(dec));
    case "dict": {
      // string keys -> a plain object (what JavaScript code uses as a dictionary), others -> a Map
      if (x.every(([k]) => k.t === "text" || k.t === "str")) { const o = {}; for (const [k, val] of x) o[k.v] = dec(val); return o; }
      return new Map(x.map(([k, val]) => [dec(k), dec(val)]));
    }
  }
  throw new Error(`cannot decode tag ${v.t}`);
}

class Unresolved extends Error {}

function stubs() {
  const el = () => ({ style: {}, classList: { add() {}, remove() {}, contains() { return false; } }, appendChild() {},
    setAttribute() {}, getAttribute() { return null; }, addEventListener() {}, removeEventListener() {}, children: [] });
  const document = { cookie: "", createElement: el, getElementById: () => null, querySelector: () => null,
    querySelectorAll: () => [], addEventListener() {}, body: el(), head: el(), documentElement: el() };
  const storage = new Map();
  const localStorage = { getItem: k => (storage.has(k) ? storage.get(k) : null), setItem: (k, v) => storage.set(k, String(v)),
    removeItem: k => storage.delete(k), clear: () => storage.clear() };
  return { document, localStorage, navigator: { userAgent: "node", language: "en" }, location: { href: "", hostname: "", protocol: "http:" } };
}

async function load(modPath) {
  const full = path.resolve(process.cwd(), modPath);
  if (!fs.existsSync(full)) throw new Unresolved(`module ${modPath} not found`);
  let mod = null;
  try {
    mod = full.endsWith(".mjs") ? await import("file://" + full) : require(full);
  } catch (e) {
    if (!(e instanceof SyntaxError) && !/Cannot use import statement|require is not defined|is not defined/.test(String(e))) throw e;
    try { mod = await import("file://" + full); } catch (e2) { mod = null; }
  }
  return { mod, full };
}

function sandboxGlobals(full) {
  const s = stubs();
  const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval, Buffer, require, module: { exports: {} },
    process, ...s };
  ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx; ctx.exports = ctx.module.exports;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(full, "utf8"), ctx, { filename: full });
  return ctx;
}

function lookup(obj, dotted) {
  let cur = obj;
  for (const part of dotted.split(".")) {
    if (cur === null || cur === undefined) return undefined;
    cur = cur[part];
  }
  return cur;
}

async function resolve(call) {
  const { mod, full } = await load(call.module);
  const name = call.kind === "function" || call.kind === "static" ? (call.class ? `${call.class}.${call.member}` : call.member)
    : call.class;
  const pick = m => {
    if (m === null || m === undefined) return undefined;
    let x = lookup(m, name);
    if (x === undefined && m.default) x = lookup(m.default, name);
    if (x === undefined && typeof m === "function" && (m.name === name || name === call.member)) x = m;
    return x;
  };
  let target = pick(mod);
  if (target === undefined) {
    const g = sandboxGlobals(full);
    target = pick(g.module.exports) || pick(g);
  }
  if (target === undefined) throw new Unresolved(`${name} is not defined in ${call.module}`);
  return target;
}

async function main() {
  const lines = fs.readFileSync(process.env.ACM_CASES, "utf8").split("\n").filter(l => l.trim());
  const c = JSON.parse(lines[Number(process.env.ACM_CASE)]);
  const call = c.call;
  const res = { case_id: c.case_id, node: process.version };
  const out = [];
  const realWrite = process.stdout.write.bind(process.stdout);
  const realLog = console.log;
  let args = (call.args || []).map(dec), inst = null;
  process.stdout.write = (s, ...r) => { out.push(String(s)); return true; };
  console.log = (...a) => { out.push(util.format(...a) + "\n"); };
  try {
    const target = await resolve(call);
    let value;
    if (call.kind === "constructor") value = new target(...args);
    else if (call.kind === "method" || call.kind === "property") {
      inst = new target(...(call.init || []).map(dec));
      if (call.kind === "property" || typeof inst[call.member] !== "function") {
        if (!(call.member in inst)) throw new Unresolved(`no member ${call.member} in ${call.class}`);
        value = typeof inst[call.member] === "function" ? inst[call.member]() : inst[call.member];
        args = [];
      } else value = inst[call.member](...args);
    } else {
      if (typeof target !== "function") value = target;
      else value = target(...args);
    }
    if (value instanceof Promise) value = await value;
    res.status = "ok";
    res.value = enc(value);
  } catch (e) {
    if (e instanceof Unresolved) { res.status = "unresolved"; res.message = e.message; }
    else {
      res.status = "exception";
      const hier = [];
      for (let p = e && Object.getPrototypeOf(e); p && p.constructor && p.constructor !== Object; p = Object.getPrototypeOf(p)) hier.push(p.constructor.name);
      res.exception = { type: (e && e.constructor && e.constructor.name) || typeof e, hierarchy: hier.length ? hier : ["Error"],
        message: e && e.message !== undefined ? String(e.message) : String(e), traceback: String((e && e.stack) || "").slice(0, 4000) };
    }
  } finally {
    process.stdout.write = realWrite;
    console.log = realLog;
  }
  res.args_after = args.map(a => enc(a));
  res.self_after = inst !== null ? enc(inst) : null;
  res.stdout = out.join("");
  res.stderr = "";
  realWrite(SENTINEL + JSON.stringify(res) + "\n");
}

main().catch(e => { process.stdout.write(SENTINEL + JSON.stringify({ status: "crash", stderr: String(e && e.stack || e) }) + "\n"); });
