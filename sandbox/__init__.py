"""Layer 0 execution sandbox: Dockerized Py2.7 / Py3 / Java17 runners plus a differential comparator."""
from .compare import compare_results
from .executor import differential, run_cases, run_suite

__all__ = ["compare_results", "differential", "run_cases", "run_suite"]
