"""ddi - DIY direct indexing.

  ddi weights                          fetch today's S&P 500 weights (SPY holdings)
  ddi target --value 25000             show the target portfolio for a $X account
  ddi plan --snapshot s.json --phase sells|buys [--state state.json] [--out plan.json]
  ddi record-fills --plan plan.json --fills fills.json --snapshot s.json
  ddi backtest --method full --start 2016-01-04 --value 100000
  ddi mcp-tools                        dump Robinhood MCP tool schemas (sign-in required)
  ddi mcp-run --plan plan.json [--place]   review (default) or place a plan headlessly
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import fields
from pathlib import Path

import pandas as pd

from .engine import Config

HOME = Path.home() / ".ddi"


def _cfg(args) -> Config:
    path = Path(args.config) if getattr(args, "config", None) else HOME / "config.json"
    raw = json.loads(path.read_text()) if path.exists() else {}
    names = {f.name for f in fields(Config)}
    raw = {k: v for k, v in raw.items() if k in names}
    if "exclude" in raw:
        raw["exclude"] = frozenset(raw["exclude"])
    for k in ("method", "n"):
        v = getattr(args, k, None)
        if v is not None:
            raw[k] = v
    return Config(**raw)


def _weights(cache_dir: Path) -> pd.Series:
    from . import index_weights
    cache_dir.mkdir(parents=True, exist_ok=True)
    return index_weights.fetch(cache_dir / "spy_holdings.xlsx")


def main(argv=None):
    p = argparse.ArgumentParser(prog="ddi", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("weights")
    t = sub.add_parser("target"); t.add_argument("--value", type=float, required=True)
    t.add_argument("--method"); t.add_argument("--n", type=int)
    pl = sub.add_parser("plan")
    pl.add_argument("--snapshot", required=True); pl.add_argument("--phase", choices=["sells", "buys"], required=True)
    pl.add_argument("--state", default=str(HOME / "state.json")); pl.add_argument("--out")
    pl.add_argument("--max-order", type=float, default=5_000); pl.add_argument("--max-turnover", type=float, default=0.30)
    rf = sub.add_parser("record-fills")
    rf.add_argument("--plan", required=True); rf.add_argument("--fills", required=True)
    rf.add_argument("--snapshot", required=True); rf.add_argument("--state", default=str(HOME / "state.json"))
    bt = sub.add_parser("backtest")
    bt.add_argument("--method"); bt.add_argument("--n", type=int)
    bt.add_argument("--start", default="2016-01-04"); bt.add_argument("--end")
    bt.add_argument("--value", type=float, default=100_000)
    sub.add_parser("mcp-tools")
    mr = sub.add_parser("mcp-run"); mr.add_argument("--plan", required=True); mr.add_argument("--place", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "weights":
        w = _weights(HOME)
        print(w.sort_values(ascending=False).head(25).to_string()); print(f"... {len(w)} names")
    elif a.cmd == "target":
        from . import construct
        cfg = _cfg(a)
        w = _weights(HOME)
        if cfg.method == "optimized":
            raise SystemExit("optimized needs return history; use `backtest` or pass a risk model")
        tw = construct.build(cfg.method, w, a.value, n=cfg.n, min_position=cfg.min_position, exclude=set(cfg.exclude))
        out = (tw * a.value).round(2).sort_values(ascending=False)
        print(out.to_string()); print(f"{len(out)} positions, ${out.sum():,.2f}")
    elif a.cmd == "plan":
        from .live import Guardrails, make_plan
        cfg = _cfg(a)
        snap = json.loads(Path(a.snapshot).read_text())
        HOME.mkdir(exist_ok=True)
        plan = make_plan(snap, _weights(HOME), cfg, Path(a.state), a.phase,
                         Guardrails(a.max_order, a.max_turnover))
        s = json.dumps(plan, indent=1)
        Path(a.out).write_text(s) if a.out else print(s)
        n = len(plan["orders"]); errs = plan["guardrail_errors"]
        print(f"{a.phase}: {n} orders, value ${plan['portfolio_value']:,.2f}" + (f"  GUARDRAIL ERRORS: {errs}" if errs else ""))
    elif a.cmd == "record-fills":
        from .live import record_fills
        new = record_fills(json.loads(Path(a.plan).read_text()), json.loads(Path(a.fills).read_text()),
                           json.loads(Path(a.snapshot).read_text()), Path(a.state))
        print(f"recorded {len(new)} loss sales (30-day no-rebuy window now active for them)")
    elif a.cmd == "backtest":
        from . import backtest, data
        cfg = _cfg(a)
        r = backtest.run(data.load(), cfg, a.start, a.end, a.value)
        for k, v in r.summary().items():
            print(f"{k:>24}: {v:,.4f}" if isinstance(v, float) else f"{k:>24}: {v}")
        print(r.by_year().round(4).to_string())
    elif a.cmd in ("mcp-tools", "mcp-run"):
        from .broker.robinhood_mcp import RobinhoodMCP
        HOME.mkdir(exist_ok=True)

        async def go():
            async with RobinhoodMCP(HOME / "rh_oauth.json") as rh:
                if a.cmd == "mcp-tools":
                    print(json.dumps(await rh.tools(), indent=1))
                else:
                    res = await rh.execute(json.loads(Path(a.plan).read_text()), place=a.place)
                    print(json.dumps(res, indent=1, default=str))
        asyncio.run(go())


if __name__ == "__main__":
    main()
