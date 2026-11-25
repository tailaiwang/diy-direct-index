"""The decision engine. Broker-agnostic and deterministic: same inputs -> same orders.

One trading day is two phases so that sells settle into cash before buys are sized:

    sells = engine.plan_sells(today, prices, bench_w, state)
    ...execute sells, update state.ledger / state.cash...
    buys  = engine.plan_buys(today, prices, bench_w, state)
    ...execute buys...

The backtester and the live Robinhood runner both drive exactly this loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

import numpy as np
import pandas as pd

from . import construct
from .lots import Ledger, LotMode, WASH_DAYS


@dataclass
class Config:
    # --- construction ---
    method: Literal["full", "topn", "optimized"] = "full"
    n: int | None = None                  # names for topn / optimized
    min_position: float = 5.0             # $ - smallest position worth holding
    min_order: float = 1.0                # $ - Robinhood dollar-based minimum
    exclude: frozenset[str] = frozenset() # personal restricted list (employer stock, ESG...)
    rebuild_days: int = 63                # re-solve targets quarterly (and on index changes)
    # --- tax-loss harvesting ---
    tlh: bool = True
    harvest_threshold: float = 0.05       # harvest a lot once it is down >= 5% vs basis
    min_harvest: float = 1.0              # $ - absolute floor on a harvestable loss...
    min_harvest_frac: float = 0.0001      # ...and at least 1bp of the portfolio ($10 at $100k, $0.50 at $5k)
    lot_mode: LotMode = "specific"        # 'specific' | 'hifo' | 'fifo' (what the broker supports)
    replacement_k: int = 3                # spread a harvested name's weight over k correlated names
    rep_mult: float = 1.0                 # a replacement may receive up to 1.0x its own base weight,
    rep_abs: float = 0.001                # but at least 0.10% of the portfolio
    rep_max: float = 0.003                # and never more than 0.30% (stops mega-caps soaking it up)
    max_sleeve: float = 0.05              # stop harvesting into the sleeve beyond 5% of the portfolio
    sleeve: str | None = None             # optional overflow ETF (e.g. "SPLG"). Default None: only
                                          # harvest what correlated replacements can absorb.
    # --- rebalancing ---
    rebalance_days: int = 21              # scheduled drift check
    active_share_budget: float = 0.10     # look-through active share above which LT gains may be trimmed
    free_gain: float = 0.005              # lots with gain <= 0.5% of proceeds are 'free' to trim
    min_trade_frac: float = 0.0005        # skip trades smaller than 0.05% of the portfolio...
    allow_st_gain_trims: bool = False     # never realise short-term gains just to rebalance
    cash_deploy_threshold: float = 0.002  # deploy cash when > 0.2% of portfolio
    force_sell_removed: bool = True       # sell names that leave the index (like an index fund)
    # --- costs / taxes (backtest + reporting) ---
    cost_bps: float = 2.0                 # half-spread + market impact per trade
    tax_st: float = 0.408                 # 37% + 3.8% NIIT
    tax_lt: float = 0.238                 # 20% + 3.8% NIIT


@dataclass
class Order:
    sym: str
    side: Literal["buy", "sell"]
    qty: float = 0.0           # shares (sells)
    dollars: float = 0.0       # notional (buys)
    reason: str = ""
    lot_ids: list[int] | None = None


@dataclass
class Replacement:
    until: date
    weights: dict[str, float]   # replacement sym -> fraction of the harvested weight


@dataclass
class State:
    ledger: Ledger = field(default_factory=Ledger)
    cash: float = 0.0
    replacements: dict[str, Replacement] = field(default_factory=dict)
    base_units: pd.Series | None = None      # target weight / price at last rebuild
    last_rebuild: date | None = None
    last_rebalance: date | None = None
    last_universe: frozenset[str] = frozenset()
    risk: construct.RiskModel | None = None
    corr: pd.DataFrame | None = None
    trading_day: int = 0
    rep_added: dict[str, float] = field(default_factory=dict)  # weight added to replacements
    _px: pd.Series | None = None                                # today's prices (set by plan_sells)
    _V: float = 0.0

    def holdings_value(self, prices: pd.Series) -> pd.Series:
        syms = self.ledger.symbols()
        return pd.Series({s: self.ledger.qty(s) * prices[s] for s in syms}, dtype=float)

    def value(self, prices: pd.Series) -> float:
        return float(self.holdings_value(prices).sum()) + self.cash


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------ targets
    def _rebuild_needed(self, today: date, universe: frozenset[str], state: State) -> bool:
        if state.base_units is None or state.last_rebuild is None:
            return True
        if universe != state.last_universe:
            return True
        return (today - state.last_rebuild).days >= self.cfg.rebuild_days * 365 / 252

    def targets(self, today: date, prices: pd.Series, bench_w: pd.Series, state: State,
                hist_returns: pd.DataFrame | None = None) -> pd.Series:
        """Target weights for today, after wash-sale replacements."""
        cfg = self.cfg
        universe = frozenset(bench_w.index)
        if self._rebuild_needed(today, universe, state):
            if hist_returns is not None and len(hist_returns) >= 60:
                cols = [c for c in hist_returns.columns if c in universe]
                h = hist_returns[cols]
                if cfg.method == "optimized":
                    state.risk = construct.RiskModel.fit(h)
                state.corr = h.fillna(0.0).corr()
            value = max(state.value(prices), 1.0)
            w = construct.build(cfg.method, bench_w, value, n=cfg.n, risk=state.risk,
                                min_position=cfg.min_position, exclude=set(cfg.exclude),
                                held=set(state.ledger.symbols()))
            state.base_units = w / prices.reindex(w.index)
            state.last_rebuild = today
            state.last_universe = universe
        w = state.base_units * prices.reindex(state.base_units.index)
        w = w.dropna()
        w = w[w.index.isin(universe)]
        w = w / w.sum()
        # expire old replacements, then move restricted weight onto replacements
        for s in [s for s, r in state.replacements.items() if r.until < today]:
            del state.replacements[s]
        base = w.copy()
        w = w.copy()
        restricted = set(state.replacements)
        overflow = 0.0
        added: dict[str, float] = {}
        V = state.value(prices)
        for s, rep in state.replacements.items():
            # keep whatever is still held (e.g. older lots that were not harvested);
            # only the part we could not buy back moves to replacements
            held = state.ledger.qty(s) * float(prices.get(s, 0.0)) / V if V > 0 else 0.0
            b0 = float(base.get(s, 0.0))
            keep = min(b0, held)
            moved = b0 - keep
            if s in w.index:
                w[s] = keep
            if moved <= 0:
                continue
            live = {r: f for r, f in rep.weights.items()
                    if r in universe and r not in restricted}
            tot = sum(live.values())
            for r, f in live.items():
                cap = max(min(cfg.rep_mult * float(base.get(r, 0.0)), cfg.rep_max), cfg.rep_abs)
                give = min(moved * f / tot, cap - added.get(r, 0.0))
                if give > 0:
                    added[r] = added.get(r, 0.0) + give
                    w[r] = w.get(r, 0.0) + give
                    moved -= give
            overflow += max(moved, 0.0)
        state.rep_added = added
        if overflow > 1e-12:
            if cfg.sleeve and cfg.sleeve in prices.index and state.ledger.can_buy(cfg.sleeve, today):
                w[cfg.sleeve] = w.get(cfg.sleeve, 0.0) + overflow
            # else: dropping it and renormalising spreads it pro-rata over everything held
        w = w[w > 0]
        return w / w.sum()

    def _pick_replacements(self, sym: str, today: date, universe: pd.Index, state: State,
                           targets: pd.Series) -> dict[str, float]:
        k = self.cfg.replacement_k
        bad = set(self.cfg.exclude) | {sym} | set(state.replacements)
        # only names the construction actually wants to hold (matters for optimized / top-N:
        # a replacement outside the target set would later be trimmed at a gain)
        in_target = set(state.base_units.index) if state.base_units is not None else set(universe)
        cands = [c for c in universe if c in in_target and c not in bad and state.ledger.can_buy(c, today)]
        if state.corr is not None and sym in state.corr.index:
            c = state.corr.loc[sym].reindex(cands).dropna().sort_values(ascending=False)
            picks = list(c.index[:k])
        else:
            picks = list(targets.reindex(cands).dropna().sort_values(ascending=False).index[:k])
        return {p: 1.0 / len(picks) for p in picks} if picks else {}

    # ------------------------------------------------------------------ phase 1
    def plan_sells(self, today: date, prices: pd.Series, bench_w: pd.Series, state: State,
                   hist_returns: pd.DataFrame | None = None) -> list[Order]:
        cfg, L = self.cfg, state.ledger
        state._px = prices
        state._V = state.value(prices)
        universe = bench_w.index
        tgt = self.targets(today, prices, bench_w, state, hist_returns)
        orders: list[Order] = []
        sold: set[str] = set()

        # 1) index removals / personal exclusions: sell everything
        for s in L.symbols():
            if s == cfg.sleeve:
                continue
            if (cfg.force_sell_removed and s not in universe) or s in cfg.exclude:
                orders.append(Order(s, "sell", qty=L.qty(s), reason="removed"))
                sold.add(s)

        # 2) tax-loss harvesting
        if cfg.tlh:
            V = max(state.value(prices), 1e-9)
            sleeve_w = (L.qty(cfg.sleeve) * float(prices.get(cfg.sleeve, 0.0)) / V) if cfg.sleeve else 0.0
            cands = []
            for s in L.symbols():
                if s in sold or s == cfg.sleeve or s not in prices.index or not np.isfinite(prices[s]):
                    continue
                o = self._harvest(s, float(prices[s]), today, state)
                if o is not None:
                    basis = sum(l.qty * l.cost for l in L.lots[s]) or 1.0
                    cands.append(((L.qty(s) * float(prices[s])) / basis, s, o))
            cands.sort(key=lambda t: t[0])          # deepest losses first
            for _, s, o in cands:
                if cfg.sleeve and sleeve_w > cfg.max_sleeve:
                    break
                if o is not None:
                    rep = self._pick_replacements(s, today, universe, state, tgt)
                    if not cfg.sleeve:
                        # no ETF sleeve: only harvest if replacements can absorb the weight
                        bw = state.base_units * prices.reindex(state.base_units.index)
                        bw = bw / bw.sum()
                        room = sum(max(min(cfg.rep_mult * float(bw.get(r, 0.0)), cfg.rep_max), cfg.rep_abs)
                                   - state.rep_added.get(r, 0.0) for r in rep)
                        need = o.qty * float(prices[s]) / max(state.value(prices), 1e-9)
                        if room < need:
                            continue
                        for r in rep:  # reserve capacity for later harvests today
                            state.rep_added[r] = state.rep_added.get(r, 0.0) + need / len(rep)
                    orders.append(o)
                    sold.add(s)
                    if cfg.sleeve:  # conservative: assume the whole sale may overflow to the sleeve
                        sleeve_w += o.qty * float(prices[s]) / V
                    state.replacements[s] = Replacement(today + timedelta(days=WASH_DAYS + 1), rep)

        # 3) tax-aware drift trim (scheduled)
        due = state.last_rebalance is None or (today - state.last_rebalance).days >= cfg.rebalance_days * 365 / 252
        if due:
            tgt = self.targets(today, prices, bench_w, state)  # include new replacements
            orders += self._trim(today, prices, tgt, state, skip=sold, bench_w=bench_w)
            state.last_rebalance = today
        return orders

    def _harvest(self, s: str, px: float, today: date, state: State) -> Order | None:
        cfg, L = self.cfg, state.ledger
        lots = L.lots.get(s, [])
        if not lots:
            return None
        thr = 1 - cfg.harvest_threshold
        min_loss = max(cfg.min_harvest, cfg.min_harvest_frac * state._V)
        if cfg.lot_mode in ("specific", "hifo"):
            losers = [l for l in lots if px <= l.cost * thr]
            if not losers:
                return None
            selling_all = len(losers) == len(lots)
            if not L.can_sell_at_loss(s, today, selling_all=selling_all):
                return None
            loss = sum(l.qty * (l.cost - px) for l in losers)
            if loss < min_loss:
                return None
            qty = sum(l.qty for l in losers)
            if cfg.lot_mode == "specific":
                return Order(s, "sell", qty=qty, reason="harvest", lot_ids=[l.id for l in losers])
            # HIFO: selling qty of the highest-cost lots == the loss lots
            return Order(s, "sell", qty=qty, reason="harvest")
        # FIFO broker: only whole-position harvesting is safe (lot order irrelevant)
        basis = L.basis(s)
        qty = L.qty(s)
        if qty * px > basis * thr or basis - qty * px < min_loss:
            return None
        if not L.can_sell_at_loss(s, today, selling_all=True):
            return None
        return Order(s, "sell", qty=qty, reason="harvest")

    def _lookthrough(self, w: pd.Series, bench_w: pd.Series) -> pd.Series:
        """Replace the sleeve ETF by its holdings (~ index weights) so it is not 'active'."""
        sl = self.cfg.sleeve
        if not sl or sl not in w.index:
            return w
        out = w.drop(sl)
        return out.add(bench_w * float(w[sl]), fill_value=0.0)

    def _trim(self, today: date, prices: pd.Series, tgt: pd.Series, state: State,
              skip: set[str], bench_w: pd.Series) -> list[Order]:
        """Tax-aware drift control.

        Tier 1 (every scheduled rebalance): trim overweights - and any excess sleeve ETF -
          using only lots that are at a loss or have a negligible gain (<= free_gain).
        Tier 2 (only if look-through active share > active_share_budget): also sell
          long-term gain lots, cheapest tax cost per $ first. Short-term gains are never
          realised for rebalancing unless allow_st_gain_trims.
        """
        cfg, L = self.cfg, state.ledger
        V = state.value(prices)
        if V <= 0:
            return []
        hv = state.holdings_value(prices)
        cur = hv / V
        sl = cfg.sleeve
        sleeve_excess = 0.0
        if sl:
            sleeve_excess = float(cur.get(sl, 0.0) - tgt.get(sl, 0.0))
        cur_lt = self._lookthrough(cur, bench_w)
        tgt_lt = self._lookthrough(tgt, bench_w)
        allsyms = cur_lt.index.union(tgt_lt.index)
        diff = cur_lt.reindex(allsyms, fill_value=0.0) - tgt_lt.reindex(allsyms, fill_value=0.0)
        active = float(diff.clip(lower=0).sum())
        over = {s: float(diff[s]) * V for s in diff[diff > 0].index}
        if sl and sleeve_excess > 0:
            over[sl] = sleeve_excess * V
        min_trade = max(cfg.min_order, cfg.min_trade_frac * V)
        tier2_need = (active - cfg.active_share_budget / 2) * V if active > cfg.active_share_budget else 0.0

        cands = []  # (tax cost per $ sold, tier, sym, lot)
        for s in over:
            if s in skip or s not in L.lots:
                continue
            px = float(prices[s])
            for l in L.lots[s]:
                g = (px - l.cost) / px                        # gain per $ of proceeds
                lt = (today - l.acquired).days > 365
                if g < 0 and not L.can_sell_at_loss(s, today):
                    continue
                if g <= cfg.free_gain:
                    cands.append((g, 1, s, l))
                elif tier2_need > 0 and (lt or cfg.allow_st_gain_trims):
                    cands.append((g * (cfg.tax_lt if lt else cfg.tax_st), 2, s, l))
        cands.sort(key=lambda t: (t[1], t[0]))
        picked: dict[str, list] = {}
        for cost, tier, s, l in cands:
            if tier == 2 and tier2_need <= 0:
                break
            px = float(prices[s])
            room = over.get(s, 0.0)
            if room < min_trade:
                continue
            q = min(l.qty, room / px, (tier2_need / px) if tier == 2 else np.inf)
            if q * px < min_trade:
                continue
            picked.setdefault(s, []).append((l.id, q))
            over[s] -= q * px
            if tier == 2:
                tier2_need -= q * px
        orders: list[Order] = []
        for s, lots in picked.items():
            qty = sum(q for _, q in lots)
            orders.append(Order(s, "sell", qty=qty, reason="rebalance",
                                lot_ids=[i for i, _ in lots] if cfg.lot_mode == "specific" else None))
        return orders

    # ------------------------------------------------------------------ phase 2
    def plan_buys(self, today: date, prices: pd.Series, bench_w: pd.Series, state: State,
                  force: bool = False) -> list[Order]:
        cfg, L = self.cfg, state.ledger
        V = state.value(prices)
        if V <= 0 or state.cash < cfg.min_order:
            return []
        if not force and state.cash < cfg.cash_deploy_threshold * V:
            return []
        tgt = self.targets(today, prices, bench_w, state)
        hv = state.holdings_value(prices).reindex(tgt.index, fill_value=0.0)
        under = (tgt * V - hv).clip(lower=0)
        buyable = [s for s in under.index if L.can_buy(s, today) and np.isfinite(prices.get(s, np.nan))]
        under = under.reindex(buyable).fillna(0.0)
        budget = state.cash * (1 - cfg.cost_bps / 1e4) - 1e-6
        min_trade = max(cfg.min_order, cfg.min_trade_frac * V)
        alloc = pd.Series(0.0, index=under.index)
        left = budget
        for sym, gap in under.sort_values(ascending=False).items():
            if left < min_trade or gap < min_trade:
                break
            a = min(gap, left)
            alloc[sym] = a
            left -= a
        if left >= min_trade:  # gaps filled: spread the rest pro-rata to target
            t = tgt.reindex(buyable).fillna(0.0)
            t = t[t * left >= min_trade] if (t * left >= min_trade).any() else t.nlargest(1)
            alloc = alloc.add(t / t.sum() * left, fill_value=0.0)
        alloc = alloc[alloc >= cfg.min_order]
        return [Order(s, "buy", dollars=float(a), reason="deploy")
                for s, a in alloc.items() if a >= cfg.min_order]
