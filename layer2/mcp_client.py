"""The agents' side of MCP: starts Layer 2's servers (layer2/servers/) as stdio subprocesses with the official
MCP Python SDK, and offers a synchronous call(server, tool, arguments).

The SDK is async; one background thread runs an event loop that holds every client session. Every call is logged
(server, tool, arguments, result size or error, seconds) to the hub's log. Tool results are JSON (the servers
return dicts and lists); a tool error raises ToolCallError with the server's message.
"""
import asyncio
import json
import sys
import threading
import time
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import Client, StdioServerParameters

REPO = Path(__file__).resolve().parent.parent


class ToolCallError(Exception):
    pass


class MCPHub:
    def __init__(self, servers, log=None, timeout=900):
        """servers: {name: [args after `python -m`]}, e.g. {"analysis": ["layer2.servers.analysis", "--bundle", d]}."""
        self.servers, self.log, self.timeout = servers, Path(log) if log else None, timeout
        self.tools, self.info, self.calls = {}, {}, 0
        self._loop, self._ready, self._error = None, threading.Event(), None
        self._thread = threading.Thread(target=self._run, name="mcp-hub", daemon=True)
        self._thread.start()
        if not self._ready.wait(180):
            raise RuntimeError("MCP servers did not start within 180 s")
        if self._error:
            raise RuntimeError(f"MCP servers failed to start: {self._error}")

    def _run(self):
        try:
            asyncio.run(self._main())
        except BaseException as e:  # noqa: BLE001
            self._error = self._error or repr(e)
            self._ready.set()

    async def _main(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with AsyncExitStack() as stack:
            self._clients = {}
            try:
                for name, args in self.servers.items():
                    c = Client(StdioServerParameters(command=sys.executable, args=["-m", *args], cwd=str(REPO)),
                               read_timeout_seconds=self.timeout)
                    self._clients[name] = await stack.enter_async_context(c)
                    listed = await self._clients[name].list_tools()
                    self.tools[name] = [{"name": t.name, "description": t.description or "",
                                         "input_schema": t.input_schema} for t in listed.tools]
                    info = self._clients[name].server_info
                    self.info[name] = {"name": info.name if info else name, "protocol": self._clients[name].protocol_version,
                                       "tools": [t["name"] for t in self.tools[name]]}
            except BaseException as e:  # noqa: BLE001
                self._error = f"{type(e).__name__}: {e}"
                self._ready.set()
                raise
            self._ready.set()
            await self._stop.wait()

    async def _call(self, server, tool, arguments):
        return await self._clients[server].call_tool(tool, arguments)

    def call(self, server, tool, arguments=None, who=""):
        """Call a tool and return its JSON result."""
        if server not in self.tools:
            raise ToolCallError(f"no MCP server {server!r}")
        start = time.time()
        entry = {"server": server, "tool": tool, "by": who, "arguments": _short(arguments or {})}
        self.calls += 1
        try:
            fut = asyncio.run_coroutine_threadsafe(self._call(server, tool, arguments or {}), self._loop)
            r = fut.result(self.timeout + 30)
            text = "".join(getattr(c, "text", "") for c in r.content)
            if r.is_error:
                raise ToolCallError(text or f"{server}.{tool} failed")
            try:
                value = json.loads(text) if text else None
            except ValueError:
                value = text
            entry.update(ok=True, result_chars=len(text))
            return value
        except ToolCallError as e:
            entry.update(ok=False, error=str(e)[:500])
            raise
        except Exception as e:  # noqa: BLE001
            entry.update(ok=False, error=f"{type(e).__name__}: {e}"[:500])
            raise ToolCallError(f"{server}.{tool}: {type(e).__name__}: {e}") from e
        finally:
            entry["seconds"] = round(time.time() - start, 3)
            if self.log:
                self.log.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, default=str) + "\n")

    def openai_tools(self, server, names=None):
        """A server's tools in the OpenAI function-calling format, named `<server>__<tool>`."""
        return [{"type": "function", "function": {"name": f"{server}__{t['name']}", "description": t["description"][:900],
                                                  "parameters": t["input_schema"]}}
                for t in self.tools[server] if names is None or t["name"] in names]

    def close(self):
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
            self._thread.join(30)


def _short(v, limit=400):
    s = json.dumps(v, default=str)
    return v if len(s) <= limit else s[:limit] + f"... ({len(s)} chars)"
