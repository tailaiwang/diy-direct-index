"""10-year head-to-head: DIY direct index vs SPY, VOO, and a modeled Wealthfront S&P 500 Direct.

$100k invested 2016-11-25, held to the last data day. Writes results/compare_10y.json.

DIY 'clean' = the index's actual path (VOO grossed up for its 0.03% fee, since owning the stocks
directly has no fund fee) + what TLH added vs a matched no-TLH twin. This strips the
survivorship noise in the free price data (the twin has the same noise) so the comparison
isolates the strategy. Wealthfront is modeled as the same strategy minus its 0.09%/yr fee.
"""
import json, sys
from dataclasses import replace
from multiprocessing import Pool
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ddi import backtest, data
from ddi.engine import Config

ROOT = Path(__file__).resolve().parents[1]
START, INIT, TAX_LT = "2016-11-25", 100_000.0, 0.238
CFG = Config(method="full", tlh=True, sleeve=None, lot_mode="specific")
FEES = {"SPY": 0.0, "VOO": 0.0, "WF": 0.0009}   # ETF expense ratios are already in their prices


def work(tlh):
    md = data.load()
    r = backtest.run(md, replace(CFG, tlh=tlh), START, None, INIT, 0.0)
    return r.summary(), r.daily[["value"]], r.daily.spy_r


if __name__ == "__main__":
    with Pool(2) as p:
        (s, dv, spy_r), (tw, _, _) = p.map(work, [True, False])
    idx = dv.index
    voo = yf.download("VOO", start="2016-11-01", end="2026-11-26", auto_adjust=True, progress=False)["Close"].squeeze()
    voo_r = voo.reindex(idx).ffill().pct_change().fillna(0.0)
    spy_path = INIT * (1 + spy_r).cumprod()
    voo_path = INIT * (1 + voo_r).cumprod()
    yrs = len(idx) / 252

    # TLH increments vs the matched twin (same data, same construction, no harvesting)
    pre_delta = s["final_value"] - tw["final_value"]          # trading-cost drag of TLH, pre-tax
    liq_delta = s["after_tax_liquidate"] - tw["after_tax_liquidate"]
    hold_delta = s["after_tax_hold"] - tw["after_tax_hold"]
    # path of the cumulative TLH tax value, scaled linearly in time for the chart
    t = pd.Series(range(len(idx)), index=idx) / (len(idx) - 1)

    def etf(path):
        end = float(path.iloc[-1])
        return {"pre_tax": end, "after_tax_sell": end - TAX_LT * (end - INIT), "after_tax_hold": end}

    spy, voo_e = etf(spy_path), etf(voo_path)
    base = etf(voo_path * (1 + 0.0003) ** (t * yrs))     # index with no fund fee
    diy = {"pre_tax": base["pre_tax"] + pre_delta,
           "after_tax_sell": base["after_tax_sell"] + liq_delta,
           "after_tax_hold": base["after_tax_hold"] + hold_delta}
    fee_mult = (1 - FEES["WF"]) ** yrs
    wf = {k: v - diy["pre_tax"] * (1 - fee_mult) for k, v in diy.items()}
    raw = {"pre_tax": s["final_value"], "after_tax_sell": s["after_tax_liquidate"], "after_tax_hold": s["after_tax_hold"]}

    # weekly chart paths (pre-tax value + accrued TLH tax savings for DIY/WF, hold bookend)
    wk = lambda x: x.resample("W-FRI").last()
    diy_path = voo_path * (1 + 0.0003) ** (t * yrs) + pre_delta * t + hold_delta * t
    wf_path = diy_path * (1 - FEES["WF"]) ** (t * yrs)
    out = {
        "start": str(idx[0].date()), "end": str(idx[-1].date()), "years": round(yrs, 2), "initial": INIT,
        "results": {"SPY": spy, "VOO": voo_e, "DIY (Robinhood MCP)": diy, "Wealthfront S&P 500 Direct (modeled)": wf,
                    "DIY raw backtest (noisy data)": raw},
        "cagr": {"SPY": (spy["pre_tax"] / INIT) ** (1 / yrs) - 1, "VOO": (voo_e["pre_tax"] / INIT) ** (1 / yrs) - 1},
        "tlh": {"harvested_losses": s["harvested_losses"], "harvest_yield_annual": s["harvest_yield_annual"],
                "liq_delta": liq_delta, "hold_delta": hold_delta, "pre_delta": pre_delta,
                "wf_fee_total": diy["pre_tax"] * (1 - fee_mult), "te_vs_spy": s["te_vs_spy"], "twin_te_vs_spy": tw["te_vs_spy"]},
        "weekly": {"date": [d.strftime("%Y-%m-%d") for d in wk(spy_path).index],
                   "SPY": wk(spy_path).round(0).tolist(), "VOO": wk(voo_path).round(0).tolist(),
                   "DIY": wk(diy_path).round(0).tolist(), "WF": wk(wf_path).round(0).tolist()},
    }
    (ROOT / "results" / "compare_10y.json").write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps({k: v for k, v in out.items() if k != "weekly"}, indent=1, default=float))
