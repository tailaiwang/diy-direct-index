"""Tax-lot ledger with wash-sale accounting.

The ledger is the source of truth for cost basis inside the engine. In live use it is
rebuilt from the broker's open tax lots (Robinhood MCP `get_equity_tax_lots`) plus the
recent order history, then the engine's own restriction state is layered on top.

Wash-sale rule (IRC §1091), as modelled here:
  A loss on a sale is (partly) disallowed if substantially identical shares are acquired
  within 30 days before or after the sale AND those replacement shares are still held.
  The disallowed loss is added to the replacement lot's basis (deferred, not lost).
We model "substantially identical" as "same ticker". Different companies (KO vs PEP) are
not substantially identical. Cross-account purchases (IRA, spouse, other brokers) also
count under the IRS rule; the engine can only see what you feed it (see `external_buys`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Literal

WASH_DAYS = 30
LONG_TERM_DAYS = 365
EPS = 1e-9

LotMode = Literal["specific", "hifo", "fifo"]


@dataclass
class Lot:
    qty: float
    cost: float          # per-share basis (includes any wash-sale adjustment)
    acquired: date       # acquisition date (we do not tack holding periods on wash adjustments)
    id: int = 0

    def unrealized(self, price: float) -> float:
        return self.qty * (price - self.cost)


@dataclass
class Realized:
    sym: str
    sold: date
    acquired: date
    qty: float
    proceeds: float
    basis: float
    reason: str
    washed: float = 0.0  # portion of the loss disallowed (positive number)

    @property
    def gain(self) -> float:
        """Recognised gain/loss after wash-sale disallowance."""
        g = self.proceeds - self.basis
        return g + self.washed if g < 0 else g

    @property
    def term(self) -> str:
        return "LT" if (self.sold - self.acquired).days > LONG_TERM_DAYS else "ST"


@dataclass
class Ledger:
    lots: dict[str, list[Lot]] = field(default_factory=dict)
    buys: dict[str, list[tuple[date, float]]] = field(default_factory=dict)   # (date, qty)
    loss_sales: dict[str, list[Realized]] = field(default_factory=dict)       # unwashed loss sales
    realized: list[Realized] = field(default_factory=list)
    external_buys: dict[str, list[date]] = field(default_factory=dict)        # other accounts
    _next_id: int = 0

    # ---------- queries ----------
    def qty(self, sym: str) -> float:
        return sum(l.qty for l in self.lots.get(sym, ()))

    def symbols(self) -> list[str]:
        return [s for s, ls in self.lots.items() if ls]

    def basis(self, sym: str) -> float:
        return sum(l.qty * l.cost for l in self.lots.get(sym, ()))

    def last_buy(self, sym: str) -> date | None:
        ds = [d for d, _ in self.buys.get(sym, ())] + self.external_buys.get(sym, [])
        return max(ds) if ds else None

    def last_loss_sale(self, sym: str) -> date | None:
        ls = self.loss_sales.get(sym)
        return max(r.sold for r in ls) if ls else None

    def can_buy(self, sym: str, today: date) -> bool:
        """False while buying would wash a loss we realised in the last 30 days."""
        d = self.last_loss_sale(sym)
        return d is None or (today - d).days > WASH_DAYS

    def can_sell_at_loss(self, sym: str, today: date, selling_all: bool = False) -> bool:
        """False if shares were bought in the last 30 days and would remain held after the
        sale (that purchase would be a replacement and wash the loss).
        Selling the *entire* position including recent lots is allowed when no external
        account bought it recently."""
        ext = [d for d in self.external_buys.get(sym, []) if (today - d).days <= WASH_DAYS]
        if ext:
            return False
        lb = max((d for d, _ in self.buys.get(sym, ())), default=None)
        if lb is None or (today - lb).days > WASH_DAYS:
            return True
        return selling_all

    # ---------- mutations ----------
    def buy(self, sym: str, qty: float, price: float, today: date) -> Lot:
        if qty <= EPS:
            raise ValueError("qty must be positive")
        cost = price
        # Wash-sale (forward direction): buying within 30 days after a loss sale.
        remaining = qty
        for r in sorted(self.loss_sales.get(sym, []), key=lambda r: r.sold):
            if remaining <= EPS:
                break
            if (today - r.sold).days > WASH_DAYS:
                continue
            loss = -(r.proceeds - r.basis) - r.washed
            if loss <= EPS:
                continue
            # disallow loss on up to `remaining` shares of that sale
            frac = min(1.0, remaining / r.qty)
            dis = loss * frac
            r.washed += dis
            cost += dis / qty
            remaining -= r.qty * frac
        lot = Lot(qty=qty, cost=cost, acquired=today, id=self._next_id)
        self._next_id += 1
        self.lots.setdefault(sym, []).append(lot)
        self.buys.setdefault(sym, []).append((today, qty))
        return lot

    def _order_lots(self, sym: str, mode: LotMode, price: float) -> list[Lot]:
        ls = list(self.lots.get(sym, []))
        if mode == "fifo":
            return sorted(ls, key=lambda l: (l.acquired, l.id))
        if mode == "hifo":
            return sorted(ls, key=lambda l: -l.cost)
        raise ValueError("specific mode needs explicit lots")

    def sell(self, sym: str, qty: float, price: float, today: date, *, mode: LotMode = "fifo",
             lot_ids: Iterable[int] | None = None, reason: str = "") -> list[Realized]:
        """Sell `qty` shares. With mode='specific' pass `lot_ids` (consumed in that order)."""
        held = self.lots.get(sym, [])
        if qty > self.qty(sym) + 1e-6:
            raise ValueError(f"selling {qty} {sym} but hold {self.qty(sym)}")
        if mode == "specific":
            by_id = {l.id: l for l in held}
            order = [by_id[i] for i in lot_ids]  # type: ignore[union-attr]
        else:
            order = self._order_lots(sym, mode, price)
        out: list[Realized] = []
        left = qty
        for lot in order:
            if left <= EPS:
                break
            q = min(lot.qty, left)
            r = Realized(sym, today, lot.acquired, q, q * price, q * lot.cost, reason)
            lot.qty -= q
            left -= q
            out.append(r)
        self.lots[sym] = [l for l in held if l.qty > EPS]
        if not self.lots[sym]:
            del self.lots[sym]
        # Wash-sale (backward direction): shares bought in the prior 30 days still held.
        recent_held = sum(l.qty for l in self.lots.get(sym, [])
                          if 0 <= (today - l.acquired).days <= WASH_DAYS)
        for r in out:
            if r.proceeds - r.basis < 0:
                if recent_held > EPS:
                    loss = -(r.proceeds - r.basis)
                    frac = min(1.0, recent_held / r.qty)
                    dis = loss * frac
                    r.washed += dis
                    recent_held -= r.qty * frac
                    # add disallowed loss to the newest recent lots' basis
                    self._add_basis_to_recent(sym, today, dis)
                if r.proceeds - r.basis + r.washed < -EPS:
                    self.loss_sales.setdefault(sym, []).append(r)
        self.realized.extend(out)
        return out

    def _add_basis_to_recent(self, sym: str, today: date, amount: float) -> None:
        recent = [l for l in self.lots.get(sym, []) if 0 <= (today - l.acquired).days <= WASH_DAYS]
        tot = sum(l.qty for l in recent)
        for l in recent:
            l.cost += amount / tot

    def prune(self, today: date) -> None:
        """Drop wash-sale bookkeeping older than the window."""
        cutoff = today - timedelta(days=WASH_DAYS + 1)
        for s in list(self.buys):
            self.buys[s] = [(d, q) for d, q in self.buys[s] if d >= cutoff]
            if not self.buys[s]:
                del self.buys[s]
        for s in list(self.loss_sales):
            self.loss_sales[s] = [r for r in self.loss_sales[s] if r.sold >= cutoff]
            if not self.loss_sales[s]:
                del self.loss_sales[s]
