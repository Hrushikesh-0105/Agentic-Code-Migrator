"""Layer 2: multi-agent translation and verification (Analyzer, Test-Generation, Translator, Executor & Sandbox,
Verdict, Repair), reached by Layer 1 once per attempt at a unit:

    class Layer2(Protocol):
        name: str
        def process(self, request: dict) -> dict: ...

`request` is a layer1.protocol.TranslationRequest and the return value must be a layer1.protocol.Verdict, both
as plain JSON-compatible dicts (JSON schemas: layer1/schemas/). Layer 1 validates every verdict before using it.

  agentic.AgentLayer2   the agents, their MCP servers (servers/) and the Docker sandbox; LLMs on Groq (llm.py)
  dummy.DummyLayer2     passes every unit without translating; scriptable failures, for testing Layer 1
See layer2/README.md.
"""
from typing import Protocol


class Layer2(Protocol):
    name: str

    def process(self, request: dict) -> dict: ...
