"""Build docs/report.html from results/report.json. Every number on the page comes from results/."""
import json
import sys
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from charts import bar_chart, legend, line_chart  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
D = json.loads((ROOT / "results" / "report.json").read_text())
rows = {r["name"]: r for r in D["rows"]}
st = D["static"]

pct = lambda v, d=1: f"{v * 100:.{d}f}%"
usd = lambda v: f"${v:,.0f}"
k = lambda v: f"${v / 1000:,.1f}k"


# ---------------------------------------------------------------- chart 1: TE vs size
srows = st["rows"]
def s_series(method):
    return sorted((r["value"], r["te"]) for r in srows if r["method"] == method)
c1_series = [
    ("All ~500, fractional", "--s1", s_series("full (frac, $1 min)")),
    ("Optimized 200", "--s2", s_series("optimized-200")),
    ("Top 100 by size", "--s3", s_series("top-100")),
    ("All ~500, whole shares only", "--s4", s_series("full (frac, $1 min) → whole shares")),
]
chart1 = line_chart(c1_series, x_labels=lambda v: f"${v / 1000:g}k" if v < 1e6 else "$1M",
                    y_fmt=lambda v: f"{v * 100:.0f}%" if v >= 0.01 or v == 0 else f"{v * 100:.1f}%",
                    y_max=0.12, title="Ex-ante tracking error vs account size", x_title="Account size (log scale)",
                    y_title="Tracking error / yr")
t1 = {r["method"]: r for r in srows if r["value"] == 5000}
t1b = {r["method"]: r for r in srows if r["value"] == 100000}

# ---------------------------------------------------------------- chart 2: harvest by year
full = rows["full"]
hy = full["harvest_by_year"]
years = sorted(int(y) for y in hy)
c20 = rows.get("start 2020", {}).get("harvest_by_year", {})
c22 = rows.get("start 2022", {}).get("harvest_by_year", {})
chart2 = bar_chart([str(y) for y in years],
                   [("Started Jan 2016", "--s1", [hy[str(y)] if str(y) in hy else hy.get(y, 0) for y in years]),
                    ("Started Jan 2020", "--s2", [c20.get(str(y), 0) for y in years]),
                    ("Started Jan 2022", "--s3", [c22.get(str(y), 0) for y in years])],
                   y_fmt=lambda v: f"{v * 100:.0f}%", value_labels=False, height=280,
                   title="Losses harvested per year as % of portfolio value")

# ---------------------------------------------------------------- chart 3: value vs fee
coh = [("2016", rows["full"]), ("2018", rows["start 2018"]), ("2020", rows["start 2020"]),
       ("2022", rows["start 2022"]), ("2024", rows["start 2024"])]
chart3 = bar_chart([f"Start {y}" for y, _ in coh],
                   [("TLH value, sell everything at end", "--s1", [max(r["add_liq"], 0) for _, r in coh]),
                    ("TLH value, never sell", "--s2", [max(r["add_hold"], 0) for _, r in coh]),
                    ("Wealthfront fee you'd avoid", "--s3", [r["wealthfront_fee_drag_end"] for _, r in coh])],
                   y_fmt=lambda v: f"${v / 1000:.0f}k" if v >= 1000 else f"${v:.0f}", height=300,
                   title="Dollar value of TLH vs. the Wealthfront fee, per $100k invested")

# ---------------------------------------------------------------- chart 4: 10-year head-to-head
C10 = json.loads((ROOT / "results" / "compare_10y.json").read_text())
R10 = C10["results"]
P10 = [("VOO", "VOO", "--s1"), ("Wealthfront (modeled)", "Wealthfront S&P 500 Direct (modeled)", "--s2"),
       ("DIY on Robinhood", "DIY (Robinhood MCP)", "--s3")]
M10 = [("pre_tax", "Before tax"), ("after_tax_sell", "After tax, sell everything"), ("after_tax_hold", "After tax, never sell")]
sgn = lambda v: ("+" if v >= 0 else "−") + f"${abs(v) / 1000:.1f}k"
chart4 = bar_chart([m for _, m in M10],
                   [(lab, col, [R10[key][m] - R10["SPY"][m] for m, _ in M10]) for lab, key, col in P10],
                   y_fmt=sgn, height=280, title="Dollars ahead of SPY after 10 years, per $100k")
def _t10():
    t10 = [tr(["", "SPY", "VOO", "Wealthfront (modeled)", "DIY on Robinhood"], head=True)]
    for m, lab in M10:
        t10.append(tr([lab] + [usd(R10[k][m]) for k in ("SPY", "VOO", "Wealthfront S&P 500 Direct (modeled)", "DIY (Robinhood MCP)")]))
    return '<div class="tbl"><table>' + "".join(t10) + "</table></div>"


def tr(cells, head=False):
    tag = "th" if head else "td"
    return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"


def scen_table(names, label_map=None):
    h = tr(["Scenario", "Holdings", "TE vs SPY", "No-TLH TE", "Harvested / yr", "1st-yr harvest",
            "TLH value (sell all)", "TLH value (hold)", "Realized LT gains", "Trades / yr"], head=True)
    out = [h]
    for n in names:
        r = rows.get(n)
        if not r:
            continue
        out.append(tr([escape((label_map or {}).get(n, n)), f"{r['avg_holdings']:.0f}", pct(r["te_vs_spy"], 2),
                       pct(r.get("base_te_vs_spy", 0), 2), pct(r["harvest_yield_annual"]),
                       pct(r.get("first_year_harvest_yield", 0)),
                       f"{k(r.get('add_liq', 0))} <small>({pct(r.get('add_liq_ann', 0), 2)}/yr)</small>",
                       f"{k(r.get('add_hold', 0))} <small>({pct(r.get('add_hold_ann', 0), 2)}/yr)</small>",
                       k(max(r["net_lt"], 0)), f"{r['trades_per_year']:.0f}"]))
    return '<div class="tbl"><table>' + "".join(out) + "</table></div>"


method_tbl = scen_table(["full", "optimized-200", "optimized-100", "optimized-50", "top-200", "top-100", "top-50"],
                        {"full": "All ~500 (default)", "optimized-200": "Optimized 200", "optimized-100": "Optimized 100",
                         "optimized-50": "Optimized 50", "top-200": "Top 200", "top-100": "Top 100", "top-50": "Top 50"})
size_tbl = scen_table(["full $5,000", "optimized-100 $5,000", "full $25,000", "full", "full $1,000,000"],
                      {"full $5,000": "$5k, all names", "optimized-100 $5,000": "$5k, optimized 100",
                       "full $25,000": "$25k", "full": "$100k", "full $1,000,000": "$1M"})
knob_tbl = scen_table(["full", "threshold 2%", "threshold 10%", "threshold 20%", "drift budget 3%", "drift budget 5%",
                       "drift budget 20%", "drift budget 100%", "ETF sleeve, 5% cap", "ETF sleeve, 20% cap",
                       "lots: HIFO default", "lots: FIFO (whole-position harvest)", "full + $1k/mo contributions"],
                      {"full": "Default (5% trigger, 10% drift, no sleeve, specific lots)",
                       "threshold 2%": "Harvest trigger 2%", "threshold 10%": "Harvest trigger 10%",
                       "threshold 20%": "Harvest trigger 20%", "drift budget 3%": "Drift budget 3%",
                       "drift budget 5%": "Drift budget 5%", "drift budget 20%": "Drift budget 20%",
                       "drift budget 100%": "Never realize gains to rebalance",
                       "ETF sleeve, 5% cap": "+ ETF overflow sleeve, 5% cap", "ETF sleeve, 20% cap": "+ ETF overflow sleeve, 20% cap", "lots: HIFO default": "Broker default HIFO",
                       "lots: FIFO (whole-position harvest)": "Broker FIFO only (whole-position harvest)",
                       "full + $1k/mo contributions": "+ $1k/month deposits"})
cohort_tbl = scen_table(["full", "start 2018", "start 2020", "start 2022", "start 2024"],
                        {"full": "Jan 2016 → Nov 2026", "start 2018": "Jan 2018 →", "start 2020": "Jan 2020 →",
                         "start 2022": "Jan 2022 →", "start 2024": "Jan 2024 →"})

F = rows["full"]
_ratios = [r[key] / r["wealthfront_fee_drag_end"] for _, r in coh[:4] for key in ("add_liq", "add_hold")]
fee_100k_yr = 100_000 * 0.0009

TEMPLATE = (ROOT / "scripts" / "report_template.html").read_text()
html = TEMPLATE
for key, val in {
    "CHART4": chart4, "TBL10": _t10(),
    "LEGEND4": legend([(lab, c) for lab, _, c in P10]),
    "C10_START": C10["start"], "C10_END": C10["end"],
    "C10_HARV": usd(-C10["tlh"]["harvested_losses"]), "C10_WFFEE": usd(C10["tlh"]["wf_fee_total"]),
    "C10_RAW": usd(R10["DIY raw backtest (noisy data)"]["pre_tax"]),
    "CHART1": chart1, "CHART2": chart2, "CHART3": chart3,
    "LEGEND1": legend([(n, c) for n, c, _ in c1_series]),
    "LEGEND2": legend([("Started Jan 2016", "--s1"), ("Started Jan 2020", "--s2"), ("Started Jan 2022", "--s3")]),
    "LEGEND3": legend([("TLH value, sell everything at end", "--s1"), ("TLH value, never sell (step-up / donate)", "--s2"),
                       ("Wealthfront 0.09% fee over the same period", "--s3")]),
    "METHOD_TBL": method_tbl, "SIZE_TBL": size_tbl, "KNOB_TBL": knob_tbl, "COHORT_TBL": cohort_tbl,
    "STATIC_DATE": st["date"],
    "TE5K_FULL": pct(t1["full (frac, $1 min)"]["te"], 2), "N5K_FULL": str(t1["full (frac, $1 min)"]["holdings"]),
    "TE5K_WHOLE": pct(t1["full (frac, $1 min) → whole shares"]["te"], 1), "N5K_WHOLE": str(t1["full (frac, $1 min) → whole shares"]["holdings"]),
    "TE100K_WHOLE": pct(t1b["full (frac, $1 min) → whole shares"]["te"], 1),
    "TE_OPT200": pct(t1b["optimized-200"]["te"], 2), "TE_OPT100": pct(t1b["optimized-100"]["te"], 2),
    "TE_TOP100": pct(t1b["top-100"]["te"], 1),
    "F_YEARS": f"{F['years']:.1f}", "F_HY": pct(F["harvest_yield_annual"]), "F_HY1": pct(F["first_year_harvest_yield"]),
    "F_CUM": pct(F["cum_harvested_pct_initial"], 0), "F_LIQ": k(F["add_liq"]), "F_HOLD": k(F["add_hold"]),
    "F_LIQ_ANN": pct(F["add_liq_ann"], 2), "F_HOLD_ANN": pct(F["add_hold_ann"], 2),
    "F_FEE": k(F["wealthfront_fee_drag_end"]), "F_TE": pct(F["te_vs_spy"], 2), "F_BASE_TE": pct(F["base_te_vs_spy"], 2),
    "F_TRADES": f"{F['trades_per_year']:.0f}", "F_HOLDINGS": f"{F['avg_holdings']:.0f}",
    "FEE_100K": usd(fee_100k_yr),
    "RATIO_LO": f"{min(_ratios):.0f}", "RATIO_HI": f"{max(_ratios):.0f}",
}.items():
    html = html.replace("{{" + key + "}}", val)
assert "{{" not in html, [l for l in html.splitlines() if "{{" in l][:3]
out = ROOT / "docs" / "report.html"
out.write_text(html)
print("wrote", out, len(html) // 1024, "KB")
