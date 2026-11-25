# diy-direct-index

DIY S&P 500 direct indexing with tax-loss harvesting (TLH), driven through
[Robinhood's agentic-trading MCP server](https://robinhood.com/us/en/support/articles/agentic-trading-overview/).
It does roughly what Wealthfront S&P 500 Direct does (0.09%/yr), for $0 advisory fee, in an
account you control.

> **Not investment or tax advice.** This is hobbyist software that can place real orders.
> Wash-sale and tax treatment here is a model, not a ruling. Read the risks section.

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
`ddi mcp-tools` signs in with OAuth and dumps the real tool schemas. `ddi mcp-run --plan plan.json`
only reviews orders; add `--place` to actually place them. The argument names in
`ddi/broker/robinhood_mcp.py` are **unverified guesses** until you run `mcp-tools`.

## Things to set before going live
- `lot_mode`: does `place_equity_order` accept specific lots? Check with `ddi mcp-tools`.
  If it does, use `specific`. If not, set the account's default disposal method in the app
  and match it here (`hifo` if offered, else `fifo`). In FIFO mode the engine harvests whole
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
- MCP is **beta**. Rate limits, tool schemas and lot selection are undocumented. Many
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
