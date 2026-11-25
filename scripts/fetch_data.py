"""Download backtest data: point-in-time S&P 500 membership + daily prices/dividends.

Sources
- Membership: github.com/fja05680/sp500 (community-maintained, point-in-time).
- Prices/dividends/shares: Yahoo Finance via yfinance (free, delisted names often missing).
"""
import sys, time, json
from pathlib import Path
import pandas as pd
import yfinance as yf

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
START = sys.argv[1] if len(sys.argv) > 1 else "2014-01-01"


def membership_tickers(since: str) -> list[str]:
    df = pd.read_csv(RAW / "components.csv", parse_dates=["date"])
    df = df[df.date >= pd.Timestamp(since) - pd.Timedelta(days=400)]
    s = set()
    for t in df.tickers:
        s.update(t.split(","))
    return sorted(s)


def yahoo(t: str) -> str:
    return t.replace(".", "-")


def main():
    tickers = membership_tickers(START)
    print(f"{len(tickers)} tickers ever in index since {START}")
    syms = [yahoo(t) for t in tickers] + ["SPY", "^SP500TR", "^GSPC"]
    frames = []
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        df = yf.download(chunk, start=START, auto_adjust=False, actions=True,
                         group_by="column", progress=False, threads=True)
        frames.append(df)
        print(f"  prices {i + len(chunk)}/{len(syms)}", flush=True)
        time.sleep(1)
    df = pd.concat(frames, axis=1)
    close = df["Close"].dropna(how="all", axis=1)
    divs = df["Dividends"].reindex(columns=close.columns).fillna(0.0)
    close.columns = [c.replace("-", ".") if not c.startswith("^") else c for c in close.columns]
    divs.columns = close.columns
    close.to_parquet(RAW / "close.parquet")
    divs.to_parquet(RAW / "dividends.parquet")
    print(f"saved close for {close.shape[1]} symbols, {close.shape[0]} days")

    shares = {}
    for n, t in enumerate(c for c in close.columns if not c.startswith("^") and c != "SPY"):
        for attempt in range(3):
            try:
                v = yf.Ticker(yahoo(t)).fast_info["shares"]
                if v:
                    shares[t] = float(v)
                break
            except Exception:
                time.sleep(1 + attempt)
        if n % 100 == 0:
            print(f"  shares {n}", flush=True)
    (RAW / "shares.json").write_text(json.dumps(shares, indent=0))
    print(f"saved shares for {len(shares)} symbols")


if __name__ == "__main__":
    main()
