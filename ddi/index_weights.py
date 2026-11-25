"""Live S&P 500 weights from the SPY daily holdings file (float-adjusted, official weights).

SPY tracks the S&P 500 by full replication, so its holdings file is a free, daily proxy
for index constituents and weights. Saved to a local cache; parse failures raise loudly.
"""
from __future__ import annotations

import io
import urllib.request
from pathlib import Path

import pandas as pd

SPY_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/"
           "etfs/us/holdings-daily-us-en-spy.xlsx")


def parse_spy_holdings(xlsx: bytes) -> pd.Series:
    df = pd.read_excel(io.BytesIO(xlsx), skiprows=4)
    df = df[df["Ticker"].notna() & df["Weight"].notna()]
    df = df[~df["Ticker"].astype(str).str.upper().isin({"CASH_USD", "-", "CASH", "US DOLLAR"})]
    df = df[df["Ticker"].astype(str).str.match(r"^[A-Z][A-Z.]*$")]
    w = df.groupby(df["Ticker"].astype(str))["Weight"].sum().astype(float)
    if len(w) < 480 or not 95 < w.sum() < 101:
        raise ValueError(f"SPY holdings look wrong: {len(w)} names, weights sum {w.sum():.2f}")
    return w / w.sum()


def fetch(cache: Path | None = None) -> pd.Series:
    req = urllib.request.Request(SPY_URL, headers={"User-Agent": "diy-direct-index"})
    raw = urllib.request.urlopen(req, timeout=30).read()
    if cache:
        cache.write_bytes(raw)
    return parse_spy_holdings(raw)
