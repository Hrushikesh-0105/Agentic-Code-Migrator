"""What every LLM agent shares: a bounded tool-calling loop over MCP tools, JSON answers, and prompt budgeting."""
import json

from layer2.llm import BadOutput, parse_json
from layer2.mcp_client import ToolCallError
from layer2.prompts import get_pair

TOOL_RESULT_CHARS = 3000


class Agent:
    name = ""
    max_tokens = 2500

    def __init__(self, llm, hub, skeleton, bundle):
        self.llm, self.hub, self.sk, self.bundle = llm, hub, skeleton, bundle
        self.frag = {f["id"]: f for f in bundle["fragments"]}
        mig = bundle["manifest"]["migration"]
        self.source_language, self.target_language = mig["source_language"], mig["target_language"]
        self.java = self.target_language == "java"
        self.py2 = self.source_language == "python2"
        self.pair = get_pair(bundle)  # the two langs/ profiles and the pair's feature rules
        self.source, self.target = self.pair.source, self.pair.target
        self.python_source = self.source.id in ("python2", "python3")
        self.splice = self.pair.same_family()  # Python -> Python: fragments rewritten in place

    def budget(self, max_tokens=None, system=None):
        """Characters the user prompt may use: the per-minute token limit, minus the answer and the system prompt
        (its own length when given: the language-pair prompts vary; else 900 tokens)."""
        reserve = len(system) / 2.8 + 120 if system else 900
        return int((self.llm.pacer.tpm - (max_tokens or self.max_tokens) - reserve) * 3.0)

    def ask(self, system, user, tools=None, rounds=0, max_tokens=None, effort="low", purpose=""):
        """-> (parsed JSON answer, meta {model, calls, tool_calls}). Tools are `<server>__<tool>` names from
        MCPHub.openai_tools; the model may call them for up to `rounds` turns, then must answer."""
        max_tokens = max_tokens or self.max_tokens
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        meta = {"agent": self.name, "calls": 0, "tool_calls": [], "models": []}
        retried = False
        turn = 0
        while True:
            offer = tools if tools and turn < rounds else None
            if not offer and any(m.get("tool_calls") for m in messages):
                # the answer turn: tool results as plain text and no tools offered (models asked to answer with
                # tools still listed sometimes call one anyway)
                messages = flatten(messages)
                messages.append({"role": "user", "content": "Now answer with only the JSON object."})
            try:
                r = self.llm.chat(self.name, messages, tools=offer, json_mode=not offer, max_tokens=max_tokens,
                                  effort=effort, purpose=purpose)
            except BadOutput as e:
                if retried:
                    raise
                retried = True
                messages = flatten(messages)
                messages.append({"role": "user", "content": f"Your previous answer was rejected ({str(e)[:300]}). "
                                                            "Do not call tools. Answer with only the JSON object."})
                tools, rounds = None, 0
                continue
            meta["calls"] += 1
            meta["models"].append(r["model"])
            if r["tool_calls"] and offer:
                messages.append({"role": "assistant", "content": r["content"] or "", "tool_calls": r["tool_calls"]})
                for tc in r["tool_calls"][:4]:
                    name = tc["function"]["name"]
                    server, _, tool = name.partition("__")
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                        result = json.dumps(self.hub.call(server, tool, args, who=self.name))
                    except (ToolCallError, ValueError) as e:
                        result = f"ERROR: {e}"
                    meta["tool_calls"].append({"tool": name, "arguments": tc["function"].get("arguments"),
                                               "result_chars": len(result)})
                    if len(result) > TOOL_RESULT_CHARS:
                        result = result[:TOOL_RESULT_CHARS] + f"... [truncated, {len(result)} chars]"
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                for tc in r["tool_calls"][4:]:
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "ERROR: at most 4 calls per turn"})
                turn += 1
                continue
            try:
                return parse_json(r["content"]), meta
            except BadOutput as e:
                if retried:
                    raise
                retried = True
                messages = flatten(messages)
                messages.append({"role": "assistant", "content": r["content"][:4000]})
                messages.append({"role": "user", "content": f"That was not a valid JSON object ({e}). Answer again "
                                                            "with only the JSON object, nothing else."})
                tools, rounds = None, 0


def flatten(messages):
    """The conversation without tool-call turns: their results become one plain user message, so it can be sent
    again without offering tools (a model that keeps calling tools gets no tools)."""
    out, results = [], []
    names = {}
    for m in messages:
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                names[tc["id"]] = f"{tc['function']['name']}({tc['function'].get('arguments', '')})"
            continue
        if m.get("role") == "tool":
            results.append(f"{names.get(m['tool_call_id'], 'tool')} ->\n{m['content']}")
            continue
        out.append(m)
    if results:
        out.append({"role": "user", "content": "Results of the tools you called:\n\n" + "\n\n".join(results)})
    return out


def clip(text, limit):
    """Keep the head of `text` within `limit` characters, saying what was cut."""
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 60)] + f"\n... [cut: {len(text) - limit + 60} more characters]"


def fit(sections, limit):
    """Join (title, text, priority) sections within `limit` characters; low priority (high number) sections are
    shortened first, then dropped."""
    sections = [s for s in sections if s[1]]
    total = sum(len(t) + len(h) + 2 for h, t, _ in sections)
    order = sorted(range(len(sections)), key=lambda i: -sections[i][2])
    out = [list(s) for s in sections]
    for i in order:
        if total <= limit:
            break
        h, t, _ = out[i]
        keep = max(0, len(t) - (total - limit))
        new = clip(t, keep) if keep > 200 else ""
        total -= len(t) - len(new)
        out[i][1] = new
    return "\n\n".join(f"{h}\n{t}" if h else t for h, t, _ in out if t)
