"""Historical data for backtests: point-in-time membership, prices, dividends, caps.

Known limitations (reported alongside every backtest):
- Survivorship: Yahoo has no prices for many delisted/acquired names, so they drop out of
  the investable universe. We report the share of index members we could price each day.
- Weights: true S&P weights are float-adjusted caps; we use (latest shares outstanding x
  price), which ignores buybacks/issuance/float. We therefore measure tracking against a
  *synthetic* cap-weighted index built from the same data (isolates construction + TLH
  effects) and, separately, against SPY total return (what you actually care about).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"


@dataclass
class MarketData:
    close: pd.DataFrame          # split-adjusted close (not dividend-adjusted)
    divs: pd.DataFrame           # cash dividend per share on ex-date
    shares: pd.Series            # shares outstanding (latest)
    members: pd.Series           # date -> frozenset(tickers), at change points
    spy_tr: pd.Series            # SPY total-return index

    def members_on(self, d: pd.Timestamp) -> frozenset[str]:
        i = self.members.index.searchsorted(d, side="right") - 1
        return self.members.iloc[max(i, 0)]

    def returns(self) -> pd.DataFrame:
        prev = self.close.shift(1)
        return (self.close + self.divs) / prev - 1.0


# Yahoo series known to be corrupt for the membership window (checked by hand).
BAD_SYMBOLS = {"PARA"}


def _shares(raw: Path, close: pd.DataFrame) -> pd.Series:
    """Index-consistent share counts.

    Current members: SPY 'Shares Held' (proportional to S&P's float-adjusted index shares,
    correct per share class, e.g. GOOGL vs GOOG). Former members: Yahoo shares outstanding,
    rescaled to SPY units by the median ratio on the overlap. Yahoo alone double-counts
    multi-class companies, so SPY is preferred wherever it exists."""
    yahoo = pd.Series(json.loads((raw / "shares.json").read_text()), dtype=float)
    spy_file = raw / "spy_holdings.xlsx"
    if not spy_file.exists():
        return yahoo
    h = pd.read_excel(spy_file, skiprows=4)
    h = h[h["Ticker"].notna() & h["Shares Held"].notna()]
    spy = h.groupby(h["Ticker"].astype(str))["Shares Held"].sum().astype(float)
    both = spy.index.intersection(yahoo.index)
    k = float((yahoo[both] / spy[both]).median())
    out = (yahoo / k)
    out.loc[spy.index.intersection(close.columns)] = spy.loc[spy.index.intersection(close.columns)]
    # multi-class names that left the index keep Yahoo's (possibly double-counted) figure;
    # GOOG/GOOGL, BRK.B, FOX/FOXA, NWS/NWSA are current members so they come from SPY.
    return out


def load(raw: Path = RAW) -> MarketData:
    close = pd.read_parquet(raw / "close.parquet")
    close = close.drop(columns=[c for c in BAD_SYMBOLS if c in close.columns])
    divs = pd.read_parquet(raw / "dividends.parquet").reindex_like(close).fillna(0.0)
    close.index = pd.to_datetime(close.index).tz_localize(None)
    divs.index = close.index
    shares = _shares(raw, close)
    comp = pd.read_csv(raw / "components.csv", parse_dates=["date"])
    members = pd.Series([frozenset(t.split(",")) for t in comp.tickers], index=comp.date).sort_index()
    spy = close["SPY"]
    spy_r = ((spy + divs["SPY"]) / spy.shift(1) - 1).fillna(0.0)
    spy_tr = (1 + spy_r).cumprod()
    return MarketData(close, divs, shares, members, spy_tr)


def bench_weights(md: MarketData, d: pd.Timestamp, row: pd.Series | None = None) -> tuple[pd.Series, float]:
    """Cap weights over index members we can price on day d, and the fraction of
    members (by count) that are priceable."""
    mem = md.members_on(d)
    px = md.close.loc[d] if row is None else row
    syms = [s for s in mem if s in px.index and np.isfinite(px[s]) and s in md.shares.index]
    cap = px[syms] * md.shares[syms]
    cap = cap[cap > 0]
    return cap / cap.sum(), len(cap) / max(len(mem), 1)
