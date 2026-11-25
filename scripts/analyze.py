"""Turn results/grid.json (+ static_te.json) into results/report.json for the write-up."""
import json
from pathlib import Path
import pandas as pd

R = Path(__file__).resolve().parents[1] / "results"
grid = json.loads((R / "grid.json").read_text())
twins = {g["key"]: g for g in grid if g.get("group") == "twin"}
rows = []
for g in grid:
    if g["group"] == "twin":
        continue
    t = twins.get(g.get("twin"))
    yrs = g["years"]
    row = {k: g[k] for k in ("name", "group", "start", "initial", "years", "cagr_port", "cagr_spy", "te_vs_synth", "te_vs_spy",
                            "avg_holdings", "harvest_yield_annual", "net_st", "net_lt", "turnover_annual", "trades_per_year",
                            "final_value", "wealthfront_fee_drag_end", "tlh_value_liquidate", "tlh_value_hold")}
    if t:
        row["base_te_vs_spy"] = t["te_vs_spy"]
        row["base_te_vs_synth"] = t["te_vs_synth"]
        row["base_cagr"] = t["cagr_port"]
        row["add_liq"] = g["tlh_value_liquidate"] - t["tlh_value_liquidate"]
        row["add_hold"] = g["tlh_value_hold"] - t["tlh_value_hold"]
        # express as % of the twin's final value, and annualised-equivalent
        row["add_liq_pct"] = row["add_liq"] / t["final_value"]
        row["add_hold_pct"] = row["add_hold"] / t["final_value"]
        row["add_liq_ann"] = (1 + row["add_liq_pct"]) ** (1 / yrs) - 1
        row["add_hold_ann"] = (1 + row["add_hold_pct"]) ** (1 / yrs) - 1
    by = pd.DataFrame(g["by_year"])
    if len(by):
        row["harvest_by_year"] = {int(r["year"]): round(r["harvest_yield"], 4) for _, r in by.iterrows()}
        first = by.iloc[0]
        row["first_year_harvest_yield"] = first["harvest_yield"]
        row["cum_harvested_pct_initial"] = float(-by.harvested.sum() / g["initial"])
    rows.append(row)
df = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
cols = ["name", "years", "avg_holdings", "te_vs_spy", "base_te_vs_spy", "harvest_yield_annual", "first_year_harvest_yield",
        "cum_harvested_pct_initial", "add_liq", "add_hold", "add_liq_ann", "add_hold_ann", "wealthfront_fee_drag_end",
        "net_lt", "trades_per_year"]
print(df[cols].round(4).to_string())
weekly = {g["name"]: g["weekly"] for g in grid if g["name"] in ("full", "start 2020", "start 2022")}
static = json.loads((R / "static_te.json").read_text())
(R / "report.json").write_text(json.dumps({"rows": rows, "static": static, "weekly": weekly}, default=float))
