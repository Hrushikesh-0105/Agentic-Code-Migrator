// Encoder of the generated C++ test driver (sandbox/drivers.py): C++ values -> runner.py's type-tagged JSON.
// Overloads cover the standard library's value types; any other type is reported by its demangled name ("repr").
#pragma once
#include <cmath>
#include <cstdint>
#include <cxxabi.h>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <tuple>
#include <type_traits>
#include <typeinfo>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
#include <array>
#include <deque>
#include <list>

namespace acm {

inline std::string q(const std::string& s) {
    std::ostringstream o;
    o << '"';
    for (unsigned char c : s) {
        switch (c) {
            case '"': o << "\\\""; break;
            case '\\': o << "\\\\"; break;
            case '\n': o << "\\n"; break;
            case '\r': o << "\\r"; break;
            case '\t': o << "\\t"; break;
            default:
                if (c < 0x20) o << "\\u" << std::hex << std::setw(4) << std::setfill('0') << (int)c << std::dec;
                else o << c;  // UTF-8 bytes pass through
        }
    }
    o << '"';
    return o.str();
}

inline std::string demangle(const char* n) {
    int st = 0;
    char* d = abi::__cxa_demangle(n, nullptr, nullptr, &st);
    std::string r = (st == 0 && d) ? d : n;
    std::free(d);
    return r;
}

inline std::string tag(const std::string& t, const std::string& v) { return "{\"t\": \"" + t + "\", \"v\": " + v + "}"; }
inline std::string none() { return "{\"t\": \"none\"}"; }

inline std::string b64(const std::vector<unsigned char>& b) {
    static const char* T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string o;
    size_t i = 0;
    for (; i + 2 < b.size(); i += 3) {
        unsigned n = (b[i] << 16) | (b[i + 1] << 8) | b[i + 2];
        o += T[n >> 18]; o += T[(n >> 12) & 63]; o += T[(n >> 6) & 63]; o += T[n & 63];
    }
    if (i + 1 == b.size()) { unsigned n = b[i] << 16; o += T[n >> 18]; o += T[(n >> 12) & 63]; o += "=="; }
    else if (i + 2 == b.size()) { unsigned n = (b[i] << 16) | (b[i + 1] << 8); o += T[n >> 18]; o += T[(n >> 12) & 63]; o += T[(n >> 6) & 63]; o += '='; }
    return o;
}

template <typename T> std::string enc(const T& v);
// every overload is declared before any is defined, so nested containers find each other
template <typename T> std::string enc(const std::vector<T>& v);
template <typename T> std::string enc(const std::deque<T>& v);
template <typename T> std::string enc(const std::list<T>& v);
template <typename T, size_t N> std::string enc(const std::array<T, N>& v);
template <typename T> std::string enc(const std::set<T>& v);
template <typename T> std::string enc(const std::unordered_set<T>& v);
template <typename K, typename V> std::string enc(const std::map<K, V>& m);
template <typename K, typename V> std::string enc(const std::unordered_map<K, V>& m);
template <typename A, typename B> std::string enc(const std::pair<A, B>& p);
template <typename... A> std::string enc(const std::tuple<A...>& t);
template <typename T> std::string enc(const std::optional<T>& v);
template <typename T> std::string enc(const std::shared_ptr<T>& v);
template <typename T> std::string enc(const std::unique_ptr<T>& v);
inline std::string enc(const std::vector<unsigned char>& v);
inline std::string enc(const std::string& v);

inline std::string enc(bool v) { return std::string("{\"t\": \"bool\", \"v\": ") + (v ? "true" : "false") + "}"; }
inline std::string enc(char v) { return tag("text", q(std::string(1, v))); }
inline std::string enc(const std::string& v) { return tag("text", q(v)); }
inline std::string enc(std::string_view v) { return tag("text", q(std::string(v))); }
inline std::string enc(const char* v) { return v ? tag("text", q(v)) : none(); }
inline std::string enc(char* v) { return v ? tag("text", q(v)) : none(); }
inline std::string enc(std::nullptr_t) { return none(); }
inline std::string enc_float(double f) {
    if (std::isnan(f)) return tag("float", "\"nan\"");
    if (std::isinf(f)) return tag("float", f > 0 ? "\"inf\"" : "\"-inf\"");
    std::ostringstream o; o << std::setprecision(17) << f; std::string s = o.str();
    if (s.find_first_of(".eE") == std::string::npos) s += ".0";
    return tag("float", s);
}
inline std::string enc(float v) { return enc_float(v); }
inline std::string enc(double v) { return enc_float(v); }
inline std::string enc(long double v) { return enc_float((double)v); }
inline std::string enc(const std::vector<unsigned char>& v) { return tag("bytes", "\"" + b64(v) + "\""); }

template <typename T> std::string seq(const T& c, const char* t) {
    std::string o = "[";
    bool first = true;
    for (const auto& x : c) { if (!first) o += ", "; first = false; o += enc(x); }
    return tag(t, o + "]");
}
template <typename T> std::string enc(const std::vector<T>& v) { return seq(v, "list"); }
template <typename T> std::string enc(const std::deque<T>& v) { return seq(v, "list"); }
template <typename T> std::string enc(const std::list<T>& v) { return seq(v, "list"); }
template <typename T, size_t N> std::string enc(const std::array<T, N>& v) { return seq(v, "list"); }
template <typename T> std::string enc(const std::set<T>& v) { return seq(v, "set"); }
template <typename T> std::string enc(const std::unordered_set<T>& v) { return seq(v, "set"); }
template <typename M> std::string mapping(const M& m) {
    std::string o = "[";
    bool first = true;
    for (const auto& kv : m) { if (!first) o += ", "; first = false; o += "[" + enc(kv.first) + ", " + enc(kv.second) + "]"; }
    return tag("dict", o + "]");
}
template <typename K, typename V> std::string enc(const std::map<K, V>& m) { return mapping(m); }
template <typename K, typename V> std::string enc(const std::unordered_map<K, V>& m) { return mapping(m); }
template <typename A, typename B> std::string enc(const std::pair<A, B>& p) { return tag("tuple", "[" + enc(p.first) + ", " + enc(p.second) + "]"); }
template <typename... A> std::string enc(const std::tuple<A...>& t) {
    std::string o = "[";
    bool first = true;
    std::apply([&](const auto&... x) { ((o += (first ? "" : ", ") + enc(x), first = false), ...); }, t);
    return tag("tuple", o + "]");
}
template <typename T> std::string enc(const std::optional<T>& v) { return v ? enc(*v) : none(); }
template <typename T> std::string enc(const std::shared_ptr<T>& v) { return v ? enc(*v) : none(); }
template <typename T> std::string enc(const std::unique_ptr<T>& v) { return v ? enc(*v) : none(); }

template <typename T> std::string enc(const T& v) {
    if constexpr (std::is_same_v<T, bool>) return enc((bool)v);
    else if constexpr (std::is_integral_v<T>) {
        if constexpr (std::is_signed_v<T>) return tag("int", "\"" + std::to_string((long long)v) + "\"");
        else return tag("int", "\"" + std::to_string((unsigned long long)v) + "\"");
    } else if constexpr (std::is_enum_v<T>) return tag("int", "\"" + std::to_string((long long)v) + "\"");
    else if constexpr (std::is_pointer_v<T>) return v ? enc(*v) : none();
    else return tag("repr", q("<" + demangle(typeid(T).name()) + ">"));
}

inline void emit(const std::string& status, const std::string& value, const std::string& exc,
                 const std::vector<std::string>& after, const std::string& self_after) {
    std::cout.flush();
    std::string o = "__HARNESS_RESULT__{\"status\": " + q(status);
    if (!value.empty()) o += ", \"value\": " + value;
    if (!exc.empty()) o += ", \"exception\": " + exc;
    o += ", \"args_after\": [";
    for (size_t i = 0; i < after.size(); i++) { if (i) o += ", "; o += after[i]; }
    o += "]";
    o += ", \"self_after\": " + (self_after.empty() ? std::string("null") : self_after);
    o += "}";
    std::cout << "\n__ACMOUT_END__\n" << o << std::endl;
}

// Run the call; `after` and `self` are lambdas evaluated after it (so they show the state the call left).
// A template, so `if constexpr` discards the branch a void result cannot compile.
template <typename F, typename A, typename S> void run(F f, A after, S self) {
    if constexpr (std::is_void_v<decltype(f())>) {
        f();
        emit("ok", none(), "", after(), self());
    } else {
        auto v = f();
        emit("ok", enc(v), "", after(), self());
    }
}

inline std::string exc(const std::string& type, const std::string& message) {
    return "{\"type\": " + q(type) + ", \"hierarchy\": [" + q(type) + ", \"std::exception\"], \"message\": " + q(message) + "}";
}

}  // namespace acm
