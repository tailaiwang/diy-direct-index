"""Portfolio construction: turn index weights into a holdable target for a $X account.

Methods
- full       : hold every constituent at cap weight (fractional shares), dropping positions
               smaller than `min_position` dollars and renormalising.
- topn       : hold the N largest names, renormalised to sum to 1 (simple, but has a
               systematic large-cap tilt).
- optimized  : hold N names (largest by weight) with weights chosen to minimise ex-ante
               tracking error against the full index under a statistical factor risk model.
               This is the standard "optimised sampling" used by direct-indexing providers.

Plus `whole_shares()` for brokers/accounts without fractional trading.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass
class RiskModel:
    """Statistical factor model: cov = B B' + diag(d). Built from trailing daily returns."""
    symbols: pd.Index
    B: np.ndarray      # (M, k) factor exposures scaled so cov = B B'
    d: np.ndarray      # (M,) specific variance

    @classmethod
    def fit(cls, rets: pd.DataFrame, k: int = 15, min_obs: int = 120) -> "RiskModel":
        r = rets.loc[:, rets.notna().sum() >= min_obs]
        r = r.fillna(0.0)
        X = r.values - r.values.mean(axis=0)
        T = X.shape[0]
        U, S, Vt = np.linalg.svd(X, full_matrices=False)
        k = min(k, len(S))
        B = Vt[:k].T * (S[:k] / np.sqrt(T - 1))
        resid = X - (U[:, :k] * S[:k]) @ Vt[:k]
        d = np.maximum(resid.var(axis=0, ddof=1), 1e-8)
        return cls(r.columns, B, d)

    def align(self, syms: pd.Index) -> tuple[np.ndarray, np.ndarray]:
        """Exposures for `syms`; names without history get the cross-sectional median
        specific variance and zero factor exposure (conservative)."""
        idx = self.symbols.get_indexer(syms)
        B = np.zeros((len(syms), self.B.shape[1]))
        d = np.full(len(syms), np.median(self.d))
        ok = idx >= 0
        B[ok] = self.B[idx[ok]]
        d[ok] = self.d[idx[ok]]
        return B, d

    def tracking_error(self, w: pd.Series, bench: pd.Series) -> float:
        """Annualised ex-ante tracking error of w vs bench."""
        syms = bench.index.union(w.index)
        a = w.reindex(syms, fill_value=0.0).values - bench.reindex(syms, fill_value=0.0).values
        B, d = self.align(syms)
        var = float(np.sum((B.T @ a) ** 2) + np.sum(d * a * a))
        return float(np.sqrt(var * TRADING_DAYS))


def _drop_small(w: pd.Series, value: float, min_position: float) -> pd.Series:
    w = w[w > 0]
    while True:
        keep = w * value >= min_position
        if keep.all() or not keep.any():
            return w / w.sum()
        w = w[keep]
        w = w / w.sum()


def full(bench: pd.Series, value: float, min_position: float = 0.0) -> pd.Series:
    return _drop_small(bench / bench.sum(), value, min_position)


def topn(bench: pd.Series, n: int, value: float = 1.0, min_position: float = 0.0) -> pd.Series:
    w = bench.sort_values(ascending=False).iloc[:n]
    return _drop_small(w / w.sum(), value, min_position)


def optimized(bench: pd.Series, risk: RiskModel, n: int, value: float = 1.0,
              min_position: float = 0.0, max_active: float = 0.02,
              exclude: set[str] | None = None, held: set[str] | None = None,
              buffer: float = 1.5) -> pd.Series:
    """Min tracking-error weights over the N largest eligible names.

    min  (w-b)' (B B' + D) (w-b)
    s.t. sum w = 1, w >= 0, |w_i - b_i| <= max_active for held names
    """
    import cvxpy as cp

    b = (bench / bench.sum()).sort_values(ascending=False)
    eligible = b.index if not exclude else b.index.difference(list(exclude), sort=False)
    # max names we can afford at the minimum position size
    n_afford = int(value // max(min_position, 1e-9)) if min_position > 0 else n
    n = max(1, min(n, len(eligible), n_afford))
    ranked = b.loc[eligible].sort_values(ascending=False).index
    if held:
        # sticky selection: keep held names still ranked inside n*buffer, fill the rest by size.
        # Without this, every re-solve swaps names at the margin and forces gain-realising sells.
        keep = [s for s in ranked[:int(n * buffer)] if s in held][:n]
        fill = [s for s in ranked if s not in set(keep)][: n - len(keep)]
        sel = pd.Index(sorted(keep + fill, key=lambda s: -b[s]))
    else:
        sel = ranked[:n]

    B, d = risk.align(b.index)
    pos = b.index.get_indexer(sel)
    bvec = b.values
    x = cp.Variable(len(sel))
    Bs, ds, bs = B[pos], d[pos], bvec[pos]
    fac = Bs.T @ x - B.T @ bvec
    spec = cp.sum(cp.multiply(ds, cp.square(x - bs)))
    cons = [cp.sum(x) == 1, x >= 0, cp.abs(x - bs) <= max(max_active, 2 * bs.max())]
    # tiny scale-up for solver conditioning
    prob = cp.Problem(cp.Minimize(1e4 * (cp.sum_squares(fac) + spec)), cons)
    try:
        prob.solve(solver=cp.CLARABEL)
    except Exception:
        prob.solve(solver=cp.OSQP)
    if x.value is None:
        return topn(b.loc[eligible], n, value, min_position)
    w = pd.Series(np.clip(x.value, 0, None), index=sel)
    w = w[w > 1e-7]
    w = w / w.sum()
    if min_position > 0 and (w * value < min_position).any():
        w = _drop_small(w, value, min_position)
    return w


def whole_shares(weights: pd.Series, prices: pd.Series, value: float) -> pd.Series:
    """Round a dollar target down to whole shares, then greedily spend leftover cash on the
    most-underweight names that fit. Returns share counts."""
    px = prices.reindex(weights.index)
    tgt = weights * value
    sh = np.floor(tgt / px)
    cash = value - float((sh * px).sum())
    gap = (tgt - sh * px)
    for sym in gap.sort_values(ascending=False).index:
        if px[sym] <= cash:
            sh[sym] += 1
            cash -= px[sym]
    return sh[sh > 0]


def build(method: str, bench: pd.Series, value: float, *, n: int | None = None,
          risk: RiskModel | None = None, min_position: float = 0.0,
          exclude: set[str] | None = None, held: set[str] | None = None) -> pd.Series:
    if method == "optimized":
        if risk is None:
            raise ValueError("optimized needs a RiskModel")
        # benchmark stays whole: excluded names are simply not eligible to hold
        return optimized(bench, risk, n or 100, value, min_position, exclude=exclude, held=held)
    if exclude:
        bench = bench.drop(index=[s for s in exclude if s in bench.index])
    if method == "full":
        return full(bench, value, min_position)
    if method == "topn":
        if held:  # same stickiness for top-N
            ranked = bench.sort_values(ascending=False)
            keep = [s for s in ranked.index[:int((n or 100) * 1.5)] if s in held][: n or 100]
            fill = [s for s in ranked.index if s not in set(keep)][: (n or 100) - len(keep)]
            w = bench[keep + fill]
            return _drop_small(w / w.sum(), value, min_position)
        return topn(bench, n or 100, value, min_position)
    raise ValueError(method)
