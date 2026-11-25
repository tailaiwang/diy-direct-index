"""Quick parallel parameter sweep: python scripts/sweep.py START KEY=V1,V2 [KEY2=...]"""
import itertools, json, sys
from dataclasses import replace
from multiprocessing import Pool
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ddi import data, backtest
from ddi.engine import Config

KEYS = ['tlh_value_liquidate', 'tlh_value_hold', 'cagr_port', 'te_vs_synth', 'te_vs_spy', 'harvest_yield_annual', 'tax_alpha_annual', 'net_st', 'net_lt',
        'turnover_annual', 'trades_per_year', 'after_tax_liquidate', 'after_tax_hold', 'spy_after_tax_liquidate', 'spy_final', 'final_value']
_md = None
def work(args):
    global _md
    start, kw, initial = args
    _md = _md or data.load()
    r = backtest.run(_md, replace(Config(), **kw), start, None, initial)
    s = r.summary()
    return kw, {k: round(float(s[k]), 4) for k in KEYS}

if __name__ == "__main__":
    start = sys.argv[1]
    axes = []
    for a in sys.argv[2:]:
        k, vs = a.split("=")
        axes.append([(k, json.loads(v)) for v in vs.split(",")])
    jobs = [(start, dict(c), 100_000) for c in itertools.product(*axes)]
    with Pool(min(12, len(jobs))) as p:
        for kw, s in p.imap(work, jobs):
            print(json.dumps({"kw": kw, **s}), flush=True)
