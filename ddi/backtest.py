"""Daily event-driven backtest that drives the exact same Engine as live trading."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date

import numpy as np
import pandas as pd

from .data import MarketData, bench_weights
from .engine import Config, Engine, Order, State

TD = 252


@dataclass
class Result:
    cfg: Config
    daily: pd.DataFrame              # value, cash, n_hold, port_r, idx_r, spy_r, coverage
    realized: pd.DataFrame           # one row per closed lot slice
    trades: pd.DataFrame
    final_unrealized: float
    contributions: float
    initial: float = 0.0
    monthly: float = 0.0

    # ---------------- metrics ----------------
    def summary(self) -> dict:
        d = self.daily
        yrs = len(d) / TD
        act_idx = d.port_r - d.idx_r
        act_spy = d.port_r - d.spy_r
        cagr = lambda r: float((1 + r).prod() ** (1 / yrs) - 1)
        rz = self.realized
        avg_val = float(d.value.mean())
        loss = rz.loc[rz.gain < 0, "gain"].sum() if len(rz) else 0.0
        harvested = rz.loc[(rz.gain < 0) & (rz.reason == "harvest"), "gain"].sum() if len(rz) else 0.0
        st = rz.loc[rz.term == "ST", "gain"].sum() if len(rz) else 0.0
        lt = rz.loc[rz.term == "LT", "gain"].sum() if len(rz) else 0.0
        washed = rz.washed.sum() if len(rz) else 0.0
        c = self.cfg
        tax_benefit = -(st * c.tax_st + lt * c.tax_lt)
        traded = self.trades.dollars.abs().sum() if len(self.trades) else 0.0
        return {
            "years": round(yrs, 2),
            "cagr_port": cagr(d.port_r),
            "cagr_synth_index": cagr(d.idx_r),
            "cagr_spy": cagr(d.spy_r),
            "te_vs_synth": float(act_idx.std() * np.sqrt(TD)),
            "te_vs_spy": float(act_spy.std() * np.sqrt(TD)),
            "td_vs_synth": cagr(d.port_r) - cagr(d.idx_r),
            "td_vs_spy": cagr(d.port_r) - cagr(d.spy_r),
            "avg_holdings": float(d.n_hold.mean()),
            "final_value": float(d.value.iloc[-1]),
            "realized_losses": float(loss),
            "harvested_losses": float(harvested),
            "harvest_yield_annual": float(-harvested / avg_val / yrs),
            "net_st": float(st), "net_lt": float(lt),
            "washed_losses": float(washed),
            "tax_benefit_total": float(tax_benefit),
            "tax_alpha_annual": float(tax_benefit / avg_val / yrs),
            "turnover_annual": float(traded / 2 / avg_val / yrs),
            "n_trades": int(len(self.trades)),
            "trades_per_year": float(len(self.trades) / yrs),
            "unrealized_gain_end": float(self.final_unrealized),
            "coverage_min": float(d.coverage.min()),
            **self.after_tax(),
        }

    def after_tax(self) -> dict:
        """After-tax wealth vs. simply holding SPY with the same cash flows.

        Tax flows from realised gains/losses each year are assumed usable against other
        income/gains (needs outside gains for losses > $3k/yr) and are compounded to the end
        at the portfolio's CAGR (savings reinvested / taxes paid from outside funds).
        Two bookends: 'liquidate' (sell everything at the end, pay LT tax on unrealised
        gains) and 'hold' (never sell: step-up at death or donate -> deferred tax never paid).
        Dividend taxes are ignored for both sides (near-identical yields)."""
        d, c = self.daily, self.cfg
        yrs = len(d) / TD
        g = float((1 + d.port_r).prod() ** (1 / yrs) - 1)
        by = self.by_year()
        end_year = d.index[-1].year + d.index[-1].dayofyear / 365
        fv = 0.0
        if len(by):
            for y, b in by.tax_benefit.items():
                fv += b * (1 + g) ** max(end_year - (y + 0.5), 0)
        # SPY buy-and-hold with the same flows
        spy_v = self.initial
        months = d.index.to_series().dt.month
        newm = (months != months.shift(1)) & (np.arange(len(months)) > 0)
        for r, nm in zip(d.spy_r.values, newm.values):
            if nm:
                spy_v += self.monthly
            spy_v *= 1 + r
        final = float(d.value.iloc[-1])
        naive_liq = final - c.tax_lt * max(final - self.contributions, 0)   # same pre-tax result, no TLH
        return {
            # pre-tax-neutral value of the tax management (what TLH added on top of this path)
            "tlh_value_liquidate": final - c.tax_lt * max(self.final_unrealized, 0) + fv - naive_liq,
            "tlh_value_hold": fv,
            "tlh_value_liquidate_pct_final": (final - c.tax_lt * max(self.final_unrealized, 0) + fv - naive_liq) / final,
            "after_tax_liquidate": final - c.tax_lt * max(self.final_unrealized, 0) + fv,
            "after_tax_hold": final + fv,
            "spy_final": spy_v,
            "spy_after_tax_liquidate": spy_v - c.tax_lt * max(spy_v - self.contributions, 0),
            "tax_flows_fv": fv,
            "wealthfront_fee_drag_end": final * (1 - (1 - 0.0009) ** yrs),
        }

    def by_year(self) -> pd.DataFrame:
        rz = self.realized.copy()
        d = self.daily
        if not len(rz):
            return pd.DataFrame()
        rz["year"] = pd.to_datetime(rz.sold).dt.year
        g = rz.assign(loss=rz.gain.clip(upper=0), harvest=np.where(rz.reason == "harvest", rz.gain.clip(upper=0), 0.0))
        out = g.groupby("year").agg(losses=("loss", "sum"), harvested=("harvest", "sum"))
        out["st_net"] = rz[rz.term == "ST"].groupby("year").gain.sum()
        out["lt_net"] = rz[rz.term == "LT"].groupby("year").gain.sum()
        out = out.fillna(0.0)
        out["avg_value"] = d.value.groupby(d.index.year).mean()
        out["harvest_yield"] = -out.harvested / out.avg_value
        out["tax_benefit"] = -(out.st_net * self.cfg.tax_st + out.lt_net * self.cfg.tax_lt)
        return out


def run(md: MarketData, cfg: Config, start: str, end: str | None = None,
        initial: float = 100_000.0, monthly_contribution: float = 0.0,
        rets: pd.DataFrame | None = None, lookback: int = 252) -> Result:
    eng = Engine(cfg)
    st = State(cash=initial)
    close = md.close
    rets = md.returns() if rets is None else rets
    dates = close.index[(close.index >= pd.Timestamp(start)) & (close.index <= pd.Timestamp(end or close.index[-1]))]
    ffclose = close.ffill()
    last_valid = close.apply(lambda c: c.last_valid_index())
    rows, trades = [], []
    prev_b: pd.Series | None = None
    contributed = initial
    month = None

    def sell(o: Order, px: float, today: date):
        p_net = px * (1 - cfg.cost_bps / 1e4)
        st.ledger.sell(o.sym, o.qty, p_net, today, mode="specific" if o.lot_ids else ("hifo" if cfg.lot_mode == "specific" else cfg.lot_mode),
                       lot_ids=o.lot_ids, reason=o.reason)
        st.cash += o.qty * p_net
        trades.append((today, o.sym, "sell", o.qty * px, o.reason))

    for i, d in enumerate(dates):
        today = d.date()
        row = close.loc[d]
        # --- cash flows
        if monthly_contribution and month is not None and d.month != month:
            st.cash += monthly_contribution
            contributed += monthly_contribution
        month = d.month
        held = st.ledger.symbols()
        drow = md.divs.loc[d]
        for s in held:
            dv = drow.get(s, 0.0)
            if dv and np.isfinite(dv):
                st.cash += st.ledger.qty(s) * dv
        # --- data ended for a held name (acquisition/delisting): sell at last price
        ffrow = ffclose.loc[d]
        for s in held:
            lv = last_valid.get(s)
            if lv is None or pd.isna(lv) or d > lv:
                o = Order(s, "sell", qty=st.ledger.qty(s), reason="delisted")
                sell(o, float(ffrow[s]), today)
        alive = last_valid.reindex(row.index) >= d
        row = row.where(row.notna() | ~alive, ffrow)   # one-off missing quotes: carry last price
        px = row.dropna()
        # names we hold with a one-off missing quote: value at last price, don't trade them
        gaps = [s for s in st.ledger.symbols() if s not in px.index]
        if gaps:
            px = pd.concat([px, ffrow[gaps]])
        b, coverage = bench_weights(md, d, row)
        hist = None
        if eng._rebuild_needed(today, frozenset(b.index), st):
            hist = rets.loc[:d].iloc[-lookback:]
        # --- engine
        for o in eng.plan_sells(today, px, b, st, hist):
            sell(o, float(px[o.sym]), today)
        sold_today = bool(trades) and trades[-1][0] == today
        for o in eng.plan_buys(today, px, b, st, force=(i == 0 or sold_today)):
            p = float(px[o.sym])
            qty = o.dollars / p
            st.ledger.buy(o.sym, qty, p * (1 + cfg.cost_bps / 1e4), today)
            st.cash -= o.dollars * (1 + cfg.cost_bps / 1e4)
            trades.append((today, o.sym, "buy", o.dollars, o.reason))
        if i % 20 == 0:
            st.ledger.prune(today)
        # --- record
        hv = st.holdings_value(px)
        value = float(hv.sum()) + st.cash
        r_idx = 0.0
        if prev_b is not None:
            r = rets.loc[d].reindex(prev_b.index).fillna(0.0)
            r_idx = float((prev_b * r).sum())
        prev_b = b
        rows.append((d, value, st.cash, len(hv), r_idx, coverage))

    daily = pd.DataFrame(rows, columns=["date", "value", "cash", "n_hold", "idx_r", "coverage"]).set_index("date")
    flows = pd.Series(0.0, index=daily.index)
    if monthly_contribution:
        m = daily.index.to_series().dt.month
        flows[(m != m.shift(1)) & (np.arange(len(m)) > 0)] = monthly_contribution
    daily["port_r"] = (daily.value - flows) / daily.value.shift(1) - 1
    daily["spy_r"] = md.spy_tr.reindex(daily.index).pct_change()
    daily = daily.iloc[1:].fillna(0.0)
    rz = pd.DataFrame([{**{k: getattr(r, k) for k in ("sym", "sold", "acquired", "qty", "proceeds", "basis", "reason", "washed")},
                        "gain": r.gain, "term": r.term} for r in st.ledger.realized])
    if not len(rz):
        rz = pd.DataFrame(columns=["sym", "sold", "acquired", "qty", "proceeds", "basis", "reason", "washed", "gain", "term"])
    tr = pd.DataFrame(trades, columns=["date", "sym", "side", "dollars", "reason"])
    px_end = close.loc[dates[-1]]
    unreal = sum(l.qty * (px_end.get(s, l.cost) - l.cost) for s, ls in st.ledger.lots.items() for l in ls)
    return Result(cfg, daily, rz, tr, float(unreal), contributed, initial, monthly_contribution)
