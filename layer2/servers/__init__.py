"""Layer 2's MCP servers (Model Context Protocol, stdio transport). Agents reach tools only through them:

  acm-analysis   analysis.py   read-only queries over the Layer 0 bundle: fragments, dependencies, types, branch
                               conditions, hazards, the target skeleton (Code Analysis Tools)
  acm-workspace  workspace.py  the migrated project under construction: committed translations, candidate trees,
                               file reads and Java outlines (File System)
  acm-sandbox    sandbox.py    the Docker sandbox: Python and Java test cases, behavioural comparison, javac,
                               project test suites (Docker Sandbox, Test Frameworks)

Each is a separate process (`python -m layer2.servers.<name> ...`) started by layer2/mcp_client.py; every call is
logged by the client (Logging & Reporting). A server only touches paths under the roots it was started with.
"""
import functools
import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


def make_server(name, instructions, log_file=None):
    if log_file:  # stdout is the protocol channel; keep stderr (tracebacks, warnings) in a file
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(log_file, "a", encoding="utf-8", buffering=1)
    return MCPServer(name, instructions=instructions, version="1.0", log_level="WARNING")


def tool(server):
    """@tool(server): register a tool. Its result is sent as one JSON text block (a list would otherwise become
    one block per item), and its exceptions reach the caller as their message (ToolError)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return json.dumps(fn(*a, **kw), default=str)
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"{type(e).__name__}: {e}") from e
        wrapper.__annotations__ = {**fn.__annotations__, "return": str}
        server.add_tool(wrapper, name=fn.__name__, description=(fn.__doc__ or "").strip(), structured_output=False)
        return fn
    return deco


class Roots:
    """The directories a server may read or write."""

    def __init__(self, roots):
        self.roots = [Path(r).resolve() for r in roots if r]

    def check(self, path):
        p = Path(path).resolve()
        if not any(p == r or r in p.parents for r in self.roots):
            raise ToolError(f"path outside the allowed roots: {path}")
        return p
