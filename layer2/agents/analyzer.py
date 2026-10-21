"""Analyzer agent (lightweight LLM): inspects the unit's fragments, their parameters and upstream dependencies, and
writes a structured migration contract: source and target types, I/O boundaries, exceptions / errors, side effects
and migration hazards (str vs bytes, integer division and overflow, pointers, overloading, ...). Its prompt is
built for the language pair from langs/ (layer2/prompts.py). It may query Layer 0 through the acm-analysis MCP
server. The contract is kept for the unit's later attempts."""
from layer2.prompts import HAZARD_KINDS, analyzer_system

from .base import Agent, fit
from .context import dependencies, facts, numbered_source

TOOLS = ["get_fragment", "get_dependencies", "find_fragments", "get_module_header", "get_class_members",
         "get_expected_api"]


class Analyzer(Agent):
    name = "analyzer"
    max_tokens = 2200

    def run(self, job):
        cached = job.load_store("contract.json")
        if cached:
            return cached, {"cached": True}
        system = analyzer_system(self.pair, job.req["migration"]["regime"])
        sections = [("UNIT", f"{job.unit}: {', '.join(job.ids)}" + (" (a dependency cycle, migrated together)"
                                                                     if job.req["cycle"] else ""), 0)]
        for f in job.fragments:
            sections.append((f"FRAGMENT {f['id']} ({f['kind']}, {f['file']})\nTARGET: {self.sk.describe(f['id'])}",
                             numbered_source(f), 1))
            sections.append(("LAYER 0 FACTS", facts(f), 2))
        sections.append(("DEPENDENCIES (already migrated; target location and translated API)",
                         dependencies(job, self.sk, self.target.id, 500, self.source.name), 3))
        user = fit(sections, self.budget(system=system) - 3500)  # room for tool results
        tools = self.hub.openai_tools("analysis", TOOLS)
        answer, meta = self.ask(system, user, tools=tools, rounds=2, purpose=f"contract {job.unit}")
        contract = self._normalise(answer, job)
        job.save_store("contract.json", contract)
        return contract, meta

    def _normalise(self, answer, job):
        got = {x.get("id"): x for x in (answer.get("fragments") or []) if isinstance(x, dict)}
        out = []
        for f in job.fragments:
            c = got.get(f["id"]) or {"id": f["id"], "purpose": "", "missing": "the Analyzer gave no contract"}
            c["hazards"] = [h for h in c.get("hazards") or [] if isinstance(h, dict)]
            out.append(c)
        return {"fragments": out}


def contract_for(contract, fid):
    return next((c for c in contract["fragments"] if c.get("id") == fid), {})


__all__ = ["Analyzer", "contract_for", "HAZARD_KINDS"]
