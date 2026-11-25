import json
from pathlib import Path

import pandas as pd

from ddi.engine import Config
from ddi.live import Guardrails, make_plan, record_fills


def _snap(cash, lots=(), quotes=None, orders=(), as_of="2026-11-25"):
    syms = [f"S{i}" for i in range(10)]
    q = quotes or {s: 100.0 for s in syms}
    q.setdefault("SPY", 600.0)
    return {"as_of": as_of, "account_id": "MCP", "cash": cash, "tax_lots": list(lots),
            "quotes": q, "recent_orders": list(orders), "external_buys": []}


BENCH = pd.Series(0.1, index=[f"S{i}" for i in range(10)])


def test_initial_deploy_plan(tmp_path: Path):
    plan = make_plan(_snap(10_000), BENCH, Config(), tmp_path / "s.json", "buys", Guardrails())
    assert not plan["guardrail_errors"]
    assert abs(sum(o["dollars"] for o in plan["orders"]) - 10_000) < 10
    assert len(plan["sha256"]) == 64


def test_harvest_plan_then_fills_block_rebuy(tmp_path: Path):
    st = tmp_path / "s.json"
    lots = [{"symbol": s, "quantity": 10, "cost_per_share": 100.0, "acquired": "2026-06-01", "lot_id": f"L{s}"}
            for s in BENCH.index]
    quotes = {s: 100.0 for s in BENCH.index}; quotes["S3"] = 80.0
    snap = _snap(0.0, lots, quotes)
    plan = make_plan(snap, BENCH, Config(min_harvest=1, sleeve="SPY"), st, "sells", Guardrails())
    harv = [o for o in plan["orders"] if o["reason"] == "harvest"]
    assert [o["symbol"] for o in harv] == ["S3"] and harv[0]["broker_lot_ids"] == ["LS3"]
    new = record_fills(plan, [{"symbol": "S3", "side": "sell", "quantity": 10, "avg_price": 80.0,
                               "date": "2026-11-25"}], snap, st)
    assert new and new[0]["basis"] == 1000.0
    # next day, with cash from the sale, the engine must not buy S3 back
    lots2 = [l for l in lots if l["symbol"] != "S3"]
    plan2 = make_plan(_snap(800.0, lots2, quotes, as_of="2026-11-26"), BENCH, Config(min_harvest=1, sleeve="SPY"), st, "buys", Guardrails())
    assert "S3" not in {o["symbol"] for o in plan2["orders"]}
    assert plan2["orders"]


def test_guardrails_block_oversized_and_out_of_universe():
    g = Guardrails(max_order=100, max_turnover=0.1)
    from ddi.engine import Order
    errs = g.check([Order("ZZZ", "buy", dollars=500)], pd.Series({"ZZZ": 1.0}), 10_000, 600, {"S0"})
    assert any("outside" in e for e in errs) and any("max_order" in e for e in errs)


def test_buy_in_main_account_counts_for_wash_sale(tmp_path: Path):
    lots = [{"symbol": "S0", "quantity": 10, "cost_per_share": 100.0, "acquired": "2026-01-02"}]
    quotes = {s: 100.0 for s in BENCH.index}; quotes["S0"] = 70.0
    orders = [{"symbol": "S0", "side": "buy", "quantity": 1, "price": 72, "date": "2026-11-20", "account_id": "MAIN"}]
    plan = make_plan(_snap(0.0, lots, quotes, orders), BENCH, Config(min_harvest=1), tmp_path / "s.json", "sells", Guardrails())
    assert not [o for o in plan["orders"] if o["reason"] == "harvest"]


def test_partial_snapshot_is_refused(tmp_path: Path):
    snap = _snap(10_000, quotes={"S0": 100.0, "S1": 100.0})
    plan = make_plan(snap, BENCH, Config(), tmp_path / "s.json", "buys", Guardrails())
    assert any("missing" in e for e in plan["guardrail_errors"])
