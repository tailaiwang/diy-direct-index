---
name: ddi-rebalance
description: Run one daily direct-indexing cycle on the Robinhood MCP account - snapshot via MCP tools, compute a deterministic plan with `ddi plan`, review every order, place only what the plan says, record fills.
---

# Daily direct-indexing cycle (Robinhood MCP)

You are a **courier**, not a portfolio manager. The `ddi` engine decides every trade.
Never add, remove, resize, or "improve" an order. If anything looks wrong, stop and tell the user.

Preconditions (check, don't assume):
- The `robinhood-trading` MCP server is connected (`claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`).
- US regular market hours, at least 30 minutes after the open and 30 before the close
  (fractional orders only fill in regular hours).
- `~/.ddi/config.json` exists and names `account_id` of the **MCP account** (from `get_accounts`).

## 1. Snapshot
Build `~/.ddi/snapshot.json` in the schema documented at the top of `ddi/live.py`:
- `get_accounts` -> the MCP account id; `get_portfolio` -> cash / buying power for it.
- `get_equity_tax_lots` for every held symbol (quantity, cost basis, acquisition date, lot id).
- `get_equity_quotes` for every S&P 500 name in `ddi weights` (max 20 symbols per call)
  plus the sleeve ETF from the config.
- `get_equity_orders` for **all** Robinhood accounts, last 31 days (buys in your main
  account count for wash sales too). Put them in `recent_orders` with `account_id`.
- Copy `~/.ddi/external_buys.json` (user-maintained: buys at Wealthfront, 401k, spouse) into `external_buys`.

## 2. Sells
`ddi plan --snapshot ~/.ddi/snapshot.json --phase sells --out ~/.ddi/plan-sells.json`
- If `guardrail_errors` is non-empty: STOP and show them to the user.
- Show the user a table: symbol, quantity, est. $ , reason (harvest / rebalance / removed),
  and the total harvested loss estimate.
- For each order call `review_equity_order` (share-based market sell). If a review warns
  about anything, stop and ask.
- If the order has `broker_lot_ids` and `place_equity_order` accepts a lot selection
  argument, pass them. If it does not, tell the user once that the account's default
  tax-lot method is being used (the config's `lot_mode` must match that default).
- Place with `place_equity_order` only after the user says go (or if the user explicitly
  enabled autopilot in `~/.ddi/config.json` -> `"autopilot": true`).
- Poll `get_equity_orders` until filled/cancelled. Write fills to `~/.ddi/fills-sells.json`
  (`symbol, side, quantity, avg_price, date`) and run
  `ddi record-fills --plan ~/.ddi/plan-sells.json --fills ~/.ddi/fills-sells.json --snapshot ~/.ddi/snapshot.json`.

## 3. Buys
Refresh the snapshot (cash changed), then
`ddi plan --snapshot ~/.ddi/snapshot.json --phase buys --out ~/.ddi/plan-buys.json`
and repeat review -> confirm -> place (dollar-based market buys) -> poll.

## 4. Report
One short summary: trades placed, harvested losses (est.), cash left, any errors.
Never retry a rejected order with different parameters.
