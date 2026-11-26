---
name: ddi-rebalance
description: Run one daily direct-indexing cycle on the Robinhood MCP account - snapshot via MCP tools, compute a deterministic plan with `ddi plan`, review every order, place only what the plan says, record fills.
---

# Daily direct-indexing cycle (Robinhood MCP)

You are a **courier**, not a portfolio manager. The `ddi` engine decides every trade.
Never add, remove, resize, or "improve" an order. If anything looks wrong, stop and tell the user.

Preconditions (check, don't assume):
- The Robinhood MCP server is connected (as a claude.ai connector, or `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`).
- US regular market hours, at least 30 minutes after the open and 30 before the close
  (fractional orders only fill in regular hours).
- `~/.ddi/config.json` exists and its `account_id` is the `account_number` of the **MCP account**
  (the one account `get_accounts` shows with `agentic_allowed: true`). Every tool takes it as `account_number`.
- `get_trade_approval_setting` for that account. Tell the user whether approvals are on.

## 1. Snapshot
Build `~/.ddi/snapshot.json` in the schema documented at the top of `ddi/live.py`:
- `get_portfolio` -> `buying_power` for the MCP account -> `cash`.
- `get_equity_positions` (follow `cursor` pages) -> held symbols.
- `get_equity_tax_lots` once per held symbol (one symbol per call; follow `cursor`). For each
  lot write quantity (use `quantity_available`), cost per share, acquisition date, and the
  lot's `open_lot_id` as `lot_id`. Specific-lot sells need it.
- `get_equity_quotes` for every S&P 500 name in `ddi weights` plus the sleeve ETF. More than
  20 symbols per call still returns quotes (closes are omitted, which is fine).
- `get_equity_orders` with `created_at_gte` = 31 days ago and `state=filled` for **every**
  account in `get_accounts` (buys in your main account count for wash sales too). Put them in
  `recent_orders` with that `account_id`.
- Copy `~/.ddi/external_buys.json` (user-maintained: buys at Wealthfront, 401k, spouse) into `external_buys`.

## 2. Sells
`ddi plan --snapshot ~/.ddi/snapshot.json --phase sells --out ~/.ddi/plan-sells.json`
- If `guardrail_errors` is non-empty: STOP and show them to the user.
- Show the user a table: symbol, quantity, est. $ , reason (harvest / rebalance / removed),
  and the total harvested loss estimate.
- For each order call `review_equity_order` with exactly the plan row: `type=market`,
  `time_in_force=gfd`, `market_hours=regular_hours`, `quantity` as a string, and the row's
  `tax_lots` array unchanged (without it Robinhood sells FIFO, which can realize gains instead
  of the harvested loss). If `order_checks` is non-empty, stop and ask.
- Place with `place_equity_order` (same arguments plus the row's `ref_id`) only after the user
  says go, or if the user explicitly enabled autopilot in `~/.ddi/config.json`.
  With trade approvals on, this sends the order to the Robinhood app; the user approves it
  there. On a transport error, retry with the **same** `ref_id`; never invent a new one.
- Poll `get_equity_orders` until filled/cancelled. Write fills to `~/.ddi/fills-sells.json`
  (`symbol, side, quantity, avg_price, date`) and run
  `ddi record-fills --plan ~/.ddi/plan-sells.json --fills ~/.ddi/fills-sells.json --snapshot ~/.ddi/snapshot.json`.

## 3. Buys
Refresh the snapshot (cash changed), then
`ddi plan --snapshot ~/.ddi/snapshot.json --phase buys --out ~/.ddi/plan-buys.json`
and repeat review -> confirm -> place -> poll. Buys are `dollar_amount` (string, 2 decimals),
`type=market`, `time_in_force=gfd`, plus the row's `ref_id`.

## 4. Report
One short summary: trades placed, harvested losses (est.), cash left, any errors.
Never retry a rejected order with different parameters.
