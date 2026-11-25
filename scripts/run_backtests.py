"""Run the full backtest grid in parallel and write results/ (json + parquet)."""
import json, sys, time
from dataclasses import asdict, replace
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ddi import data, backtest
from ddi.engine import Config

OUT = Path(__file__).resolve().parents[1] / "results"
OUT.mkdir(exist_ok=True)
START, END = "2016-01-04", None
BASE = Config(method="full", tlh=True, sleeve=None, lot_mode="specific")

GRID = []
def add(name, cfg, start=START, initial=100_000, contrib=0.0, group=""):
    GRID.append(dict(name=name, cfg=cfg, start=start, initial=initial, contrib=contrib, group=group))

# A. construction methods ($100k)
add("full", BASE, group="method")
for n in (50, 100, 200):
    add(f"top-{n}", replace(BASE, method="topn", n=n), group="method")
    add(f"optimized-{n}", replace(BASE, method="optimized", n=n), group="method")
# B. account size
for v in (5_000, 25_000, 1_000_000):
    add(f"full ${v:,}", BASE, initial=v, group="size")
add("optimized-100 $5,000", replace(BASE, method="optimized", n=100), initial=5_000, group="size")
# C. lot matching available at the broker
add("lots: HIFO default", replace(BASE, lot_mode="hifo"), group="lots")
add("lots: FIFO (whole-position harvest)", replace(BASE, lot_mode="fifo"), group="lots")
# D. harvest threshold
for t in (0.02, 0.10, 0.20):
    add(f"threshold {int(t*100)}%", replace(BASE, harvest_threshold=t), group="threshold")
# D2. drift budget frontier (tracking error vs tax)
for a in (0.03, 0.05, 0.20, 1.0):
    add(f"drift budget {int(a*100)}%", replace(BASE, active_share_budget=a), group="frontier")
add("ETF sleeve, 5% cap", replace(BASE, sleeve="SPY", max_sleeve=0.05), group="frontier")
add("ETF sleeve, 20% cap", replace(BASE, sleeve="SPY", max_sleeve=0.20), group="frontier")
# F. start-date cohorts + contributions
for y in (2018, 2020, 2022, 2024):
    add(f"start {y}", BASE, start=f"{y}-01-02", group="cohort")
add("full + $1k/mo contributions", BASE, contrib=1_000, group="contrib")

# matched no-TLH twin for every TLH job (same construction, size, dates, flows)
def _key(j):
    c = replace(j["cfg"], tlh=False, harvest_threshold=0, lot_mode="specific", active_share_budget=0,
                max_sleeve=0, sleeve=None)
    return repr((c, j["start"], j["initial"], j["contrib"]))
_twins = {}
for j in list(GRID):
    if j["cfg"].tlh:
        k = _key(j)
        j["twin"] = k
        if k not in _twins:
            _twins[k] = dict(name="twin:" + j["name"], cfg=replace(j["cfg"], tlh=False), start=j["start"],
                             initial=j["initial"], contrib=j["contrib"], group="twin", key=k)
GRID += list(_twins.values())

_md = None
def work(job):
    global _md
    if _md is None:
        _md = data.load()
    t = time.time()
    r = backtest.run(_md, job["cfg"], job["start"], END, job["initial"], job["contrib"])
    s = r.summary()
    s.update(name=job["name"], group=job["group"], twin=job.get("twin"), key=job.get("key"), start=job["start"], initial=job["initial"],
             contrib=job["contrib"], secs=round(time.time() - t, 1),
             cfg={k: (list(v) if isinstance(v, frozenset) else v) for k, v in asdict(job["cfg"]).items()})
    by = r.by_year().reset_index()
    s["by_year"] = by.to_dict(orient="records")
    wk = r.daily[["value", "port_r", "idx_r", "spy_r", "n_hold", "cash"]].resample("W-FRI").agg(
        {"value": "last", "port_r": lambda x: (1 + x).prod() - 1, "idx_r": lambda x: (1 + x).prod() - 1,
         "spy_r": lambda x: (1 + x).prod() - 1, "n_hold": "last", "cash": "last"})
    s["weekly"] = {"date": [d.strftime("%Y-%m-%d") for d in wk.index], **{c: wk[c].round(6).tolist() for c in wk.columns}}
    print(f"done {job['name']:<40} {s['secs']}s  harvest_yield={s['harvest_yield_annual']:.3f} te_spy={s['te_vs_spy']:.4f}", flush=True)
    return s

if __name__ == "__main__":
    only = sys.argv[1:]  # optional name filters
    jobs = [j for j in GRID if not only or any(o in j["name"] for o in only)]
    with Pool(min(12, len(jobs))) as p:
        res = p.map(work, jobs, chunksize=1)
    if only and (OUT / "grid.json").exists():  # partial rerun: merge by name
        old = {r["name"]: r for r in json.loads((OUT / "grid.json").read_text())}
        old.update({r["name"]: r for r in res})
        res = list(old.values())
    (OUT / "grid.json").write_text(json.dumps(res, default=str))
    print("wrote", OUT / "grid.json")
