// Prelude of the generated Rust test driver (sandbox/drivers.py writes it as tests/acm_driver.rs with one fn per
// case). Results are printed as JSON whose values are the {:?} text of the Rust values; the host parses that text.
#![allow(unused, unused_mut, non_snake_case, unused_imports, clippy::all)]
use std::collections::*;
use std::io::Write;

// autoref specialisation: Debug text when the type implements Debug, else its type name
struct AcmW<'a, T: ?Sized>(&'a T);
trait AcmViaDebug { fn acm_s(&self) -> String; }
impl<'a, T: std::fmt::Debug + ?Sized> AcmViaDebug for AcmW<'a, T> { fn acm_s(&self) -> String { format!("{:?}", self.0) } }
trait AcmNoDebug { fn acm_s(&self) -> String; }
impl<'a, T: ?Sized> AcmNoDebug for &AcmW<'a, T> { fn acm_s(&self) -> String { format!("<{}>", std::any::type_name::<T>()) } }
macro_rules! acm_dbg { ($e:expr) => { (&AcmW(&$e)).acm_s() } }

fn acm_q(s: &str) -> String {
    let mut o = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn acm_emit(status: &str, value: Option<String>, msg: Option<String>, after: Vec<String>, selfv: Option<String>) {
    let _ = std::io::stdout().flush();
    let mut o = String::from("__ACMRUST__{");
    o.push_str(&format!("\"status\": {}", acm_q(status)));
    if let Some(v) = value { o.push_str(&format!(", \"debug\": {}", acm_q(&v))); }
    if let Some(m) = msg { o.push_str(&format!(", \"message\": {}", acm_q(&m))); }
    o.push_str(", \"args_after\": [");
    for (i, a) in after.iter().enumerate() {
        if i > 0 { o.push_str(", "); }
        o.push_str(&acm_q(a));
    }
    o.push(']');
    if let Some(s) = selfv { o.push_str(&format!(", \"self_after\": {}", acm_q(&s))); }
    o.push('}');
    println!("\n__ACMOUT_END__\n{}", o);
}

fn acm_panic(p: Box<dyn std::any::Any + Send>) -> String {
    if let Some(s) = p.downcast_ref::<&str>() { s.to_string() }
    else if let Some(s) = p.downcast_ref::<String>() { s.clone() }
    else { String::from("panic") }
}
