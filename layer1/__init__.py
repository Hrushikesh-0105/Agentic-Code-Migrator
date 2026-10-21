"""Layer 1: deterministic orchestration (LangGraph). Schedules Layer 0's fragments bottom-up, sends each unit to
Layer 2, decides on its verdict (commit / retry / escalate to a human) and assembles the migrated codebase.
See layer1/README.md.

protocol.py      the Layer 1 <-> Layer 2 request / verdict contract, and Layer 1's check of a verdict
orchestrator.py  the state machine and its driver (migrate)
assemble.py      translations -> migrated codebase (splice for Python targets, compose for Java)
contract.py      re-checks a finished run from its files
build_check.py   compiles the migrated codebase in the sandbox
llm.py           LLM-written report prose (run summary, escalation briefs), checked against the facts;
                 providers Groq and Gemini.  llm_bench.py  compares models on the real report tasks
report.py        human-readable report;  verify.py  known-answer scenarios;  __main__.py  CLI
"""
