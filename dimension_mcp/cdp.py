"""Minimal Chrome DevTools Protocol client for Electron apps (Dimension's renderer).

Dimension ships an old Chromium: the protocol ignores awaitPromise, and the page
has no async/await. So every call here is synchronous JS that returns a JSON
string; long work is started in the page and polled.
"""
from __future__ import annotations

import json
import urllib.request

import websocket


class CDPError(RuntimeError):
    """The page threw, or the protocol returned an error."""


class CDPTimeout(RuntimeError):
    """The page did not answer in time (often a modal dialog is blocking it)."""


class CDPUnavailable(RuntimeError):
    """Nothing is listening on the debugging port."""


class CDP:
    def __init__(self, port: int, page_suffix: str = "main.html"):
        self.port = port
        self.page_suffix = page_suffix
        self._msg_id = 0

    def targets(self) -> list[dict]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
                return json.load(r)
        except Exception as e:  # connection refused, timeout ...
            raise CDPUnavailable(f"no DevTools endpoint on port {self.port}: {e}") from e

    def available(self) -> bool:
        try:
            return any(t.get("url", "").endswith(self.page_suffix) for t in self.targets())
        except CDPUnavailable:
            return False

    def _ws_url(self) -> str:
        for t in self.targets():
            if t.get("type") == "page" and t.get("url", "").endswith(self.page_suffix):
                return t["webSocketDebuggerUrl"]
        raise CDPUnavailable(f"port {self.port} is open but the {self.page_suffix} page is not loaded yet")

    def send(self, method: str, params: dict | None = None, timeout: float = 15) -> dict:
        try:
            ws = websocket.create_connection(self._ws_url(), timeout=timeout, suppress_origin=True)
        except CDPUnavailable:
            raise
        except Exception as e:
            raise CDPUnavailable(f"could not open DevTools websocket: {e}") from e
        try:
            self._msg_id += 1
            mid = self._msg_id
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException as e:
                    raise CDPTimeout(f"{method} got no answer within {timeout:.0f}s") from e
                if msg.get("id") == mid:
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if "error" in msg:
            raise CDPError(json.dumps(msg["error"]))
        return msg.get("result", {})

    def eval_json(self, expression: str, timeout: float = 15):
        """Evaluate JS that returns a JSON string; return the parsed value."""
        res = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=timeout)
        if "exceptionDetails" in res:
            d = res["exceptionDetails"]
            text = d.get("exception", {}).get("description") or d.get("text")
            raise CDPError(f"page exception: {text}")
        val = res.get("result", {}).get("value")
        if isinstance(val, str):
            try:
                return json.loads(val)
            except ValueError:
                return val
        return val
