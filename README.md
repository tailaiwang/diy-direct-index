# diy-direct-index

DIY S&P 500 direct indexing with tax-loss harvesting (TLH), driven through
[Robinhood's agentic-trading MCP server](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).
It does roughly what Wealthfront S&P 500 Direct does (0.09%/yr), for $0 advisory fee, in an
account you control.

> **Not investment or tax advice.** This is hobbyist software that can place real orders.
> Wash-sale and tax treatment here is a model, not a ruling. Read the risks section.

## Backtest results

**Short version:** before tax, direct indexing tracks the S&P 500 and VOO almost exactly. After tax,
harvesting put it about $13–16k ahead of VOO per $100k over 10 years. Every direct-indexing provider
gets that same benefit, so choosing between them mostly comes down to the fee. Over 10 years,
Wealthfront's 0.09% cost about $3.4k per $100k. That's all DIY saves, and in exchange you own the bugs.

### Last 10 years: $100k invested Nov 28, 2016, valued Nov 24, 2026

| Ending value | Before tax | After tax, sell everything | After tax, never sell |
|---|---|---|---|
| SPY | $380.2k | $313.5k | $380.2k |
| VOO | $384.1k | $316.5k | $384.1k |
| **DIY on Robinhood (this repo, $0 fee)** | **$382.5k** | **$330.4k** | **$401.7k** |
| Wealthfront S&P 500 Direct (0.09%) | $379.1k | $327.0k | $398.3k |
| Frec (0.10%) | $378.7k | $326.6k | $397.9k |
| Public (0.19%) | $375.4k | $323.2k | $394.5k |
| Schwab / Fidelity direct indexing (~0.40%) | $367.6k | $315.4k | $386.7k |

How to read it:
- **SPY and VOO** are their actual total returns with dividends reinvested. VOO beats SPY by its
  lower fee.
- **DIY** is the index's actual return with no fund fee, plus what harvesting added compared with a
  matched no-harvest run of the same engine on the same data.
  - Over the decade it harvested $34k of losses (1.5% of the portfolio a year).
  - That was worth +$13.0k after tax if you sell everything at the end, or +$16.4k if you never
    sell. "Never sell" means a step-up at death or donating the shares, so the deferred tax is never paid.
  - Harvesting's extra trades cost about $2.7k before tax, which is why DIY ends slightly behind VOO
    before tax.
- **Other providers** are modeled as the same strategy minus their advisory fee. None of these
  numbers are their real track records: several products didn't exist for most of the decade, and
  each harvests differently. Fees come from third-party roundups as of Nov 2026, conflict between
  sources, and vary by tier, so check current pricing.
- **Taxes:** 40.8% short-term, 23.8% long-term. Harvested losses are assumed to offset gains you
  have elsewhere. Dividend taxes are ignored because they're about the same for every row.
- **Noise:** harvesting is path-dependent, and small code changes moved results by about ±$5k per $100k.

Reproduce with `python scripts/compare_10y.py`. The full write-up with charts is in
[`docs/report.html`](docs/report.html).

### By start date ($100k, through Nov 2026)

Harvesting is worth the most in a portfolio's first year or two and during drawdowns. Once every
lot is above its cost basis, there's little left to harvest.

| Start | Losses harvested in year 1 | TLH value, sell everything | TLH value, never sell | Wealthfront fee over the same period |
|---|---|---|---|---|
| Jan 2016 | 5.6% | $19.9k | $24.7k | $4.4k |
| Jan 2018 | 8.5% | $16.9k | $22.2k | $2.5k |
| Jan 2020 | 12.4% | $14.2k | $20.0k | $1.5k |
| Jan 2022 | 16.6% | $6.8k | $11.1k | $0.7k |
| Jan 2024 | 3.5% | $2.9k | $5.3k | $0.4k |

Harvesting value scales with account size: about $0.9k at $5k, $4.8k at $25k and $193k at $1M
(2016 start).

### Other ways to build the portfolio ($100k, Jan 2016 start)

| Construction | Avg. holdings | Tracking error today* | TLH value, sell everything | Long-term gains realized | Trades / yr |
|---|---|---|---|---|---|
| **All ~500, fractional (default)** | 332 | ~0% | $19.9k | $6.1k | 339 |
| Optimized 200 | 187 | 0.32% | $21.9k | $31.9k | 301 |
| Top 200 by size | 174 | 1.2% | $19.7k | $1.2k | 235 |
| Top 100 by size | 94 | 2.6% | $18.1k | none | 124 |
| Optimized 100 | 97 | 0.8% | $18.5k | $104.9k | 301 |
| Top 50 by size | 48 | 4.4% | $10.2k | none | 53 |

\* Expected tracking error vs the S&P 500 from the risk model, for a portfolio built on Nov 24, 2026.

- Fewer names track worse, and the top-N lists tilt toward large caps.
- Optimized portfolios realize far more long-term gains because each quarterly re-solve moves weight
  between names you hold.
- With fractional shares, holding every name is the simplest and best option, from about $5k up.

Other settings tested:
- **Broker FIFO lot selling** (whole-position harvesting only): $16.7k.
- **3% drift budget:** $12.9k. Rebalancing that tightly realizes gains that cancel the harvesting.
- **ETF overflow sleeve:** $13–16k.

All results are in [`results/report.json`](results/report.json).

### Data caveat

The backtest uses free Yahoo data, which has no prices for about 25% of 2016 index members (mostly
acquired or delisted companies). The raw engine output ended at $415k, far ahead of SPY, but the
no-harvest run on the same data shows the same gap. So it comes from the missing data, not the
strategy. That's why every number above is measured against a matched no-harvest run rather than
taken from the raw output.

## How it works

```
          ┌───────────── Robinhood MCP (agent.robinhood.com/mcp/trading) ─────────────┐
          │ get_equity_tax_lots · get_equity_quotes · get_equity_orders (all accts)   │
          │ review_equity_order · place_equity_order                                  │
          └───────▲───────────────────────────────────────────────┬───────────────────┘
                  │ execute plan (agent or headless client)       │ snapshot.json
          ┌───────┴───────┐                               ┌───────▼───────┐
          │  plan.json    │◄──────── ddi plan ────────────│  ddi engine   │◄── SPY holdings
          │ (sha256,      │   deterministic, unit-tested   │ (lots, wash,  │    (index weights)
          │  guardrails)  │                                │  TLH, drift)  │
          └───────────────┘                               └───────────────┘
```

The engine is pure Python and never talks to the broker. An agent (Claude Code with the
Robinhood MCP connected) or the headless MCP client gathers a snapshot, the engine writes a
plan, and the plan is executed order by order with `review_equity_order` first. The LLM
only carries the plan to the broker. It never decides what to trade.

### Each trading day
1. **Sells**
   - Sell names that left the index.
   - Harvest lots that are ≥5% below basis (and the loss is ≥1bp of the portfolio), deepest losses first. Skip any name bought
     in the last 30 days in *any* account you report.
   - Move each harvested name's weight to its 3 most-correlated stocks, capped at
     0.1–0.3% of the portfolio each. Harvest only if they have room. An optional S&P ETF
     "sleeve" (`sleeve: "SPLG"`, capped by `max_sleeve`) can absorb the overflow instead;
     backtests found it harvests a bit more but tracks worse.
   - Tax-aware drift trims. Only lots at a loss or ≤0.5% gain are sold, unless
     look-through drift exceeds 10%. Then long-term lots are sold, cheapest first.
     Short-term gains are never sold to rebalance.
2. **Buys**: deploy cash into the largest underweights. Restricted names are skipped for 31 days.

### Construction methods (`method`)
| method | what | when |
|---|---|---|
| `full` | every constituent, cap-weighted, fractional shares, drop positions < `min_position` | default; works from ~$5k thanks to fractional shares |
| `optimized` | N names, weights minimise ex-ante tracking error (factor risk model + QP) | fewer orders/lots; N=200 ≈ 0.3% TE |
| `topn` | N largest, renormalised | simple baseline, has a size tilt (avoid) |

## Quick start

```bash
uv venv -p 3.12 && uv pip install -e ".[backtest,mcp,dev]"
pytest -q                                  # 18 tests: lots, wash sales, engine, live plan
python scripts/fetch_data.py 2014-01-01    # ~5 min: membership + Yahoo prices/dividends/shares
python scripts/run_backtests.py            # full grid -> results/grid.json
ddi target --value 25000                   # today's target portfolio for $25k
```

### Live, agent mode (recommended)
```bash
claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading
mkdir -p ~/.ddi && cp examples/config.json ~/.ddi/config.json   # set account_id, lot_mode
```
Then in Claude Code, inside this repo, run the `ddi-rebalance` skill (`.claude/skills/ddi-rebalance`).
Keep Robinhood **Trade approvals ON** for the MCP account at first. Every order then needs
your tap in the Robinhood app.

### Live, headless (experimental)
`ddi mcp-tools` signs in with OAuth and dumps the tool schemas. `ddi mcp-run --plan plan.json`
only reviews orders; add `--place` to actually place them. Argument names were checked against
the live schemas in Nov 2026; response parsing is still left to the agent workflow.

### What the live MCP supports (checked Nov 2026)
- **Specific-lot sells:** `tax_lots: [{open_lot_id, quantity}]`, at most 30 lots per order.
  Plans carry this payload and split larger sells. Without it the broker sells FIFO.
- **Idempotency:** `ref_id` on `place_equity_order`. Each plan row has a deterministic one, so
  re-running a plan can't double-place.
- **Fractional / dollar orders:** market, good-for-day, regular hours only.
- **Trade approvals:** when on, placed orders wait for your tap in the Robinhood app.
- Tax lots come one symbol per call, so a full snapshot is ~one call per holding.
- A first deployment is ~300 dollar-based buys at $10k (one review + one place each).

### Wash-sale traps in your other accounts
- **Dividend reinvestment (DRIP)** in another account is a purchase. Every DRIP buy of an
  S&P name blocks harvesting that name here for 30 days. Consider turning DRIP off for
  S&P stocks you hold elsewhere.
- The engine only protects the MCP account. If you sell an S&P stock at a loss in your main
  account, a buy of the same stock here within 30 days washes that loss. Don't harvest
  manually elsewhere while this runs.
- IRA purchases of a stock you harvested can permanently disallow the loss.

## Things to set before going live
- `lot_mode`: `specific` (the default in `examples/config.json`). Robinhood's MCP accepts
  specific lots. Use `fifo` only if lot selection ever fails; then the engine harvests whole
  positions only, so lot order can't matter.
- `sleeve` (optional): if you enable it, pick an S&P ETF you do **not** sell at a loss anywhere else (e.g. SPLG or IVV).
- `external_buys.json`: list buys of individual S&P stocks in other accounts (Wealthfront,
  401k, IRA, spouse). Robinhood accounts are read automatically via MCP. IRA purchases can
  permanently disallow a loss.
- Migration from Wealthfront: don't run both on the same stocks during the overlap. Transfer
  whole shares in-kind (keeps basis). Fractional shares usually get liquidated on transfer.

## Risks / limitations
- **Bugs cost money.** The whole point of the 0.09% Wealthfront fee is someone else owns this
  risk. On $100k that fee is ~$90/yr.
- MCP is **beta**. Rate limits are undocumented. Many
  small fractional orders may hit limits you can't see.
- Backtest data is free Yahoo data:
  - ~25% of 2016 members (mostly delisted or acquired) have no prices.
  - Weights are approximated from SPY's current share counts.
  - Results are measured against a synthetic index built from the same data, and against SPY.
- Harvested losses only help if you have gains or income to offset. Above $3k/yr of excess
  losses they carry forward. Harvesting defers tax, it doesn't forgive it, unless you hold
  until step-up or donate.
- State lives in `~/.ddi/state.json` (30-day restrictions). Losing it means relying on
  the order history alone.

MIT licensed.
