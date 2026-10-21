// C# side of the differential harness (sandbox/polyglot.py): compiled into one assembly with the code under test,
// run once per case: `dotnet acm.dll` with ACM_CASES=<JSON-lines file> ACM_CASE=<index>.
// It finds the type and member by reflection, converts the type-tagged arguments to the parameter types (choosing
// the most specific overload, as the Java harness does), calls, and prints SENTINEL + the result in runner.py's
// format. A missing type / member, or no overload taking the arguments, is "unresolved".
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Numerics;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;

public static class AcmHarness
{
    const string Sentinel = "__HARNESS_RESULT__";
    static readonly object Fail = new object();

    class Unresolved : Exception { public Unresolved(string m) : base(m) { } }

    // ---------------------------------------------------------------------------------------------- JSON out
    static string Q(string s)
    {
        var b = new StringBuilder("\"");
        foreach (var c in s)
        {
            switch (c)
            {
                case '"': b.Append("\\\""); break;
                case '\\': b.Append("\\\\"); break;
                case '\n': b.Append("\\n"); break;
                case '\r': b.Append("\\r"); break;
                case '\t': b.Append("\\t"); break;
                default:
                    if (c < 0x20 || c > 0x7e) b.Append("\\u" + ((int)c).ToString("x4"));
                    else b.Append(c);
                    break;
            }
        }
        return b.Append('"').ToString();
    }

    static string Tag(string t, string v) => "{\"t\": \"" + t + "\", \"v\": " + v + "}";
    static string None() => "{\"t\": \"none\"}";

    static bool IsSystem(Type t) => (t.Namespace ?? "").StartsWith("System") || (t.Namespace ?? "").StartsWith("Microsoft");

    static string Enc(object v, int depth, HashSet<object> seen)
    {
        if (v == null) return None();
        if (depth > 40) return Tag("repr", Q(v.ToString() ?? ""));
        switch (v)
        {
            case bool b: return "{\"t\": \"bool\", \"v\": " + (b ? "true" : "false") + "}";
            case sbyte or byte or short or ushort or int or uint or long or ulong or BigInteger:
                return Tag("int", Q(Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture) ?? "0"));
            case float or double or decimal:
                double d = Convert.ToDouble(v);
                if (double.IsNaN(d)) return Tag("float", "\"nan\"");
                if (double.IsInfinity(d)) return Tag("float", d > 0 ? "\"inf\"" : "\"-inf\"");
                var s = d.ToString("R", System.Globalization.CultureInfo.InvariantCulture);
                if (!s.Contains('.') && !s.Contains('E')) s += ".0";
                return Tag("float", s);
            case string str: return Tag("text", Q(str));
            case char ch: return Tag("text", Q(ch.ToString()));
            case byte[] bytes: return Tag("bytes", Q(Convert.ToBase64String(bytes)));
        }
        var t = v.GetType();
        if (t.IsEnum) return Tag("text", Q(v.ToString() ?? ""));
        if (!t.IsValueType)
        {
            if (seen.Contains(v)) return Tag("repr", Q("<cycle " + t.Name + ">"));
            seen.Add(v);
        }
        try
        {
            if (v is IDictionary dict)
            {
                var parts = new List<string>();
                foreach (DictionaryEntry e in dict) parts.Add("[" + Enc(e.Key, depth + 1, seen) + ", " + Enc(e.Value, depth + 1, seen) + "]");
                return Tag("dict", "[" + string.Join(", ", parts) + "]");
            }
            if (t.GetInterfaces().Any(i => i.IsGenericType && i.GetGenericTypeDefinition() == typeof(ISet<>)))
                return Tag("set", "[" + string.Join(", ", ((IEnumerable)v).Cast<object>().Select(x => Enc(x, depth + 1, seen))) + "]");
            if (t.IsGenericType && t.GetGenericTypeDefinition() == typeof(KeyValuePair<,>))
                return Tag("tuple", "[" + Enc(t.GetProperty("Key")!.GetValue(v), depth + 1, seen) + ", " + Enc(t.GetProperty("Value")!.GetValue(v), depth + 1, seen) + "]");
            if (v is ITuple tup)
            {
                var items = new List<string>();
                for (int i = 0; i < tup.Length; i++) items.Add(Enc(tup[i], depth + 1, seen));
                return Tag("tuple", "[" + string.Join(", ", items) + "]");
            }
            if (v is IEnumerable en)
            {
                var items = new List<string>();
                int n = 0;
                foreach (var x in en) { if (n++ > 10000) break; items.Add(Enc(x, depth + 1, seen)); }
                bool lazy = !(v is IList) && !(v is Array) && !(v is ICollection);
                return lazy ? "{\"t\": \"iterator\", \"kind\": " + Q(t.Name) + ", \"v\": [" + string.Join(", ", items) + "]}"
                            : Tag("list", "[" + string.Join(", ", items) + "]");
            }
            if (IsSystem(t) || v is Delegate || v is Type) return Tag("repr", Q(v.ToString() ?? t.Name));
            var fields = new List<string>();
            for (var k = t; k != null && k != typeof(object) && !IsSystem(k); k = k.BaseType)
                foreach (var f in k.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                {
                    var name = f.Name.StartsWith("<") && f.Name.Contains(">k__BackingField") ? f.Name.Substring(1, f.Name.IndexOf('>') - 1) : f.Name;
                    fields.Add("[" + Tag("text", Q(name)) + ", " + Enc(f.GetValue(v), depth + 1, seen) + "]");
                }
            return "{\"t\": \"object\", \"class\": " + Q(t.Name) + ", \"v\": " + Tag("dict", "[" + string.Join(", ", fields) + "]") + "}";
        }
        finally { if (!t.IsValueType) seen.Remove(v); }
    }

    // ---------------------------------------------------------------------------------------------- JSON in
    static object Decode(JsonElement e)
    {
        if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty("t", out var tagEl))
        {
            return e.ValueKind switch
            {
                JsonValueKind.String => e.GetString(),
                JsonValueKind.Number => e.TryGetInt64(out var l) ? new BigInteger(l) : (object)e.GetDouble(),
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                JsonValueKind.Array => e.EnumerateArray().Select(Decode).ToList(),
                _ => null,
            };
        }
        var tag = tagEl.GetString();
        e.TryGetProperty("v", out var v);
        switch (tag)
        {
            case "none": return null;
            case "bool": return v.GetBoolean();
            case "int": return BigInteger.Parse(v.ValueKind == JsonValueKind.String ? v.GetString()! : v.GetRawText());
            case "float":
                if (v.ValueKind == JsonValueKind.String)
                {
                    var s = v.GetString();
                    return s == "nan" ? double.NaN : s == "inf" ? double.PositiveInfinity : s == "-inf" ? double.NegativeInfinity : double.Parse(s!, System.Globalization.CultureInfo.InvariantCulture);
                }
                return v.GetDouble();
            case "text": case "str": return v.GetString();
            case "bytes": return Convert.FromBase64String(v.GetString()!);
            case "list": case "tuple": return v.EnumerateArray().Select(Decode).ToList();
            case "set": return new HashSet<object>(v.EnumerateArray().Select(Decode));
            case "dict":
                var d = new Dictionary<object, object>();
                foreach (var p in v.EnumerateArray()) { var kv = p.EnumerateArray().ToArray(); d[Decode(kv[0])!] = Decode(kv[1]); }
                return d;
        }
        throw new ArgumentException("cannot decode tag " + tag);
    }

    static object Natural(object v) => v switch
    {
        BigInteger b => b >= int.MinValue && b <= int.MaxValue ? (object)(int)b : b >= long.MinValue && b <= long.MaxValue ? (long)b : b,
        List<object> l => l.Select(Natural).ToList(),
        HashSet<object> s => new HashSet<object>(s.Select(Natural)),
        Dictionary<object, object> d => d.ToDictionary(kv => Natural(kv.Key)!, kv => Natural(kv.Value)),
        _ => v,
    };

    static object Conv(object v, Type t)
    {
        var u = Nullable.GetUnderlyingType(t);
        if (v == null) return (!t.IsValueType || u != null) ? null : Fail;
        if (u != null) t = u;
        if (t == typeof(object)) return Natural(v);
        if (v is BigInteger n)
        {
            try
            {
                if (t == typeof(int)) return (int)n;
                if (t == typeof(long)) return (long)n;
                if (t == typeof(short)) return (short)n;
                if (t == typeof(byte)) return (byte)n;
                if (t == typeof(sbyte)) return (sbyte)n;
                if (t == typeof(uint)) return (uint)n;
                if (t == typeof(ulong)) return (ulong)n;
                if (t == typeof(ushort)) return (ushort)n;
                if (t == typeof(double)) return (double)n;
                if (t == typeof(float)) return (float)n;
                if (t == typeof(decimal)) return (decimal)n;
                if (t == typeof(BigInteger)) return n;
            }
            catch (OverflowException) { return Fail; }
            return Fail;
        }
        if (v is double dd)
        {
            if (t == typeof(double)) return dd;
            if (t == typeof(float)) return (float)dd;
            if (t == typeof(decimal)) return (decimal)dd;
            return Fail;
        }
        if (v is bool) return t == typeof(bool) ? v : Fail;
        if (v is string s)
        {
            if (t == typeof(string)) return s;
            if (t == typeof(char) && s.Length == 1) return s[0];
            return Fail;
        }
        if (v is byte[] bs) return t == typeof(byte[]) ? bs : Fail;
        if (v is List<object> list)
        {
            if (t.IsArray)
            {
                var et = t.GetElementType()!;
                var arr = Array.CreateInstance(et, list.Count);
                for (int i = 0; i < list.Count; i++) { var x = Conv(list[i], et); if (x == Fail) return Fail; arr.SetValue(x, i); }
                return arr;
            }
            var elem = ElementType(t, typeof(List<>));
            if (elem == null) return Fail;
            var lt = typeof(List<>).MakeGenericType(elem);
            if (!t.IsAssignableFrom(lt)) return Fail;
            var outList = (IList)Activator.CreateInstance(lt)!;
            foreach (var x in list) { var y = Conv(x, elem); if (y == Fail) return Fail; outList.Add(y); }
            return outList;
        }
        if (v is HashSet<object> set)
        {
            var elem = ElementType(t, typeof(HashSet<>));
            if (elem == null) return Fail;
            var st = typeof(HashSet<>).MakeGenericType(elem);
            if (!t.IsAssignableFrom(st)) return Fail;
            var outSet = Activator.CreateInstance(st)!;
            var add = st.GetMethod("Add")!;
            foreach (var x in set) { var y = Conv(x, elem); if (y == Fail) return Fail; add.Invoke(outSet, new[] { y }); }
            return outSet;
        }
        if (v is Dictionary<object, object> dict)
        {
            Type kt = typeof(object), vt = typeof(object);
            if (t.IsGenericType && t.GetGenericArguments().Length == 2) { kt = t.GetGenericArguments()[0]; vt = t.GetGenericArguments()[1]; }
            var dt = typeof(Dictionary<,>).MakeGenericType(kt, vt);
            if (!t.IsAssignableFrom(dt)) return Fail;
            var od = (IDictionary)Activator.CreateInstance(dt)!;
            foreach (var kv in dict) { var k = Conv(kv.Key, kt); var x = Conv(kv.Value, vt); if (k == Fail || x == Fail) return Fail; od[k] = x; }
            return od;
        }
        return Fail;
    }

    static Type ElementType(Type t, Type concrete)
    {
        if (t == typeof(object)) return typeof(object);
        if (t.IsGenericType && t.GetGenericArguments().Length == 1) return t.GetGenericArguments()[0];
        if (t == typeof(IEnumerable) || t == typeof(ICollection) || t == typeof(IList)) return typeof(object);
        return null;
    }

    static int Cost(object v, Type t)
    {
        var u = Nullable.GetUnderlyingType(t) ?? t;
        if (v == null) return u == typeof(object) ? 2 : 0;
        if (v is BigInteger) return u == typeof(long) || u == typeof(int) ? 0 : u == typeof(BigInteger) || u == typeof(short) ? 1 : u == typeof(object) ? 3 : 5;
        if (v is double) return u == typeof(double) ? 0 : u == typeof(float) || u == typeof(decimal) ? 1 : u == typeof(object) ? 3 : 4;
        if (v is string) return u == typeof(string) ? 0 : u == typeof(char) ? 1 : 3;
        if (v is List<object>) return u.IsArray || (u.IsGenericType && u.GetGenericTypeDefinition() == typeof(List<>)) ? 0 : u == typeof(object) ? 3 : 1;
        return u == typeof(object) ? 3 : 0;
    }

    static object[] Bind(MethodBase m, List<object> args, out int cost)
    {
        cost = 0;
        var ps = m.GetParameters();
        bool varargs = ps.Length > 0 && ps[^1].GetCustomAttribute<ParamArrayAttribute>() != null;
        int fixedN = varargs ? ps.Length - 1 : ps.Length;
        int optional = ps.Count(p => p.IsOptional);
        if (args.Count < fixedN - optional || (!varargs && args.Count > ps.Length)) return null;
        var outArgs = new object[ps.Length];
        for (int i = 0; i < fixedN; i++)
        {
            if (i >= args.Count) { if (!ps[i].IsOptional) return null; outArgs[i] = ps[i].DefaultValue; continue; }
            var x = Conv(args[i], ps[i].ParameterType.IsByRef ? ps[i].ParameterType.GetElementType()! : ps[i].ParameterType);
            if (x == Fail) return null;
            cost += Cost(args[i], ps[i].ParameterType);
            outArgs[i] = x;
        }
        if (varargs)
        {
            var et = ps[^1].ParameterType.GetElementType()!;
            var rest = Array.CreateInstance(et, Math.Max(0, args.Count - fixedN));
            for (int i = fixedN; i < args.Count; i++) { var x = Conv(args[i], et); if (x == Fail) return null; rest.SetValue(x, i - fixedN); }
            outArgs[^1] = rest;
            cost += 10;
        }
        return outArgs;
    }

    static (T, object[]) Pick<T>(IEnumerable<T> cands, List<object> args, string what) where T : MethodBase
    {
        T best = null; object[] bestArgs = null; int bestCost = int.MaxValue;
        foreach (var m in cands.OrderBy(m => m.ToString()))
        {
            var a = Bind(m, args, out var c);
            if (a != null && c < bestCost) { best = m; bestArgs = a; bestCost = c; }
        }
        if (best == null)
            throw new Unresolved("no " + what + " accepts " + args.Count + " argument(s) of these types; found: " + string.Join("; ", cands.Select(m => m.ToString())));
        return (best, bestArgs);
    }

    static Type FindType(string name)
    {
        var all = Assembly.GetExecutingAssembly().GetTypes();
        var t = all.FirstOrDefault(x => x.FullName == name || x.FullName == name.Replace('$', '+'))
             ?? all.Where(x => x.Name == name.Split('.', '+', '$').Last()).OrderBy(x => x.FullName).FirstOrDefault();
        if (t == null) throw new Unresolved("type " + name + " not found");
        return t;
    }

    const BindingFlags All = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.FlattenHierarchy;

    static List<object> Args(JsonElement call, string key) =>
        call.TryGetProperty(key, out var a) && a.ValueKind == JsonValueKind.Array ? a.EnumerateArray().Select(Decode).ToList() : new List<object>();

    static string Prop(JsonElement call, string key) =>
        call.TryGetProperty(key, out var a) && a.ValueKind == JsonValueKind.String ? a.GetString() : null;

    public static int Main()
    {
        var lines = File.ReadAllLines(Environment.GetEnvironmentVariable("ACM_CASES")!).Where(l => l.Trim().Length > 0).ToArray();
        int idx = int.Parse(Environment.GetEnvironmentVariable("ACM_CASE")!);
        using var doc = JsonDocument.Parse(lines[idx]);
        var c = doc.RootElement;
        var call = c.GetProperty("call");
        var res = new List<string> { "\"case_id\": " + c.GetProperty("case_id").GetRawText() };
        var realOut = Console.Out;
        var buf = new StringWriter();
        object[] bound = null; object inst = null;
        var seen = new HashSet<object>(ReferenceEqualityComparer.Instance);
        Console.SetOut(buf);
        try
        {
            var kind = Prop(call, "kind") ?? "function";
            var t = FindType(Prop(call, "class") ?? throw new Unresolved("no class given"));
            var member = Prop(call, "member");
            object value;
            if (kind == "constructor")
            {
                var (ctor, a) = Pick(t.GetConstructors(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic), Args(call, "args"), "constructor of " + t.Name);
                bound = a;
                value = ((ConstructorInfo)ctor).Invoke(a);
            }
            else if (kind == "method" || kind == "property")
            {
                var (ctor, ia) = Pick(t.GetConstructors(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic), Args(call, "init"), "constructor of " + t.Name);
                inst = ((ConstructorInfo)ctor).Invoke(ia);
                var prop = kind == "property" ? t.GetProperty(member!, All | BindingFlags.Instance) : null;
                if (prop != null) { bound = Array.Empty<object>(); value = prop.GetValue(inst); }
                else
                {
                    var ms = t.GetMethods(All | BindingFlags.Instance | BindingFlags.Static).Where(m => m.Name == member).ToList();
                    if (ms.Count == 0) throw new Unresolved("no method " + member + " in " + t.Name);
                    var (m, a) = Pick(ms, kind == "property" ? new List<object>() : Args(call, "args"), "method " + t.Name + "." + member);
                    bound = a;
                    value = m.Invoke(m.IsStatic ? null : inst, a);
                }
            }
            else
            {
                var ms = t.GetMethods(All | BindingFlags.Static).Where(m => m.Name == member).ToList();
                if (ms.Count == 0)
                {
                    var p = t.GetProperty(member!, All | BindingFlags.Static);
                    if (p == null) throw new Unresolved("no static method " + member + " in " + t.Name);
                    bound = Array.Empty<object>();
                    value = p.GetValue(null);
                }
                else
                {
                    var (m, a) = Pick(ms, Args(call, "args"), "static method " + t.Name + "." + member);
                    bound = a;
                    value = m.Invoke(null, a);
                }
            }
            Console.SetOut(realOut);
            res.Add("\"status\": \"ok\"");
            res.Add("\"value\": " + Enc(value, 0, seen));
        }
        catch (Unresolved u)
        {
            Console.SetOut(realOut);
            res.Add("\"status\": \"unresolved\"");
            res.Add("\"message\": " + Q(u.Message));
        }
        catch (Exception ex)
        {
            Console.SetOut(realOut);
            var e = ex is TargetInvocationException tie && tie.InnerException != null ? tie.InnerException : ex;
            var hier = new List<string>();
            for (var k = e.GetType(); k != null && k != typeof(object); k = k.BaseType) hier.Add(Q(k.Name));
            res.Add("\"status\": \"exception\"");
            res.Add("\"exception\": {\"type\": " + Q(e.GetType().Name) + ", \"hierarchy\": [" + string.Join(", ", hier) + "], \"message\": " + Q(e.Message) + ", \"traceback\": " + Q((e.StackTrace ?? "").Length > 4000 ? e.StackTrace!.Substring(0, 4000) : e.StackTrace ?? "") + "}");
        }
        res.Add("\"args_after\": [" + string.Join(", ", (bound ?? Array.Empty<object>()).Select(x => Enc(x, 0, seen))) + "]");
        res.Add("\"self_after\": " + (inst != null ? Enc(inst, 0, seen) : "null"));
        res.Add("\"stdout\": " + Q(buf.ToString()));
        res.Add("\"stderr\": \"\"");
        realOut.WriteLine(Sentinel + "{" + string.Join(", ", res) + "}");
        return 0;
    }
}
