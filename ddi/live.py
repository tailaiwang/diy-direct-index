"""Live loop: broker snapshot -> deterministic plan -> (human/agent executes) -> fills.

The engine never talks to the broker. Something else (a Claude Code agent using the
Robinhood MCP tools, or the headless MCP client in ddi.broker.robinhood_mcp) produces a
*snapshot* JSON, and executes the *plan* JSON this module writes. That keeps the money-moving
part small, auditable and replaceable.

snapshot.json
{
  "as_of": "2026-11-25",
  "account_id": "<MCP agentic account id>",
  "cash": 1234.56,                                  # buying power usable for buys
  "tax_lots": [{"symbol": "AAPL", "quantity": 0.5, "cost_per_share": 180.1,
                "acquired": "2026-01-02", "lot_id": "abc"}],   # lot_id = open_lot_id from get_equity_tax_lots
  "quotes": {"AAPL": 231.2, ...},                   # last trade / mid for every index name
  "recent_orders": [{"symbol": "AAPL", "side": "buy", "quantity": 1.0, "price": 230.0,
                     "date": "2026-11-20", "account_id": "<any RH account>"}],
  "external_buys": [{"symbol": "MSFT", "date": "2026-11-10", "source": "Wealthfront"}]
}
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

import math

import pandas as pd

from .engine import Config, Engine, Order, Replacement, State
from .lots import Ledger, Lot, Realized, WASH_DAYS


# ------------------------------------------------------------------ guardrails
class Guardrails:
    def __init__(self, max_order: float = 5_000.0, max_turnover: float = 0.30,
                 allow_initial_deploy: bool = True, max_missing_weight: float = 0.02):
        self.max_missing_weight = max_missing_weight
        self.max_order = max_order
        self.max_turnover = max_turnover          # of portfolio value per run
        self.allow_initial_deploy = allow_initial_deploy

    def check(self, orders: list[Order], prices: pd.Series, value: float, cash: float,
              universe: set[str]) -> list[str]:
        errs = []
        notional = 0.0
        initial = value > 0 and cash / value > 0.95
        for o in orders:
            n = o.dollars if o.side == "buy" else o.qty * float(prices.get(o.sym, 0.0))
            notional += n
            if o.side == "buy" and o.sym not in universe:
                errs.append(f"{o.sym}: buy outside index/sleeve universe")
            if n > self.max_order and not (initial and self.allow_initial_deploy):
                errs.append(f"{o.sym}: order ${n:,.0f} > max_order ${self.max_order:,.0f}")
        buys = sum(o.dollars for o in orders if o.side == "buy")
        if buys > cash + 0.01:
            errs.append(f"buys ${buys:,.2f} exceed cash ${cash:,.2f}")
        if value > 0 and notional / value > self.max_turnover and not (initial and self.allow_initial_deploy):
            errs.append(f"turnover {notional / value:.0%} > {self.max_turnover:.0%} cap")
        return errs


# ------------------------------------------------------------------ state io
def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def build_state(snapshot: dict, saved: dict, own_account: str | None = None) -> State:
    """Ledger from broker tax lots; wash-sale memory from saved state + order history."""
    L = Ledger()
    for i, t in enumerate(snapshot.get("tax_lots", [])):
        lot = Lot(float(t["quantity"]), float(t["cost_per_share"]),
                  date.fromisoformat(t["acquired"][:10]), id=i)
        L.lots.setdefault(t["symbol"], []).append(lot)
        L._next_id = i + 1
    today = date.fromisoformat(snapshot["as_of"])
    for o in snapshot.get("recent_orders", []):
        od = date.fromisoformat(o["date"][:10])
        if o["side"] != "buy" or (today - od).days > WASH_DAYS:
            continue
        if own_account and o.get("account_id") not in (None, own_account):
            L.external_buys.setdefault(o["symbol"], []).append(od)   # main RH account etc.
        else:
            L.buys.setdefault(o["symbol"], []).append((od, float(o["quantity"])))
    for e in snapshot.get("external_buys", []):
        L.external_buys.setdefault(e["symbol"], []).append(date.fromisoformat(e["date"][:10]))
    for r in saved.get("loss_sales", []):
        sd = date.fromisoformat(r["sold"])
        if (today - sd).days <= WASH_DAYS:
            L.loss_sales.setdefault(r["sym"], []).append(
                Realized(r["sym"], sd, date.fromisoformat(r["acquired"]), r["qty"], r["proceeds"], r["basis"], "harvest"))
    st = State(ledger=L, cash=float(snapshot["cash"]))
    for s, r in saved.get("replacements", {}).items():
        st.replacements[s] = Replacement(date.fromisoformat(r["until"]), r["weights"])
    if saved.get("base_units"):
        st.base_units = pd.Series(saved["base_units"], dtype=float)
        st.last_rebuild = date.fromisoformat(saved["last_rebuild"])
        st.last_universe = frozenset(saved.get("last_universe", []))
    if saved.get("last_rebalance"):
        st.last_rebalance = date.fromisoformat(saved["last_rebalance"])
    return st


def save_state(path: Path, st: State, extra_loss_sales: list[dict] | None = None, prev: dict | None = None) -> None:
    prev = prev or {}
    today_cut = (date.today() - timedelta(days=WASH_DAYS + 1)).isoformat()
    ls = [r for r in prev.get("loss_sales", []) if r["sold"] >= today_cut] + (extra_loss_sales or [])
    out = {
        "replacements": {s: {"until": r.until.isoformat(), "weights": r.weights} for s, r in st.replacements.items()},
        "base_units": None if st.base_units is None else st.base_units.to_dict(),
        "last_rebuild": st.last_rebuild.isoformat() if st.last_rebuild else None,
        "last_universe": sorted(st.last_universe),
        "last_rebalance": st.last_rebalance.isoformat() if st.last_rebalance else None,
        "loss_sales": ls,
    }
    path.write_text(json.dumps(out, indent=1))


# ------------------------------------------------------------------ plan
MAX_LOTS_PER_ORDER = 30   # Robinhood place_equity_order: tax_lots takes at most 30 lots


def _floor6(x: float) -> float:
    return math.floor(x * 1e6 + 1e-6) / 1e6


def _sell_rows(o: Order, snapshot: dict, prices: pd.Series) -> list[dict]:
    """One plan row per broker order. Specific-lot sells carry Robinhood's tax_lots payload
    ({open_lot_id, quantity}, quantities summing to the order quantity) and are split into
    chunks of at most 30 lots."""
    base = {"symbol": o.sym, "side": "sell", "reason": o.reason, "est_price": round(float(prices.get(o.sym, 0.0)), 4)}
    if not o.lot_ids:
        return [{**base, "quantity": _floor6(o.qty)}]
    lots = snapshot["tax_lots"]
    alloc, left = [], o.qty
    for i in o.lot_ids:                       # same consumption order as Ledger.sell(mode="specific")
        if left <= 1e-9:
            break
        q = _floor6(min(float(lots[i]["quantity"]), left))
        left -= q
        if q > 0:
            alloc.append((i, q))
    rows = []
    for k in range(0, len(alloc), MAX_LOTS_PER_ORDER):
        part = alloc[k:k + MAX_LOTS_PER_ORDER]
        rows.append({**base, "quantity": round(sum(q for _, q in part), 6),
                     "lot_ids_internal": [i for i, _ in part],
                     "tax_lots": [{"open_lot_id": lots[i].get("lot_id") or lots[i].get("open_lot_id"),
                                   "quantity": f"{q:.6f}"} for i, q in part]})
    return rows


def _ref_id(as_of: str, phase: str, i: int, row: dict) -> str:
    """Deterministic idempotency key: re-running the same plan re-sends the same ref_ids, so
    the broker cannot place an order twice."""
    size = row.get("quantity", row.get("dollars"))
    return "ddi-" + hashlib.sha256(f"{as_of}|{phase}|{i}|{row['symbol']}|{row['side']}|{size}".encode()).hexdigest()[:24]


def make_plan(snapshot: dict, bench_w: pd.Series, cfg: Config, state_path: Path, phase: str,
              guard: Guardrails, hist_returns: pd.DataFrame | None = None,
              own_account: str | None = None) -> dict:
    saved = load_state(state_path)
    st = build_state(snapshot, saved, own_account or snapshot.get("account_id"))
    prices = pd.Series(snapshot["quotes"], dtype=float)
    today = date.fromisoformat(snapshot["as_of"])
    missing = [s for s in bench_w.index if s not in prices.index]
    missing_w = float(bench_w.reindex(missing).sum()) if missing else 0.0
    if missing:
        bench_w = bench_w.drop(index=missing)
        bench_w = bench_w / bench_w.sum()
    eng = Engine(cfg)
    if phase == "sells":
        orders = eng.plan_sells(today, prices, bench_w, st, hist_returns)
    else:
        orders = eng.plan_buys(today, prices, bench_w, st, force=True)
    value = st.value(prices)
    universe = set(bench_w.index) | ({cfg.sleeve} if cfg.sleeve else set())
    errs = guard.check(orders, prices, value, st.cash, universe)
    if missing_w > guard.max_missing_weight:
        errs.append(f"quotes missing for {len(missing)} names = {missing_w:.1%} of index weight "
                    f"(> {guard.max_missing_weight:.0%}); refusing to plan on a partial snapshot")
    save_state(state_path, st, prev=saved)
    rows = []
    for o in orders:
        if o.side == "sell":
            rows += _sell_rows(o, snapshot, prices)
        else:
            rows.append({"symbol": o.sym, "side": "buy", "dollars": round(o.dollars, 2), "reason": o.reason,
                         "est_price": round(float(prices.get(o.sym, 0.0)), 4)})
    for i, r in enumerate(rows):
        r["ref_id"] = _ref_id(snapshot["as_of"], phase, i, r)
        if r["side"] == "sell" and "tax_lots" in r and any(t["open_lot_id"] is None for t in r["tax_lots"]):
            errs.append(f"{r['symbol']}: specific-lot sell but the snapshot has no open_lot_id for a lot")
    body = {
        "as_of": snapshot["as_of"], "account_id": snapshot.get("account_id"), "phase": phase,
        "portfolio_value": round(value, 2), "cash": round(st.cash, 2),
        "missing_quotes": missing,
        "orders": rows,
        "guardrail_errors": errs,
        "config": {k: (sorted(v) if isinstance(v, frozenset) else v) for k, v in asdict(cfg).items()},
    }
    body["sha256"] = hashlib.sha256(json.dumps(body["orders"], sort_keys=True).encode()).hexdigest()
    return body


def record_fills(plan: dict, fills: list[dict], snapshot: dict, state_path: Path) -> list[dict]:
    """After execution: remember loss sales (for the 30-day no-rebuy rule).
    fills: [{"symbol", "side", "quantity", "avg_price", "date"}]"""
    saved = load_state(state_path)
    lots = {}
    for t in snapshot.get("tax_lots", []):
        lots.setdefault(t["symbol"], []).append(t)
    new = []
    for f in fills:
        if f["side"] != "sell":
            continue
        basis_ps = None
        ids = [i for o in plan["orders"] if o["symbol"] == f["symbol"] and o["side"] == "sell"
               for i in o.get("lot_ids_internal", [])]
        if ids:
            ls = [snapshot["tax_lots"][i] for i in ids]
        else:
            ls = sorted(lots.get(f["symbol"], []), key=lambda t: -float(t["cost_per_share"]))
        if ls:
            q = sum(float(t["quantity"]) for t in ls)
            basis_ps = sum(float(t["quantity"]) * float(t["cost_per_share"]) for t in ls) / q
        if basis_ps is not None and float(f["avg_price"]) < basis_ps:
            new.append({"sym": f["symbol"], "sold": f["date"][:10], "acquired": ls[0]["acquired"][:10],
                        "qty": float(f["quantity"]), "proceeds": float(f["quantity"]) * float(f["avg_price"]),
                        "basis": float(f["quantity"]) * basis_ps})
    saved["loss_sales"] = saved.get("loss_sales", []) + new
    state_path.write_text(json.dumps(saved, indent=1))
    return new
