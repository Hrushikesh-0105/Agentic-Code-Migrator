"""The agents' LLM access: Groq chat completions (OpenAI-compatible), with JSON answers and MCP tool calls.

Each agent has a chain of models; the first is its intended tier (proposal: a lightweight model for analysis and
verdict diagnosis, a heavy open-weight coding model for translation and repair), the rest are used when that
model's daily quota is spent. Override a chain with ACM_L2_MODEL_<AGENT> (comma-separated), e.g.
ACM_L2_MODEL_TRANSLATOR=openai/gpt-oss-120b,qwen/qwen3.8-27b.

Groq's free tier allows each model ACM_L2_TPM tokens per minute (8000 for this key) and a request's prompt plus
max_tokens must fit in it, so calls are paced per model and prompts are kept small. When every model of a chain
is out of daily quota the run stops with QuotaExhausted (a BaseException: Layer 1 does not catch it), and is
continued later with `python -m layer1 --resume`.
Every call (agent, model, messages, answer, tool calls, usage, seconds) is appended to the given log.
"""
import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

from layer1.llm import GroqLLM, LLMError, clean, load_env

CHAINS = {
    "analyzer": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
    "testgen": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "translator": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
    "verdict": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
    "repair": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
}
CHARS_PER_TOKEN = 3.2  # conservative for code-heavy prompts


class QuotaExhausted(BaseException):
    """Every model an agent may use is out of daily quota: stop the run (resumable), do not fail units."""


class PromptTooLarge(Exception):
    pass


class BadOutput(Exception):
    """The model's answer could not be used (invalid JSON, invalid tool call)."""


def estimate(messages, max_tokens):
    return int(sum(len(json.dumps(m)) for m in messages) / CHARS_PER_TOKEN) + max_tokens


class Pacer:
    """Per-model sliding one-minute window of tokens, so calls wait instead of hitting HTTP 429."""

    def __init__(self, tpm):
        self.tpm, self.used, self.lock = tpm, {}, threading.Lock()

    def wait(self, model, tokens):
        while True:
            with self.lock:
                q = self.used.setdefault(model, deque())
                now = time.time()
                while q and now - q[0][0] > 60:
                    q.popleft()
                if sum(t for _, t in q) + tokens <= self.tpm or not q:
                    q.append((now, tokens))
                    return
                sleep = 60 - (now - q[0][0]) + 0.5
            time.sleep(min(max(sleep, 0.5), 61))

    def settle(self, model, estimated, actual):
        """Replace the estimate by the tokens the provider counted."""
        with self.lock:
            q = self.used.get(model) or deque()
            for i in range(len(q) - 1, -1, -1):
                if q[i][1] == estimated:
                    q[i] = (q[i][0], actual)
                    break


def _wait_hint(msg):
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", msg)
    if not m or not any(m.groups()):
        return 3600
    h, mi, s = (float(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


class AgentLLM:
    """Shared by all agents of a Layer 2 instance. chat() -> {content, tool_calls, model, usage, seconds}."""
    provider = "groq"

    def __init__(self, api_key=None, tpm=None, log=None, chains=None):
        load_env()
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise LLMError("GROQ_API_KEY is not set (environment or .env)")
        self._groq = GroqLLM(key, timeout=180, retries=6)
        self.pacer = Pacer(tpm or int(os.environ.get("ACM_L2_TPM", "8000")))
        self.log = Path(log) if log else None
        self.chains = {a: (os.environ.get(f"ACM_L2_MODEL_{a.upper()}") or ",".join(c)).split(",")
                       for a, c in (chains or CHAINS).items()}
        self.exhausted = {}  # model -> time it may be used again
        self.max_wait = float(os.environ.get("ACM_L2_MAX_QUOTA_WAIT", "900"))
        self.usage = {}

    def describe(self):
        return {"provider": "groq", "chains": self.chains, "tokens_per_minute_per_model": self.pacer.tpm}

    def _body(self, model, messages, tools, json_mode, max_tokens, effort, tool_choice="auto"):
        body = {"model": model, "messages": messages, "temperature": 0, "seed": 7, "max_tokens": max_tokens}
        if model.startswith("openai/gpt-oss"):
            body.update(reasoning_effort=effort, include_reasoning=False)
        elif "qwen3" in model:
            body.update(reasoning_format="hidden", reasoning_effort="none" if effort == "low" else "default")
        if tools:  # tool_choice "none": the history has tool calls, but this turn must answer
            body.update(tools=tools, tool_choice=tool_choice)
        elif json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def chat(self, agent, messages, tools=None, json_mode=True, max_tokens=2500, effort="low", purpose="",
             tool_choice="auto"):
        chain = self.chains[agent]
        while True:
            now = time.time()
            usable = [m for m in chain if self.exhausted.get(m, 0) <= now]
            if usable:
                break
            wait = min(self.exhausted[m] for m in chain) - now
            if wait > self.max_wait:
                raise QuotaExhausted(f"{agent}: every model is out of daily quota ({', '.join(chain)}); "
                                     f"the earliest is usable again in {wait / 60:.0f} min")
            time.sleep(wait + 1)
        model = usable[0]
        est = estimate(messages, max_tokens)
        if est > self.pacer.tpm:
            raise PromptTooLarge(f"~{est} tokens (prompt + max_tokens) exceed the {self.pacer.tpm}/min limit")
        body = self._body(model, messages, tools, json_mode, max_tokens, effort, tool_choice)
        self.pacer.wait(model, est)
        start, entry = time.time(), {"agent": agent, "purpose": purpose, "model": model, "messages": messages,
                                     "tools": [t["function"]["name"] for t in tools or []], "json_mode": json_mode}
        try:
            d = self._groq._http(GroqLLM.API + "/chat/completions", body)
        except LLMError as e:
            msg = str(e)
            entry.update(error=msg, seconds=round(time.time() - start, 2))
            self._write(entry)
            if "per day" in msg.lower() or "(TPD)" in msg or "(RPD)" in msg:
                self.exhausted[model] = time.time() + _wait_hint(msg)
                return self.chat(agent, messages, tools, json_mode, max_tokens, effort, purpose, tool_choice)
            if "HTTP 413" in msg or "too large" in msg.lower():
                raise PromptTooLarge(msg) from None
            if "HTTP 400" in msg and any(x in msg.lower() for x in (
                    "tool_use_failed", "json_validate_failed", "failed to call a function", "valid json",
                    "failed to validate json", "failed_generation",
                    "tool call validation failed", "not in request.tools", "output_parse_failed",
                    "parsing failed", "could not be parsed", "tool choice is none", "but model called a tool")):
                raise BadOutput(msg) from None
            raise
        msg = d["choices"][0]["message"]
        usage = d.get("usage", {})
        self.pacer.settle(model, est, int(usage.get("total_tokens") or est))
        u = self.usage.setdefault(agent, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        u["calls"] += 1
        u["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        u["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        out = {"content": clean(msg.get("content") or ""), "tool_calls": msg.get("tool_calls") or [],
               "model": model, "usage": usage, "seconds": round(time.time() - start, 2),
               "finish_reason": d["choices"][0].get("finish_reason")}
        entry.update(response=out["content"], tool_calls=out["tool_calls"], usage=usage, seconds=out["seconds"],
                     finish_reason=out["finish_reason"])
        self._write(entry)
        return out

    def _write(self, entry):
        if self.log:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_json(text):
    """The first JSON object in a model's answer (code fences and prose around it are ignored)."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    start = text.find("{")
    if start < 0:
        raise BadOutput("no JSON object in the answer")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError as e:
                    raise BadOutput(f"invalid JSON: {e}") from None
    raise BadOutput("unterminated JSON object")
