// Runs INSIDE the java17 sandbox container: the Java side of the differential harness (runner.py is the Python
// side). stdin: one JSON test case per line. stdout: one line per case, SENTINEL + JSON result, in runner.py's
// type-tagged value format, so sandbox/compare.py can compare a Python run with a Java run case by case.
//
//   java -cp <dir with this class> AcmHarness <compiled classes dir> [--timeout SECONDS]
//
// A case: {"case_id": ..., "java": {"class": "pkg.Outer$Inner", "kind": "static" | "instance" | "new",
//          "member": "methodName", "init": [tagged args of the constructor], "args": [tagged args]}}
//   static    call a static method of the class
//   instance  construct the class with `init`, call the method on it; self_after is the object afterwards
//   new       construct the class with `args`; the value is the new object
// Arguments are converted to the parameter types of the method or constructor chosen by name and arity (an
// overload that cannot take the values is skipped). Every case gets a fresh class loader, so static state cannot
// leak between cases, and its own thread, so a hang becomes a "timeout". A class, method or constructor that
// cannot be found or called with the arguments is status "unresolved": a defect of the translation's API, not
// a behaviour.
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.PrintStream;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.reflect.*;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class AcmHarness {
    static final String SENTINEL = "__HARNESS_RESULT__";
    static final int MAX_ITEMS = 10000;
    static final Object FAIL = new Object();

    // ------------------------------------------------------------------------------------------------ JSON
    static final class Json {
        final String s;
        int i;

        Json(String s) { this.s = s; }

        static Object parse(String s) { Json j = new Json(s); Object v = j.value(); j.ws(); return v; }

        void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }

        RuntimeException err() { return new IllegalArgumentException("bad JSON at offset " + i); }

        void expect(char c) { ws(); if (i >= s.length() || s.charAt(i++) != c) throw err(); }

        Object value() {
            ws();
            if (i >= s.length()) throw err();
            char c = s.charAt(i);
            if (c == '{') {
                i++;
                Map<String, Object> m = new LinkedHashMap<>();
                ws();
                if (s.charAt(i) == '}') { i++; return m; }
                while (true) {
                    ws();
                    String k = str();
                    expect(':');
                    m.put(k, value());
                    ws();
                    char d = s.charAt(i++);
                    if (d == '}') return m;
                    if (d != ',') throw err();
                }
            }
            if (c == '[') {
                i++;
                List<Object> l = new ArrayList<>();
                ws();
                if (s.charAt(i) == ']') { i++; return l; }
                while (true) {
                    l.add(value());
                    ws();
                    char d = s.charAt(i++);
                    if (d == ']') return l;
                    if (d != ',') throw err();
                }
            }
            if (c == '"') return str();
            if (s.startsWith("true", i)) { i += 4; return Boolean.TRUE; }
            if (s.startsWith("false", i)) { i += 5; return Boolean.FALSE; }
            if (s.startsWith("null", i)) { i += 4; return null; }
            int st = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            String n = s.substring(st, i);
            if (n.isEmpty()) throw err();
            if (n.contains(".") || n.contains("e") || n.contains("E")) return Double.parseDouble(n);
            return new BigInteger(n);
        }

        String str() {
            expect('"');
            StringBuilder b = new StringBuilder();
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') return b.toString();
                if (c != '\\') { b.append(c); continue; }
                char e = s.charAt(i++);
                switch (e) {
                    case 'n': b.append('\n'); break;
                    case 't': b.append('\t'); break;
                    case 'r': b.append('\r'); break;
                    case 'b': b.append('\b'); break;
                    case 'f': b.append('\f'); break;
                    case 'u': b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                    default: b.append(e);
                }
            }
        }
    }

    static String quote(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (int k = 0; k < s.length(); k++) {
            char c = s.charAt(k);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20 || c > 0x7e) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }

    @SuppressWarnings("unchecked")
    static String dump(Object o) {
        if (o == null) return "null";
        if (o instanceof String) return quote((String) o);
        if (o instanceof Double) {
            double d = (Double) o;
            return Double.isNaN(d) || Double.isInfinite(d) ? quote(String.valueOf(d)) : String.valueOf(d);
        }
        if (o instanceof Number || o instanceof Boolean) return o.toString();
        StringBuilder b = new StringBuilder();
        if (o instanceof Map) {
            b.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) o).entrySet()) {
                if (!first) b.append(", ");
                first = false;
                b.append(quote(e.getKey())).append(": ").append(dump(e.getValue()));
            }
            return b.append('}').toString();
        }
        b.append('[');
        boolean first = true;
        for (Object x : (List<Object>) o) {
            if (!first) b.append(", ");
            first = false;
            b.append(dump(x));
        }
        return b.append(']').toString();
    }

    // ------------------------------------------------------------------------------------------------ encode
    static Map<String, Object> tag(String t, Object v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("t", t);
        if (v != null || !t.equals("none")) m.put("v", v);
        return m;
    }

    static boolean isJdk(Class<?> c) {
        String n = c.getName();
        return n.startsWith("java.") || n.startsWith("javax.") || n.startsWith("jdk.") || n.startsWith("sun.")
            || n.startsWith("com.sun.");
    }

    static String repr(Object v) {
        try { return String.valueOf(v); } catch (Throwable t) { return "<" + v.getClass().getName() + ">"; }
    }

    static Map<String, Object> enc(Object v, int depth, IdentityHashMap<Object, Boolean> seen) {
        if (depth > 40) return tag("repr", repr(v));
        if (v == null) { Map<String, Object> m = new LinkedHashMap<>(); m.put("t", "none"); return m; }
        if (v instanceof Boolean) return tag("bool", v);
        if (v instanceof Byte || v instanceof Short || v instanceof Integer || v instanceof Long
            || v instanceof BigInteger) return tag("int", v.toString());
        if (v instanceof Float || v instanceof Double || v instanceof BigDecimal) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d)) return tag("float", "nan");
            if (Double.isInfinite(d)) return tag("float", d > 0 ? "inf" : "-inf");
            return tag("float", d);
        }
        if (v instanceof Character || v instanceof CharSequence) return tag("text", v.toString());
        if (v instanceof byte[]) return tag("bytes", Base64.getEncoder().encodeToString((byte[]) v));
        if (v instanceof Optional) return enc(((Optional<?>) v).orElse(null), depth, seen);
        int d = depth + 1;
        if (seen.containsKey(v)) return tag("repr", "<cycle " + v.getClass().getSimpleName() + ">");
        seen.put(v, true);
        try {
            if (v.getClass().isArray()) {
                List<Object> l = new ArrayList<>();
                for (int k = 0; k < Math.min(Array.getLength(v), MAX_ITEMS); k++) l.add(enc(Array.get(v, k), d, seen));
                return tag("list", l);
            }
            if (v instanceof Map) {
                List<Object> l = new ArrayList<>();
                for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet())
                    l.add(Arrays.asList(enc(e.getKey(), d, seen), enc(e.getValue(), d, seen)));
                return tag("dict", l);
            }
            if (v instanceof Set) return tag("set", items(((Set<?>) v).iterator(), d, seen));
            if (v instanceof Collection) return tag("list", items(((Collection<?>) v).iterator(), d, seen));
            if (v instanceof Iterator || v instanceof Iterable || v instanceof java.util.stream.BaseStream) {
                Iterator<?> it = v instanceof Iterator ? (Iterator<?>) v : v instanceof Iterable
                    ? ((Iterable<?>) v).iterator() : ((java.util.stream.BaseStream<?, ?>) v).iterator();
                Map<String, Object> m = tag("iterator", items(it, d, seen));
                m.put("kind", v.getClass().getSimpleName());
                return m;
            }
            // JDK objects (and their fields) are implementation detail; a project exception shows its own fields
            if (v instanceof Enum || v instanceof Class || isJdk(v.getClass())) return tag("repr", repr(v));
            List<Object> attrs = new ArrayList<>();
            for (Class<?> c = v.getClass(); c != null && c != Object.class; c = c.getSuperclass()) {
                if (isJdk(c)) break;
                for (Field f : c.getDeclaredFields()) {
                    if (Modifier.isStatic(f.getModifiers()) || f.isSynthetic()) continue;
                    Object x;
                    try { f.setAccessible(true); x = f.get(v); } catch (Throwable t) { continue; }
                    attrs.add(Arrays.asList(tag("text", f.getName()), enc(x, d + 1, seen)));
                }
            }
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("t", "object");
            m.put("class", v.getClass().getSimpleName());
            m.put("v", tag("dict", attrs));
            return m;
        } finally {
            seen.remove(v);
        }
    }

    static List<Object> items(Iterator<?> it, int d, IdentityHashMap<Object, Boolean> seen) {
        List<Object> l = new ArrayList<>();
        for (int k = 0; k < MAX_ITEMS && it.hasNext(); k++) l.add(enc(it.next(), d, seen));
        return l;
    }

    // ------------------------------------------------------------------------------------------------ decode
    /** Tagged (or plain) JSON -> a neutral Java value: BigInteger, Double, Boolean, String, byte[], List,
     *  LinkedHashSet, LinkedHashMap or null. conv() then fits it to a parameter type. */
    @SuppressWarnings("unchecked")
    static Object decode(Object v) {
        if (v instanceof Map) {
            Map<String, Object> m = (Map<String, Object>) v;
            if (m.containsKey("t")) {
                String t = (String) m.get("t");
                Object x = m.get("v");
                switch (t) {
                    case "none": return null;
                    case "bool": return x;
                    case "int": return new BigInteger(x.toString());
                    case "float":
                        if (x instanceof String) {
                            String s = (String) x;
                            return s.equals("nan") ? Double.NaN : s.startsWith("-") ? Double.NEGATIVE_INFINITY
                                : s.contains("inf") ? Double.POSITIVE_INFINITY : Double.parseDouble(s);
                        }
                        return ((Number) x).doubleValue();
                    case "text": case "str": return x;
                    case "bytes": return Base64.getDecoder().decode((String) x);
                    case "list": case "tuple": {
                        List<Object> l = new ArrayList<>();
                        for (Object y : (List<Object>) x) l.add(decode(y));
                        return l;
                    }
                    case "set": {
                        Set<Object> s = new LinkedHashSet<>();
                        for (Object y : (List<Object>) x) s.add(decode(y));
                        return s;
                    }
                    case "dict": {
                        Map<Object, Object> d = new LinkedHashMap<>();
                        for (Object kv : (List<Object>) x) {
                            List<Object> p = (List<Object>) kv;
                            d.put(decode(p.get(0)), decode(p.get(1)));
                        }
                        return d;
                    }
                    default: throw new IllegalArgumentException("cannot decode tag " + t);
                }
            }
            Map<Object, Object> d = new LinkedHashMap<>();
            for (Map.Entry<String, Object> e : m.entrySet()) d.put(e.getKey(), decode(e.getValue()));
            return d;
        }
        if (v instanceof List) {
            List<Object> l = new ArrayList<>();
            for (Object y : (List<Object>) v) l.add(decode(y));
            return l;
        }
        return v;
    }

    /** A neutral value for an Object parameter: the narrowest boxed integer that holds it. */
    @SuppressWarnings("unchecked")
    static Object natural(Object v) {
        if (v instanceof BigInteger) {
            BigInteger b = (BigInteger) v;
            return b.bitLength() < 32 ? (Object) b.intValue() : b.bitLength() < 64 ? (Object) b.longValue() : b;
        }
        if (v instanceof List) {
            List<Object> l = new ArrayList<>();
            for (Object x : (List<Object>) v) l.add(natural(x));
            return l;
        }
        if (v instanceof Set) {
            Set<Object> s = new LinkedHashSet<>();
            for (Object x : (Set<Object>) v) s.add(natural(x));
            return s;
        }
        if (v instanceof Map) {
            Map<Object, Object> d = new LinkedHashMap<>();
            for (Map.Entry<Object, Object> e : ((Map<Object, Object>) v).entrySet())
                d.put(natural(e.getKey()), natural(e.getValue()));
            return d;
        }
        return v;
    }

    static Class<?> raw(Type t) {
        if (t instanceof Class) return (Class<?>) t;
        if (t instanceof ParameterizedType) return raw(((ParameterizedType) t).getRawType());
        if (t instanceof GenericArrayType) return Array.newInstance(raw(((GenericArrayType) t).getGenericComponentType()), 0).getClass();
        return Object.class;  // type variables and wildcards
    }

    static Type typeArg(Type t, int k) {
        if (t instanceof ParameterizedType) {
            Type[] a = ((ParameterizedType) t).getActualTypeArguments();
            if (k < a.length) return a[k];
        }
        return Object.class;
    }

    static Class<?> box(Class<?> c) {
        if (!c.isPrimitive()) return c;
        if (c == int.class) return Integer.class;
        if (c == long.class) return Long.class;
        if (c == double.class) return Double.class;
        if (c == float.class) return Float.class;
        if (c == boolean.class) return Boolean.class;
        if (c == short.class) return Short.class;
        if (c == byte.class) return Byte.class;
        if (c == char.class) return Character.class;
        return c;
    }

    @SuppressWarnings("unchecked")
    static Object conv(Object v, Type type) {
        Class<?> c = raw(type);
        if (v == null) return c.isPrimitive() ? FAIL : null;
        if (c == Object.class) return natural(v);
        Class<?> b = box(c);
        if (v instanceof BigInteger) {
            BigInteger n = (BigInteger) v;
            if (b == Integer.class) return n.bitLength() < 32 ? (Object) n.intValue() : FAIL;
            if (b == Long.class) return n.bitLength() < 64 ? (Object) n.longValue() : FAIL;
            if (b == Short.class) return n.bitLength() < 16 ? (Object) n.shortValue() : FAIL;
            if (b == Byte.class) return n.bitLength() < 8 ? (Object) n.byteValue() : FAIL;
            if (b == Double.class) return n.doubleValue();
            if (b == Float.class) return n.floatValue();
            if (b == BigInteger.class) return n;
            if (b == BigDecimal.class) return new BigDecimal(n);
            if (b == Number.class) return natural(n);
            return FAIL;
        }
        if (v instanceof Double) {
            double d = (Double) v;
            if (b == Double.class || b == Number.class) return d;
            if (b == Float.class) return (float) d;
            if (b == BigDecimal.class) return new BigDecimal(d);
            return FAIL;
        }
        if (v instanceof Boolean) return b == Boolean.class ? v : FAIL;
        if (v instanceof String) {
            String s = (String) v;
            if (b == String.class || b == CharSequence.class) return s;
            if (b == Character.class) return s.length() == 1 ? (Object) s.charAt(0) : FAIL;
            return FAIL;
        }
        if (v instanceof byte[]) return c == byte[].class ? v : FAIL;
        if (v instanceof List) {
            List<Object> l = (List<Object>) v;
            if (c.isArray()) {
                Class<?> ct = c.getComponentType();
                Object arr = Array.newInstance(ct, l.size());
                for (int k = 0; k < l.size(); k++) {
                    Object x = conv(l.get(k), ct);
                    if (x == FAIL) return FAIL;
                    Array.set(arr, k, x);
                }
                return arr;
            }
            if (c.isAssignableFrom(ArrayList.class)) {
                List<Object> out = new ArrayList<>();
                for (Object x : l) {
                    Object y = conv(x, typeArg(type, 0));
                    if (y == FAIL) return FAIL;
                    out.add(y);
                }
                return out;
            }
            return FAIL;
        }
        if (v instanceof Set) {
            if (!c.isAssignableFrom(LinkedHashSet.class)) return FAIL;
            Set<Object> out = new LinkedHashSet<>();
            for (Object x : (Set<Object>) v) {
                Object y = conv(x, typeArg(type, 0));
                if (y == FAIL) return FAIL;
                out.add(y);
            }
            return out;
        }
        if (v instanceof Map) {
            if (!c.isAssignableFrom(LinkedHashMap.class)) return FAIL;
            Map<Object, Object> out = new LinkedHashMap<>();
            for (Map.Entry<Object, Object> e : ((Map<Object, Object>) v).entrySet()) {
                Object k = conv(e.getKey(), typeArg(type, 0)), x = conv(e.getValue(), typeArg(type, 1));
                if (k == FAIL || x == FAIL) return FAIL;
                out.put(k, x);
            }
            return out;
        }
        return FAIL;
    }

    /** Arguments for `m`, or null if it cannot take them (arity or types). */
    static Object[] bind(Executable m, List<Object> args) {
        Type[] pts = m.getGenericParameterTypes();
        int n = pts.length;
        if (m.isVarArgs() ? args.size() < n - 1 : args.size() != n) return null;
        Object[] out = new Object[n];
        int fixed = m.isVarArgs() ? n - 1 : n;
        for (int k = 0; k < fixed; k++) {
            Object x = conv(args.get(k), pts[k]);
            if (x == FAIL) return null;
            out[k] = x;
        }
        if (m.isVarArgs()) {
            Class<?> ct = m.getParameterTypes()[n - 1].getComponentType();
            Object arr = Array.newInstance(ct, args.size() - fixed);
            for (int k = fixed; k < args.size(); k++) {
                Object x = conv(args.get(k), ct);
                if (x == FAIL) return null;
                Array.set(arr, k - fixed, x);
            }
            out[n - 1] = arr;
        }
        return out;
    }

    static <T extends Executable> List<T> ordered(Collection<T> c) {
        List<T> l = new ArrayList<>(c);
        l.sort(Comparator.comparing((T m) -> m.isVarArgs()).thenComparing(m -> m.toGenericString()));
        return l;
    }

    static String signatures(Collection<? extends Executable> c) {
        StringBuilder b = new StringBuilder();
        for (Executable m : ordered(c)) b.append(b.length() > 0 ? "; " : "").append(m.toGenericString());
        return b.length() > 0 ? b.toString() : "none";
    }

    static final class Unresolved extends Exception {
        Unresolved(String m) { super(m); }
    }

    /** How well a value fits a parameter type: 0 = its natural Java type, higher = a wider or looser fit. Like
     *  Java's own choice of the most specific overload: an int goes to long(Integer) before Object before double. */
    static int cost(Object v, Type type) {
        Class<?> c = box(raw(type));
        if (v == null) return c == Object.class ? 2 : 0;
        if (v instanceof BigInteger)
            return c == Integer.class || c == Long.class ? 0 : c == BigInteger.class || c == Short.class
                || c == Byte.class ? 1 : c == Number.class ? 2 : c == Object.class ? 3 : 5;
        if (v instanceof Double) return c == Double.class ? 0 : c == Float.class || c == Number.class ? 1 : c == Object.class ? 3 : 4;
        if (v instanceof String) return c == String.class ? 0 : c == CharSequence.class || c == Character.class ? 1 : 3;
        if (v instanceof byte[] || v instanceof Boolean) return c == Object.class ? 3 : 0;
        if (v instanceof List) return c.isArray() || c == List.class || c == ArrayList.class ? 0 : c == Object.class ? 3 : 1;
        if (v instanceof Set) return c == Set.class || c == LinkedHashSet.class ? 0 : c == Object.class ? 3 : 1;
        if (v instanceof Map) return c == Map.class || c == LinkedHashMap.class ? 0 : c == Object.class ? 3 : 1;
        return 3;
    }

    static int cost(Executable m, List<Object> args) {
        Type[] pts = m.getGenericParameterTypes();
        int total = m.isVarArgs() ? 10 : 0;
        for (int k = 0; k < args.size(); k++) {
            Type t = m.isVarArgs() && k >= pts.length - 1 ? m.getParameterTypes()[pts.length - 1].getComponentType() : pts[k];
            total += cost(args.get(k), t);
        }
        return total;
    }

    static Object[] pick(Collection<? extends Executable> cands, List<Object> args, Executable[] chosen, String what)
        throws Unresolved {
        Object[] best = null;
        int bestCost = Integer.MAX_VALUE;
        for (Executable m : ordered(cands)) {
            Object[] a = bind(m, args);
            if (a == null) continue;
            int c = cost(m, args);
            if (c < bestCost) { best = a; bestCost = c; chosen[0] = m; }
        }
        if (best != null) { chosen[0].setAccessible(true); return best; }
        throw new Unresolved("no " + what + " accepts " + args.size() + " argument(s) of these types; found: "
            + signatures(cands));
    }

    static List<Method> methods(Class<?> cls, String name, boolean wantStatic) {
        Map<String, Method> found = new LinkedHashMap<>();  // signature -> most derived
        for (Class<?> c = cls; c != null; c = c.getSuperclass())
            for (Method m : c.getDeclaredMethods())
                if (m.getName().equals(name) && !m.isBridge() && (!wantStatic || Modifier.isStatic(m.getModifiers()))) {
                    String key = name + Arrays.toString(m.getParameterTypes());
                    found.putIfAbsent(key, m);
                }
        for (Class<?> i : cls.getInterfaces())
            for (Method m : i.getMethods())
                if (m.getName().equals(name) && m.isDefault() && !wantStatic)
                    found.putIfAbsent(name + Arrays.toString(m.getParameterTypes()), m);
        return new ArrayList<>(found.values());
    }

    // ------------------------------------------------------------------------------------------------ run
    static Throwable unwrap(Throwable t) {
        while ((t instanceof InvocationTargetException || t instanceof ExceptionInInitializerError)
            && t.getCause() != null) t = t.getCause();
        return t;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> runCase(Map<String, Object> c, File classes, double timeout) throws Exception {
        Map<String, Object> res = new LinkedHashMap<>();
        res.put("case_id", c.get("case_id"));
        res.put("java", System.getProperty("java.version"));
        Map<String, Object> j = (Map<String, Object>) c.get("java");
        List<Object> args = new ArrayList<>(), init = new ArrayList<>();
        for (Object a : (List<Object>) j.getOrDefault("args", new ArrayList<>())) args.add(decode(a));
        for (Object a : (List<Object>) j.getOrDefault("init", new ArrayList<>())) init.add(decode(a));
        String kind = (String) j.get("kind"), member = (String) j.get("member"), name = (String) j.get("class");
        ByteArrayOutputStream out = new ByteArrayOutputStream(), err = new ByteArrayOutputStream();
        Object[] box = new Object[5];  // 0 value, 1 instance, 2 throwable, 3 unresolved message, 4 bound args
        Thread t = new Thread(() -> {
            try (URLClassLoader loader = new URLClassLoader(new URL[]{classes.toURI().toURL()},
                ClassLoader.getPlatformClassLoader())) {
                Thread.currentThread().setContextClassLoader(loader);
                Class<?> cls;
                try {
                    cls = Class.forName(name, false, loader);
                } catch (ClassNotFoundException | NoClassDefFoundError e) {
                    throw new Unresolved("class " + name + " not found");
                }
                Executable[] chosen = new Executable[1];
                if (kind.equals("new")) {
                    Object[] a = pick(Arrays.asList(cls.getDeclaredConstructors()), args, chosen, "constructor of " + name);
                    box[4] = a;
                    box[0] = ((Constructor<?>) chosen[0]).newInstance(a);
                } else if (kind.equals("static")) {
                    List<Method> ms = methods(cls, member, true);
                    if (ms.isEmpty()) throw new Unresolved("no static method " + member + " in " + name
                        + (methods(cls, member, false).isEmpty() ? "" : " (there is an instance method of that name)"));
                    Object[] a = pick(ms, args, chosen, "static method " + name + "." + member);
                    box[4] = a;
                    box[0] = ((Method) chosen[0]).invoke(null, a);
                } else {
                    Object[] ia = pick(Arrays.asList(cls.getDeclaredConstructors()), init, chosen, "constructor of " + name);
                    Object inst = ((Constructor<?>) chosen[0]).newInstance(ia);
                    box[1] = inst;
                    List<Method> ms = methods(cls, member, false);
                    if (ms.isEmpty()) throw new Unresolved("no method " + member + " in " + name);
                    Object[] a = pick(ms, args, chosen, "method " + name + "." + member);
                    box[4] = a;
                    box[0] = ((Method) chosen[0]).invoke(Modifier.isStatic(chosen[0].getModifiers()) ? null : inst, a);
                }
            } catch (Unresolved e) {
                box[3] = e.getMessage();
            } catch (Throwable e) {
                box[2] = unwrap(e);
            }
        });
        PrintStream realOut = System.out, realErr = System.err;
        long start = System.nanoTime();
        System.setOut(new PrintStream(out, true, "UTF-8"));
        System.setErr(new PrintStream(err, true, "UTF-8"));
        t.setDaemon(true);
        t.start();
        t.join((long) (timeout * 1000));
        System.setOut(realOut);
        System.setErr(realErr);
        res.put("duration_ms", (System.nanoTime() - start) / 1_000_000);
        if (t.isAlive()) {
            res.put("status", "timeout");
            return res;
        }
        IdentityHashMap<Object, Boolean> seen = new IdentityHashMap<>();
        if (box[3] != null) {
            res.put("status", "unresolved");
            res.put("message", box[3]);
        } else if (box[2] != null) {
            Throwable e = (Throwable) box[2];
            Map<String, Object> ex = new LinkedHashMap<>();
            List<Object> hier = new ArrayList<>();
            for (Class<?> k = e.getClass(); k != null && k != Object.class; k = k.getSuperclass()) hier.add(k.getSimpleName());
            StringWriter sw = new StringWriter();
            e.printStackTrace(new PrintWriter(sw));
            String tb = sw.toString();
            ex.put("type", e.getClass().getSimpleName());
            ex.put("hierarchy", hier);
            ex.put("message", String.valueOf(e.getMessage()));
            ex.put("traceback", tb.length() > 4000 ? tb.substring(0, 4000) : tb);
            res.put("status", "exception");
            res.put("exception", ex);
        } else {
            res.put("status", "ok");
            res.put("value", enc(box[0], 0, seen));
        }
        List<Object> after = new ArrayList<>();
        if (box[4] != null) for (Object a : (Object[]) box[4]) after.add(enc(a, 0, seen));
        res.put("args_after", after);
        res.put("self_after", box[1] != null ? enc(box[1], 0, seen) : null);
        res.put("stdout", out.toString("UTF-8"));
        res.put("stderr", err.toString("UTF-8"));
        return res;
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] argv) throws Exception {
        File classes = new File(argv[0]);
        double timeout = 10;
        for (int k = 1; k < argv.length - 1; k++) if (argv[k].equals("--timeout")) timeout = Double.parseDouble(argv[k + 1]);
        PrintStream out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true, "UTF-8");
        java.io.BufferedReader in = new java.io.BufferedReader(new java.io.InputStreamReader(System.in, StandardCharsets.UTF_8));
        String line;
        while ((line = in.readLine()) != null) {
            if (line.trim().isEmpty()) continue;
            Map<String, Object> c = (Map<String, Object>) Json.parse(line), res;
            try {
                res = runCase(c, classes, timeout);
            } catch (Throwable e) {
                res = new LinkedHashMap<>();
                res.put("case_id", c.get("case_id"));
                res.put("status", "crash");
                res.put("stderr", String.valueOf(e));
            }
            out.println(SENTINEL + dump(res));
        }
        out.flush();
        System.exit(0);  // timed-out case threads may still be running
    }
}
