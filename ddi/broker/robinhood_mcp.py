"""Headless client for Robinhood's official agentic-trading MCP server.

    Endpoint : https://agent.robinhood.com/mcp/trading  (streamable HTTP, OAuth sign-in)
    Account  : trades only in your dedicated "Robinhood MCP" account; reads all accounts.

STATUS: argument names checked against the live tool schemas (Nov 2026). Response shapes
are not parsed here; the agent workflow (.claude/skills/ddi-rebalance) builds the snapshot.

What the live schemas say
- Accounts are addressed by `account_number`. Numbers are sent as strings.
- `place_equity_order` / `review_equity_order` accept `tax_lots`: [{open_lot_id, quantity}]
  for specific-lot SELLS (max 30 lots, quantities must sum to the order quantity, not allowed
  with dollar_amount). Without it the broker uses FIFO.
- `place_equity_order` takes `ref_id`, an idempotency key: retries with the same ref_id can't
  place twice. Plans carry a deterministic ref_id per row.
- Fractional and dollar_amount orders: type=market, time_in_force=gfd, regular hours only.
- If trade approvals are on, a placed order goes to the Robinhood app for you to approve.

Safety design
- `execute()` defaults to review-only (calls review_equity_order, never place_equity_order).
- Refuses plans with guardrail errors or a sha256 mismatch.
- Keep Robinhood "Trade approvals" ON for the MCP account at first: then even a placed
  order still needs your tap in the Robinhood app.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import webbrowser
from contextlib import AsyncExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, urlparse

URL = "https://agent.robinhood.com/mcp/trading"

# Internal name -> live argument name (verified against the server's schemas, Nov 2026).
_ORDER = {"account": "account_number", "symbol": "symbol", "side": "side", "quantity": "quantity",
          "dollars": "dollar_amount", "type": "type", "time_in_force": "time_in_force",
          "market_hours": "market_hours", "tax_lots": "tax_lots"}
ARG_MAP: dict[str, dict[str, str]] = {
    "get_equity_tax_lots": {"account": "account_number", "symbol": "symbol"},
    "get_equity_positions": {"account": "account_number"},
    "get_equity_quotes": {"symbols": "symbols"},     # >20 symbols returns quotes but omits closes
    "get_equity_orders": {"account": "account_number"},
    "get_portfolio": {"account": "account_number"},
    "get_trade_approval_setting": {"account": "account_number"},
    "review_equity_order": _ORDER,
    "place_equity_order": {**_ORDER, "ref_id": "ref_id"},
}


class _FileTokens:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        d = self._read().get("tokens")
        return OAuthToken(**d) if d else None

    async def set_tokens(self, tokens) -> None:
        d = self._read(); d["tokens"] = tokens.model_dump(mode="json")
        self.path.write_text(json.dumps(d)); self.path.chmod(0o600)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        d = self._read().get("client")
        return OAuthClientInformationFull(**d) if d else None

    async def set_client_info(self, info) -> None:
        d = self._read(); d["client"] = info.model_dump(mode="json")
        self.path.write_text(json.dumps(d)); self.path.chmod(0o600)


def _wait_for_callback(port: int) -> tuple[str, str | None]:
    box: dict[str, Any] = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            box["code"] = q.get("code", [None])[0]
            box["state"] = q.get("state", [None])[0]
            self.send_response(200); self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", port), H)
    srv.handle_request()
    return box["code"], box["state"]


class RobinhoodMCP:
    def __init__(self, token_file: Path, port: int = 8765, url: str = URL):
        self.url, self.token_file, self.port = url, token_file, port
        self._stack = AsyncExitStack()
        self.session = None

    async def __aenter__(self):
        from mcp import ClientSession
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

        async def redirect(url: str):
            print(f"Opening browser for Robinhood sign-in:\n  {url}")
            webbrowser.open(url)

        async def callback():
            code, state = await asyncio.to_thread(_wait_for_callback, self.port)
            return AuthorizationCodeResult(code=code, state=state)

        auth = OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata(
                client_name="diy-direct-index", redirect_uris=[f"http://127.0.0.1:{self.port}/callback"],
                grant_types=["authorization_code", "refresh_token"], response_types=["code"]),
            storage=_FileTokens(self.token_file), redirect_handler=redirect, callback_handler=callback)
        http = await self._stack.enter_async_context(create_mcp_http_client(auth=auth))
        streams = await self._stack.enter_async_context(streamable_http_client(self.url, http_client=http))
        read, write = streams[0], streams[1]
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc):
        await self._stack.aclose()

    async def tools(self) -> list[dict]:
        res = await self.session.list_tools()
        return [{"name": t.name, "description": t.description, "input_schema": t.inputSchema} for t in res.tools]

    async def call(self, tool: str, **kw) -> Any:
        m = ARG_MAP.get(tool, {})
        args = {m.get(k, k): v for k, v in kw.items() if v is not None}
        res = await self.session.call_tool(tool, args)
        if res.isError:
            raise RuntimeError(f"{tool} failed: {res.content}")
        if getattr(res, "structuredContent", None):
            return res.structuredContent
        txt = "".join(getattr(c, "text", "") for c in res.content)
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            return txt

    # ------------------------------------------------------------ execution
    async def execute(self, plan: dict, *, place: bool = False) -> list[dict]:
        body = json.dumps(plan["orders"], sort_keys=True).encode()
        if hashlib.sha256(body).hexdigest() != plan["sha256"]:
            raise RuntimeError("plan was modified after it was generated")
        if plan["guardrail_errors"]:
            raise RuntimeError(f"guardrails: {plan['guardrail_errors']}")
        out = []
        for o in plan["orders"]:
            common = dict(account=plan["account_id"], symbol=o["symbol"], side=o["side"], type="market",
                          time_in_force="gfd", market_hours="regular_hours")
            if o["side"] == "sell":
                size = {"quantity": f"{o['quantity']:.6f}", "tax_lots": o.get("tax_lots")}
            else:
                size = {"dollars": f"{o['dollars']:.2f}"}
            review = await self.call("review_equity_order", **common, **size)
            rec = {"order": o, "review": review}
            body = review.get("data", review) if isinstance(review, dict) else {}
            checks = body.get("order_checks")          # {} when the broker has no alerts
            if place and checks:
                rec["skipped"] = "review returned order_checks; not placing"
            elif place:
                rec["placed"] = await self.call("place_equity_order", **common, **size, ref_id=o["ref_id"])
            out.append(rec)
        return out
