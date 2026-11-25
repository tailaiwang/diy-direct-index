from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ddi import construct
from ddi.engine import Config, Engine, State
from ddi.lots import Ledger

D0 = date(2024, 1, 2)


def d(n):
    return D0 + timedelta(days=n)


# ---------------------------------------------------------------- ledger / wash sales
def test_fifo_and_hifo_lot_order():
    L = Ledger()
    L.buy("A", 10, 100, d(0))
    L.buy("A", 10, 120, d(40))
    r = L.sell("A", 10, 110, d(100), mode="fifo")
    assert r[0].basis == pytest.approx(1000) and r[0].gain == pytest.approx(100)
    L2 = Ledger()
    L2.buy("A", 10, 100, d(0))
    L2.buy("A", 10, 120, d(40))
    r = L2.sell("A", 10, 110, d(100), mode="hifo")
    assert r[0].gain == pytest.approx(-100)


def test_loss_then_rebuy_within_30_days_is_washed_and_basis_moves():
    L = Ledger()
    L.buy("A", 10, 100, d(0))
    L.sell("A", 10, 80, d(60), mode="fifo")          # -200 loss
    assert not L.can_buy("A", d(70))
    lot = L.buy("A", 10, 85, d(70))                    # forward wash
    r = L.realized[0]
    assert r.washed == pytest.approx(200) and r.gain == pytest.approx(0)
    assert lot.cost == pytest.approx(85 + 20)          # disallowed loss added to basis


def test_rebuy_after_31_days_is_clean():
    L = Ledger()
    L.buy("A", 10, 100, d(0))
    L.sell("A", 10, 80, d(60), mode="fifo")
    assert L.can_buy("A", d(92))
    L.buy("A", 10, 85, d(92))
    assert L.realized[0].gain == pytest.approx(-200)


def test_buy_before_loss_sale_washes_when_replacement_still_held():
    L = Ledger()
    L.buy("A", 10, 100, d(0))
    L.buy("A", 5, 90, d(50))                           # recent buy
    assert not L.can_sell_at_loss("A", d(60))
    L.sell("A", 10, 80, d(60), mode="fifo")            # sells the old lot at a loss
    r = L.realized[0]
    assert r.washed == pytest.approx(100)              # 5 replacement shares x $20
    assert L.lots["A"][0].cost == pytest.approx(90 + 100 / 5)


def test_selling_entire_position_including_recent_lot_is_not_washed():
    L = Ledger()
    L.buy("A", 10, 100, d(0))
    L.buy("A", 5, 90, d(50))
    assert L.can_sell_at_loss("A", d(60), selling_all=True)
    L.sell("A", 15, 80, d(60), mode="fifo")
    assert sum(r.washed for r in L.realized) == 0
    assert sum(r.gain for r in L.realized) == pytest.approx(-200 - 50)


def test_external_account_purchase_blocks_loss_sale():
    L = Ledger()
    L.buy("A", 10, 100, d(0))
    L.external_buys["A"] = [d(55)]
    assert not L.can_sell_at_loss("A", d(60), selling_all=True)


def test_term_classification():
    L = Ledger()
    L.buy("A", 1, 100, d(0))
    L.buy("A", 1, 100, d(300))
    L.sell("A", 2, 150, d(400), mode="fifo")
    assert [r.term for r in L.realized] == ["LT", "ST"]


# ---------------------------------------------------------------- construction
def _bench(n=50, seed=0):
    rng = np.random.default_rng(seed)
    caps = np.sort(rng.pareto(1.2, n) + 1)[::-1]
    return pd.Series(caps / caps.sum(), index=[f"S{i}" for i in range(n)])


def _rets(syms, T=300, seed=1):
    rng = np.random.default_rng(seed)
    f = rng.normal(0, 0.01, (T, 3))
    B = rng.normal(1, 0.3, (len(syms), 3))
    return pd.DataFrame(f @ B.T + rng.normal(0, 0.01, (T, len(syms))), columns=syms)


def test_full_drops_small_positions_and_sums_to_one():
    b = _bench()
    w = construct.full(b, value=1000, min_position=20)
    assert w.sum() == pytest.approx(1) and (w * 1000 >= 20 - 1e-9).all()


def test_optimized_beats_topn_on_tracking_error():
    b = _bench(80)
    risk = construct.RiskModel.fit(_rets(b.index))
    wt = construct.topn(b, 20)
    wo = construct.optimized(b, risk, 20)
    assert len(wo) <= 20
    assert risk.tracking_error(wo, b) < risk.tracking_error(wt, b)


def test_whole_shares_never_overspends():
    b = _bench(30)
    px = pd.Series(np.linspace(20, 900, 30), index=b.index)
    sh = construct.whole_shares(b, px, 5000)
    assert (sh * px.reindex(sh.index)).sum() <= 5000 + 1e-9
    assert (sh == sh.round()).all()


# ---------------------------------------------------------------- engine
def _setup(cfg):
    syms = [f"S{i}" for i in range(10)]
    b = pd.Series(1 / 10, index=syms)
    px = pd.Series(100.0, index=syms + ["SPY"])
    st = State(cash=10_000)
    eng = Engine(cfg)
    eng.plan_sells(D0, px, b, st)
    for o in eng.plan_buys(D0, px, b, st, force=True):
        st.ledger.buy(o.sym, o.dollars / px[o.sym], px[o.sym], D0)
        st.cash -= o.dollars
    return eng, st, b, px


def test_harvest_respects_threshold_and_wash_window():
    eng, st, b, px = _setup(Config(min_harvest=1, harvest_threshold=0.05, cost_bps=0, sleeve="SPY"))
    px2 = px.copy(); px2["S0"] = 90.0
    st.ledger.buy("S0", 1, 85.0, d(5))   # a recent lot that is NOT a loser stays held
    # inside 30 days of that purchase: blocked (it would wash the harvested loss)
    assert not [o for o in eng.plan_sells(d(10), px2, b, st) if o.reason == "harvest"]
    sells = [o for o in eng.plan_sells(d(40), px2, b, st) if o.reason == "harvest"]
    assert [o.sym for o in sells] == ["S0"]
    # a 3% dip is below threshold
    px3 = px.copy(); px3["S1"] = 97.0
    assert not [o for o in eng.plan_sells(d(41), px3, b, st) if o.sym == "S1"]


def test_harvested_weight_goes_to_replacements_and_sleeve_not_original():
    eng, st, b, px = _setup(Config(min_harvest=1, cost_bps=0, rep_abs=0.01, rep_mult=0.1, sleeve="SPY", max_sleeve=0.5))
    px2 = px.copy(); px2["S0"] = 80.0
    for o in eng.plan_sells(d(40), px2, b, st):
        st.ledger.sell(o.sym, o.qty, px2[o.sym], d(40), mode="specific", lot_ids=o.lot_ids)
        st.cash += o.qty * px2[o.sym]
    tgt = eng.targets(d(40), px2, b, st)
    assert tgt.get("S0", 0) == 0
    assert tgt.get("SPY", 0) > 0                     # capacity-limited overflow -> sleeve
    buys = eng.plan_buys(d(40), px2, b, st, force=True)
    assert "S0" not in {o.sym for o in buys}
    # after the window the original is buyable again
    assert st.ledger.can_buy("S0", d(72))


def test_without_sleeve_harvest_only_when_replacements_have_room():
    tight = _setup(Config(min_harvest=1, cost_bps=0))                     # caps 0.1-0.3% << 10% position
    roomy = _setup(Config(min_harvest=1, cost_bps=0, rep_abs=0.05, rep_max=0.05))
    for (eng, st, b, px), expect in ((tight, False), (roomy, True)):
        px2 = px.copy(); px2["S0"] = 80.0
        got = any(o.reason == "harvest" for o in eng.plan_sells(d(40), px2, b, st))
        assert got is expect
