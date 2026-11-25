"""Ex-ante tracking error vs account size for each construction method (one date)."""
import json, sys
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ddi import data, construct

md = data.load()
d = md.close.index[-1]
b, cov = data.bench_weights(md, d)
rets = md.returns().loc[:d].iloc[-252:][b.index]
risk = construct.RiskModel.fit(rets)
px = md.close.loc[d]
rows = []
for V in (2_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000, 1_000_000):
    cands = {
        "full (frac, $1 min)": construct.full(b, V, 1.0),
        "full (frac, $5 min)": construct.full(b, V, 5.0),
        "top-50": construct.topn(b, 50, V, 1.0),
        "top-100": construct.topn(b, 100, V, 1.0),
        "top-200": construct.topn(b, 200, V, 1.0),
        "optimized-50": construct.optimized(b, risk, 50, V, 1.0),
        "optimized-100": construct.optimized(b, risk, 100, V, 1.0),
        "optimized-200": construct.optimized(b, risk, 200, V, 1.0),
    }
    for name, w in list(cands.items()):
        rows.append(dict(value=V, method=name, holdings=int(len(w)), te=risk.tracking_error(w, b)))
    for name in ("full (frac, $1 min)", "optimized-100"):
        sh = construct.whole_shares(cands[name], px, V)
        dollars = sh * px.reindex(sh.index)
        w = dollars / V  # leftover cash is an active (zero-return) position
        cash = 1 - w.sum()
        te = risk.tracking_error(w, b)
        rows.append(dict(value=V, method=f"{name} → whole shares", holdings=int(len(sh)), te=te, cash=cash))
    print(V, flush=True)
df = pd.DataFrame(rows)
print(df.pivot(index="method", columns="value", values="te").round(4).to_string())
print(df.pivot(index="method", columns="value", values="holdings").to_string())
out = Path(__file__).resolve().parents[1] / "results" / "static_te.json"
out.write_text(json.dumps({"date": str(d.date()), "coverage": cov, "n_bench": len(b), "rows": rows}))
