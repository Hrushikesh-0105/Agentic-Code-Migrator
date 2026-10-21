"""An optional LLM for the prose in Layer 1's reports. It never makes a decision.

Scheduling, commits, retries, escalation and blocking stay deterministic. The LLM only writes text a person reads:
  run summary        the opening section of each report.md
  escalation brief   per escalated unit: what failed, the likely cause, what the reviewer can do. Shown in the
                     --hitl prompt, saved as units/<unit>/brief.json and repeated in report.md
Every text is checked against the facts it was written from (`grounded`): each number must occur in the facts,
and each `code span` naming the project must quote them. A text that fails is retried once with the problems
listed, then replaced by a deterministic template; the report says which happened. Without an LLM the templates
are used.

Providers (keys from the environment or the repo's .env, which is git-ignored; keys are never logged):
  groq    Groq's OpenAI-compatible API, GROQ_API_KEY, model ACM_GROQ_MODEL or ACM_LLM_MODEL (default
          llama-3.1-8b-instant, the project's choice; when the key cannot use it, the first working fallback)
  gemini  Google's Gemini API, GEMINI_API_KEY, model ACM_GEMINI_MODEL (default gemini-3.5-flash-lite)
`auto` chains every provider that has a key, in the order ACM_LLM_PROVIDERS (default groq,gemini; see
layer1/README.md for how it was chosen). Each provider is probed once with a tiny request; one that cannot generate
(billing, retired model) is reported and skipped. If a call fails later (outage, quota), the next provider writes
the text. Every call (prompt, answer, usage) is appended to <repo>/llm/calls.jsonl. To compare models on a real
run: python -m layer1.llm_bench.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDER_ORDER = ("groq", "gemini")
FATAL = ("HTTP 401", "HTTP 402", "HTTP 403")  # the key or its billing: no model of that provider will work


class LLMError(Exception):
    pass


def load_env(path=REPO_ROOT / ".env"):
    """KEY=VALUE lines of .env into os.environ (values already set in the environment win)."""
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


class ChatLLM:
    """One provider: resolves its model once, then answers chat(system, user) with retries on 429 / 5xx."""
    provider, key_env, model_envs, default_model, fallback_models = "", "", (), "", ()

    def __init__(self, api_key, model=None, timeout=90, retries=4):
        self._key = api_key
        self.requested = model or next((os.environ[e] for e in self.model_envs if os.environ.get(e)),
                                       self.default_model)
        self.timeout, self.retries = timeout, retries
        self.model, self.substitution = None, None

    def _headers(self):
        raise NotImplementedError

    def _http(self, url, body=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={**self._headers(), "User-Agent": "acm-layer1/1.0",
                                              "Content-Type": "application/json"})
        for i in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read()[:600].decode("utf-8", "replace")
                daily = "PerDay" in detail or "per day" in detail.lower()
                if e.code not in (429, 500, 502, 503, 504) or daily or i == self.retries:
                    try:
                        msg = json.loads(detail)["error"]["message"]
                    except (ValueError, KeyError, TypeError):
                        msg = detail
                    raise LLMError(f"{self.provider} HTTP {e.code}: {' '.join(str(msg).split())[:240]}") from None
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', detail)
                wait = float(e.headers.get("retry-after") or (m.group(1) if m else 2 ** i))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if i == self.retries:
                    raise LLMError(f"cannot reach {self.provider}: {e}") from None
                wait = 2 ** i
            time.sleep(min(wait, 60))

    def _list_models(self):
        raise NotImplementedError

    def _complete(self, model, system, user, max_tokens):
        """-> (text, usage, request body as logged)"""
        raise NotImplementedError

    def resolve(self):
        """Pick the model once: the requested one if this key can generate with it, else the first fallback that
        can. Each candidate is probed with a tiny request, because a listed model may still be retired or unpaid."""
        if self.model is None:
            ids, tried = set(self._list_models()), []
            for m in [self.requested] + [x for x in self.fallback_models if x != self.requested]:
                if m not in ids:
                    tried.append(f"{m}: not listed")
                    continue
                try:
                    self._complete(m, "Answer with one word.", "Say OK.", 16)
                except LLMError as e:
                    if f"{self.provider} HTTP" not in str(e):  # it answered, just not usefully: the model works
                        self.model = m
                        break
                    if any(code in str(e) for code in FATAL):
                        raise
                    tried.append(f"{m}: {str(e)[:120]}")
                    continue
                self.model = m
                break
            if self.model is None:
                raise LLMError(f"{self.provider}: no usable model ({'; '.join(tried)})")
            if self.model != self.requested:
                self.substitution = f"{self.requested} is not usable with this {self.provider} key; used {self.model}"
        return self.model

    def describe(self):
        return {"provider": self.provider, "requested_model": self.requested, "model": self.model,
                "substitution": self.substitution}

    def chat(self, system, user, max_tokens=700, log=None, purpose=""):
        model = self.resolve()
        start, entry = time.time(), {"purpose": purpose, "provider": self.provider, "model": model}
        try:
            text, usage, body = self._complete(model, system, user, max_tokens)
            text = clean(text).strip()
            entry.update(request=body, response=text, usage=usage, seconds=round(time.time() - start, 2))
            return {"text": text, "model": model, "provider": self.provider, "usage": usage,
                    "seconds": entry["seconds"]}
        except LLMError as e:
            entry.update(error=str(e), seconds=round(time.time() - start, 2))
            raise
        finally:
            if log:
                Path(log).parent.mkdir(parents=True, exist_ok=True)
                with open(log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")


class GroqLLM(ChatLLM):
    provider, key_env, model_envs = "groq", "GROQ_API_KEY", ("ACM_GROQ_MODEL", "ACM_LLM_MODEL")
    default_model = "llama-3.1-8b-instant"
    fallback_models = ("openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b")
    API = "https://api.groq.com/openai/v1"

    def _headers(self):
        return {"Authorization": "Bearer " + self._key}

    def _list_models(self):
        return [m["id"] for m in self._http(self.API + "/models")["data"]]

    def _complete(self, model, system, user, max_tokens):
        body = {"model": model, "temperature": 0, "seed": 7, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if model.startswith("openai/gpt-oss"):  # reasoning models: keep only the answer
            body.update(reasoning_effort="low", include_reasoning=False)
        elif "qwen3" in model:
            body.update(reasoning_format="hidden")
        d = self._http(self.API + "/chat/completions", body)
        return d["choices"][0]["message"].get("content") or "", d.get("usage", {}), body


class GeminiLLM(ChatLLM):
    provider, key_env, model_envs = "gemini", "GEMINI_API_KEY", ("ACM_GEMINI_MODEL",)
    default_model = "gemini-3.5-flash-lite"
    fallback_models = ("gemini-3.1-flash-lite", "gemini-3.8-flash")
    API = "https://generativelanguage.googleapis.com/v1beta"

    def _headers(self):
        return {"x-goog-api-key": self._key}

    def _list_models(self):
        return [m["name"].split("/", 1)[1] for m in self._http(self.API + "/models?pageSize=200")["models"]
                if "generateContent" in m.get("supportedGenerationMethods", [])]

    @staticmethod
    def _thinking(model):
        """Report prose needs no long reasoning: keep the model's thinking to its minimum."""
        if model.startswith("gemini-2.5"):
            return {"thinkingBudget": 0 if "pro" not in model else 128}
        if model.startswith("gemini-"):
            return {"thinkingLevel": "minimal" if "pro" not in model else "low"}
        return None

    def _complete(self, model, system, user, max_tokens):
        cfg = {"temperature": 0, "seed": 7, "maxOutputTokens": max_tokens + 1500}  # headroom for thinking tokens
        if self._thinking(model):
            cfg["thinkingConfig"] = self._thinking(model)
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": cfg}
        d = self._http(f"{self.API}/models/{model}:generateContent", body)
        cand = (d.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []) if not p.get("thought"))
        if not text.strip():
            raise LLMError(f"gemini {model}: empty answer (finishReason {cand.get('finishReason')}, "
                           f"block {d.get('promptFeedback', {}).get('blockReason')})")
        return text, d.get("usageMetadata", {}), body


PROVIDERS = {"gemini": GeminiLLM, "groq": GroqLLM}


class FallbackLLM:
    """Several providers in order: a call that fails on one (outage, quota) is answered by the next. A provider
    whose key or billing fails (HTTP 401/402/403) is not asked again in this process."""

    def __init__(self, llms):
        self.order, self.llms, self.used, self.failed = list(llms), list(llms), llms[0], []

    @property
    def provider(self):
        return self.used.provider

    @property
    def model(self):
        return self.used.model

    @property
    def substitution(self):
        notes = [f"{x.provider} failed ({e})" for x, e in self.failed]
        if self.used.substitution:
            notes.append(self.used.substitution)
        return "; ".join(notes) or None

    def describe(self):
        return [x.describe() for x in self.order]

    def chat(self, system, user, **kw):
        errors, self.failed = [], []
        for x in list(self.llms):
            try:
                r = x.chat(system, user, **kw)
                self.used = x
                return r
            except LLMError as e:
                errors.append(str(e))
                self.failed.append((x, str(e)[:80]))
                if any(code in str(e) for code in FATAL) and len(self.llms) > 1:
                    self.llms.remove(x)
        raise LLMError(" | ".join(errors))


def from_env(mode="auto", model=None):
    """mode: off | auto (every provider with a key, in PROVIDER_ORDER) | gemini | groq (that one, required).
    `model` overrides the first provider's model. Returns (llm or None, note)."""
    if mode == "off":
        return None, "off"
    load_env()
    order = [p.strip() for p in os.environ.get("ACM_LLM_PROVIDERS", ",".join(PROVIDER_ORDER)).split(",")
             if p.strip() in PROVIDERS]
    names = [p for p in order if os.environ.get(PROVIDERS[p].key_env)] if mode == "auto" else [mode]
    llms, notes = [], []
    for i, name in enumerate(names):
        cls = PROVIDERS[name]
        key = os.environ.get(cls.key_env)
        if not key:
            raise LLMError(f"{cls.key_env} is not set (environment or .env)")
        llm = cls(key, model if i == 0 else None)
        try:
            llm.resolve()
        except LLMError as e:
            if mode != "auto":
                raise
            notes.append(f"{name} skipped: {str(e)[:160]}")
            continue
        llms.append(llm)
    if not llms:
        return None, "off (" + ("; ".join(notes) or "no GEMINI_API_KEY or GROQ_API_KEY") + ")"
    desc = " -> ".join(f"{x.provider} {x.model}" + (f" ({x.substitution})" if x.substitution else "") for x in llms)
    return (llms[0] if len(llms) == 1 else FallbackLLM(llms)), desc + ("; " + "; ".join(notes) if notes else "")


# ------------------------------------------------------------------------------------------- grounding guard
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
_CODE = re.compile(r"`([^`\n]+)`")


_ASCII = str.maketrans({" ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
                        "‐": "-", "‑": "-", "‒": "-", "–": "-"})


def clean(text):
    """Models write narrow no-break spaces and non-breaking hyphens ("javac (OpenJDK 17)"); use plain ones."""
    return text.translate(_ASCII)


def _norm(n):
    return str(float(n)).rstrip("0").rstrip(".")


def grounded(text, facts_text, max_words, roots=None):
    """Problems that make `text` untrustworthy as a statement of `facts_text`. With `roots` (the project's
    top-level names), only code spans naming the project (`root.x`, `root::x`) or a unit (`step-0012`) must quote
    the facts, so a brief may suggest target-language APIs; without, every code span must."""
    problems = []
    if not text.strip():
        return ["empty answer"]
    allowed = {_norm(n) for n in re.findall(r"\d+(?:\.\d+)?", facts_text)}
    for span in _CODE.findall(text):
        about_project = roots is None or re.match(r"step-\d+$", span.strip()) or \
            any(span.strip() == r or span.strip().startswith((r + ".", r + "::")) for r in roots)
        if about_project and span.strip() not in facts_text:
            problems.append(f"`{span}` does not appear in the facts")
    for n in _NUM.findall(_CODE.sub(" ", text)):
        if _norm(n) not in allowed:
            problems.append(f"the number {n} does not appear in the facts")
    words = len(text.split())
    if words > max_words:
        problems.append(f"{words} words, the limit is {max_words}")
    if text.lstrip().startswith("#"):
        problems.append("starts with a heading")
    return problems


def write(llm, system, facts, template, max_words, log=None, purpose="", roots=None):
    """Grounded LLM text for `facts`, else `template`. Returns {text, source, model, attempts, problems, note}."""
    facts_text = json.dumps(facts, indent=1, ensure_ascii=False)
    if llm is None:
        return {"text": template, "source": "template", "model": None, "attempts": 0, "problems": [],
                "note": "no LLM configured"}
    user, problems = f"FACTS (JSON):\n{facts_text}", []
    for attempt in (1, 2):
        try:
            r = llm.chat(system, user, log=log, purpose=purpose)
        except LLMError as e:
            return {"text": template, "source": "template", "model": getattr(llm, "model", None), "attempts": attempt,
                    "problems": [str(e)], "note": f"LLM call failed: {e}"}
        r["text"] = clean(r["text"]).strip()
        problems = grounded(r["text"], facts_text, max_words, roots)
        if not problems:
            return {"text": r["text"], "source": "llm", "provider": r.get("provider"), "model": r["model"], "attempts": attempt, "problems": [],
                    "note": getattr(llm, "substitution", None)}
        user = (f"FACTS (JSON):\n{facts_text}\n\nYour previous answer was rejected:\n- " + "\n- ".join(problems) +
                f"\n\nPrevious answer:\n{r['text']}\n\nWrite it again, fixing these problems.")
    return {"text": template, "source": "template", "model": getattr(llm, "model", None), "attempts": 2,
            "problems": problems, "note": "LLM text failed the grounding check twice; template used"}


# ------------------------------------------------------------------------------------------- run summary
SUMMARY_SYSTEM = """You write the opening summary of a code-migration run report for software engineers.
Rules:
- Use ONLY the facts in the JSON. Do not guess, do not add advice the facts do not support.
- Every number you write must appear in the facts, written as digits. Do not compute new numbers: percentages are given.
- Write unit ids, fragment ids, file names and check names in backticks, copied exactly from the facts.
- No headings. 80 to 180 words: one paragraph on the outcome, then 1 to 4 bullet points ("- ") on what needs
  attention (escalated units and how many units they block, failed checks, build errors). If nothing needs attention, say so.
- If "layer2" is "dummy", say that the translations are placeholders from a dummy Layer 2 and no behaviour was tested.
- If "layer2_evidence" is given, say how many migrated units were verified by differential tests and how many only
  compiled, with the numbers given."""


def run_facts(state, validation, build, layer2, evidence=None):
    res, units = state["result"], state["units"]
    total = len(units)
    nonvend = sum(u["status"] != "vendored" for u in units) or 1
    blocked_by_root = {}
    for u in units:
        for r in u.get("root_causes", []):
            blocked_by_root[r] = blocked_by_root.get(r, 0) + 1
    c = res["counters"]
    return {
        "repo": state["repo"], "regime": state["migration"]["regime"], "layer2": layer2,
        "assembly_mode": state["assembly_mode"], "max_attempts_per_unit": state["max_attempts"],
        "human_in_the_loop": state["hitl"], "units_total": total, "units_by_status": res["units_by_status"],
        "percent_of_units_by_status": {k: round(100 * v / total) for k, v in res["units_by_status"].items()},
        "percent_of_non_vendored_units_by_status": {k: round(100 * v / nonvend) for k, v in res["units_by_status"].items()
                                                    if k != "vendored"},
        "layer2_calls": c["invocations"], "retries": c["retries"], "invalid_verdicts": c["invalid_verdicts"],
        "human_decisions": {k: c[k] for k in ("human_accepted", "human_retries", "human_skipped") if c[k]},
        "contract_checks_passed": f"{sum(x['ok'] for x in validation)}/{len(validation)}",
        "failed_contract_checks": [x["check"] for x in validation if not x["ok"]],
        "build": {"compiles": build["ok"], "tool": build["tool"], "files": build["files"],
                  "first_errors": build["errors"][:3]} if build else "not run",
        "migrated_files": len(res["assembly"]["files"]), "assembly_problems": res["assembly"]["problems"][:3],
        "escalated_units": [{"unit": u["unit"], "fragments": u["fragments"], "reason": u["escalation"]["reason"],
                             "attempts": u["attempts"], "last_reason": u["escalation"]["last_reason"],
                             "units_blocked_because_of_it": sum(blocked_by_root.get(f, 0) for f in u["fragments"])}
                            for u in units if u["status"] == "escalated"],
        "units_migrated_after_retries": [{"unit": u["unit"], "fragments": u["fragments"], "attempts": u["attempts"]}
                                         for u in units if u["status"] == "migrated" and u["attempts"] > 1][:10],
        "units_migrated_by_a_human": [u["unit"] for u in units if u.get("by") == "human"],
        **({"layer2_evidence": {k: v for k, v in evidence.items() if k != "rows" and k != "llm_calls"}}
           if evidence else {}),
    }


def run_template(f):
    st = ", ".join(f"{v} {k}" for k, v in f["units_by_status"].items())
    b = f["build"]
    lines = [f"Layer 1 processed {f['units_total']} units of `{f['repo']}` ({f['regime']}): {st}. "
             f"Layer 2 was called {f['layer2_calls']} times, with {f['retries']} retries; the run contract passed "
             f"{f['contract_checks_passed']} checks" +
             (f" and the migrated codebase {'compiles' if b['compiles'] else 'does not compile'} "
              f"({b['tool']}, {b['files']} files)." if isinstance(b, dict) else ".")]
    if f["layer2"] == "dummy":
        lines[0] += " The translations are placeholders from the dummy Layer 2; no behaviour was tested."
    if f.get("layer2_evidence"):
        e = f["layer2_evidence"]
        lines[0] += (f" Layer 2 verified {e['units_verified_by_tests']} migrated units with differential tests "
                     f"({e['tests_equivalent']}/{e['differential_tests']} tests equivalent); "
                     f"{e['units_compiled_only']} were only compile-checked.")
    att = [f"- `{e['unit']}` escalated ({e['reason']}, {e['attempts']} attempts); "
           f"{e['units_blocked_because_of_it']} units blocked because of it." for e in f["escalated_units"]]
    att += [f"- failed check: `{x}`" for x in f["failed_contract_checks"]]
    if isinstance(b, dict) and not b["compiles"]:
        att.append(f"- build errors, e.g. `{b['first_errors'][0]}`" if b["first_errors"] else "- the build failed")
    return lines[0] + "\n\n" + ("\n".join(att) if att else "- Nothing needs attention.")


def run_summary(llm, state, validation, build, layer2, log=None, evidence=None):
    f = run_facts(state, validation, build, layer2, evidence)
    return write(llm, SUMMARY_SYSTEM, f, run_template(f), 220, log, "run summary")


# ------------------------------------------------------------------------------------------- escalation brief
BRIEF_SYSTEM = """You help a human reviewer decide on a unit of code that an automated migration could not migrate.
Rules:
- Use ONLY the facts in the JSON: the unit's source code, its hazards, and the outcome of each attempt.
- Every number you write must appear in the facts. Put code, identifiers and file names in backticks, copied exactly.
- No headings. At most 150 words, as three bullet points ("- "):
  - what failed (from the attempts' outcomes and reasons),
  - the most likely cause (from the reasons, hazards and code; say "unclear" if the facts do not show it),
  - what the reviewer can do: accept a hand-written translation, retry with guidance (suggest the guidance in one
    sentence), or skip (say how many units directly depend on it and would be blocked)."""


def brief_facts(case, fragments, attempts, n_dependents, migration):
    return {"unit": case["unit"], "regime": migration["regime"], "escalation_reason": case["reason"],
            "attempts": attempts, "units_that_directly_depend_on_it": n_dependents,
            "fragments": [{"id": f["id"], "kind": f["kind"], "file": f["file"],
                           "source": "\n".join(f["source"].splitlines()[:60]),
                           "hazards": [h["rule"] for h in f["hazards"]],
                           "features": sorted(f["features"])} for f in fragments]}


def brief_template(f):
    last = f["attempts"][-1] if f["attempts"] else {}
    return (f"- {len(f['attempts'])} attempt(s) failed; the last ended `{last.get('outcome')}`: "
            f"{last.get('reason') or 'no reason given'}.\n"
            f"- Escalated as `{f['escalation_reason']}`; the cause is not determined automatically.\n"
            f"- Accept a hand-written translation, retry with guidance, or skip "
            f"({f['units_that_directly_depend_on_it']} units depend on it directly and would be blocked).")


def escalation_brief(llm, case, fragments, attempts, n_dependents, migration, log=None):
    f = brief_facts(case, fragments, attempts, n_dependents, migration)
    roots = sorted({x["id"].split("::")[0].split(".")[0] for x in fragments})
    return write(llm, BRIEF_SYSTEM, f, brief_template(f), 170, log, f"escalation brief {case['unit']}", roots)
