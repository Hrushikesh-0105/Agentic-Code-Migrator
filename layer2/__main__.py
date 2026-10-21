"""Layer 2 on its own.

python -m layer2 --verify     known-answer checks: Layer 0 fixture -> Layer 1 -> the agents (scripted LLM) through
                              the real MCP servers and Docker sandbox; quota stop + resume; comparator; MCP roots
                              (Python 2 -> Java)
python -m layer2 --verify-pairs [SRC->TGT ...]
                              the same, for the other 12 language pairs of RepoTransBench (one small fixture per
                              source language; planted bugs caught and repaired)
To migrate a repository with the agents, run Layer 1: python -m layer1 --layer2 agents [--scope ID] [REPO ...]
"""
import argparse
import sys

from sandbox.logs import new_run


def main():
    ap = argparse.ArgumentParser(prog="python -m layer2", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--verify", action="store_true", help="run the known-answer checks")
    ap.add_argument("--verify-pairs", nargs="*", metavar="SRC->TGT",
                    help="known-answer checks across language pairs (default: all 12)")
    a = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    if a.verify_pairs is not None:
        from layer1.__main__ import Console
        from .verify_pairs import run_pairs
        pairs = [tuple(p.split("->")) for p in a.verify_pairs] or None
        run = new_run("layer2", "verify-pairs")
        sys.exit(0 if run_pairs(run, Console(run), pairs) else 1)
    if not a.verify:
        ap.print_help()
        return
    from layer1.__main__ import Console
    from .verify import run_verify
    run = new_run("layer2", "verify")
    sys.exit(0 if run_verify(run, Console(run)) else 1)


if __name__ == "__main__":
    main()
