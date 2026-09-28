"""Paper exchange: simulated fills against the live book.

taker: walks the real asks, pays the real fee.
maker: a resting bid fills only when a print goes through it (taker SELL at <= our price), capped at
       printed size — i.e. we assume we were at the front of the queue, nothing more generous.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from ..config import settings
from ..feeds.clob_ws import OrderBook, Trade
from ..model import taker_fee


@dataclass
class Fill:
    ts: float
    slug: str
    side: str
    price: float
    shares: float
    fee: float
    kind: str          # take | make | settle
    note: str = ""


@dataclass
class Quote:
    side: str
    price: float
    shares: float
    filled: float = 0.0
    placed_at: float = field(default_factory=time.time)


@dataclass
class Position:
    slug: str
    up: float = 0.0
    down: float = 0.0
    spent: float = 0.0
    fees: float = 0.0
    settled: bool = False
    payout: float = 0.0

    @property
    def sets(self) -> float:
        return min(self.up, self.down)

    @property
    def residual(self) -> float:
        return self.up - self.down


class PaperExchange:
    def __init__(self, cash: float | None = None):
        self.cash = settings.starting_cash if cash is None else cash
        self.positions: dict[str, Position] = {}
        self.quotes: dict[str, dict[str, Quote]] = {}     # slug -> side -> Quote
        self.fills: list[Fill] = []
        self.on_fill = []

    def position(self, slug: str) -> Position:
        return self.positions.setdefault(slug, Position(slug))

    def _record(self, f: Fill) -> None:
        self.fills.append(f)
        for cb in self.on_fill:
            cb(f)

    # ---- taker
    def take(self, market, side: str, book: OrderBook, shares: float, max_price: float, note: str = "") -> Fill | None:
        left = shares
        cost = 0.0
        got = 0.0
        for p, s in book.asks_sorted():
            if p > max_price + 1e-9:
                break
            q = min(left, s, max(0.0, self.cash * 0.999 - cost) / p)   # наличные не уходят в минус
            if q <= 1e-9:
                break
            cost += q * p
            got += q
            left -= q
            if left <= 1e-9:
                break
        if got < 1e-9:
            return None
        avg = cost / got
        fee = taker_fee(got, avg, market.fee_rate)
        if cost + fee > self.cash:                         # с комиссией не хватает — берём меньше
            k = max(0.0, self.cash * 0.999) / (cost + fee)
            got, cost, fee = got * k, cost * k, fee * k
            if got < 1e-9:
                return None
        pos = self.position(market.slug)
        if side == "Up":
            pos.up += got
        else:
            pos.down += got
        pos.spent += cost
        pos.fees += fee
        self.cash -= cost + fee
        f = Fill(time.time(), market.slug, side, avg, got, fee, "take", note)
        self._record(f)
        return f

    # ---- maker
    def set_quote(self, market, side: str, price: float, shares: float) -> None:
        qs = self.quotes.setdefault(market.slug, {})
        if shares <= 0:
            qs.pop(side, None)
            return
        q = qs.get(side)
        if q and abs(q.price - price) < 1e-9 and q.shares - q.filled >= shares - 1e-9:
            return                                  # keep queue position
        qs[side] = Quote(side, price, shares)

    def cancel_all(self, slug: str) -> None:
        self.quotes.pop(slug, None)

    def on_trade(self, market, trade: Trade) -> Fill | None:
        """A print on one of the market's tokens: does it go through our resting bid?"""
        side = market.side_of(trade.token)
        if not side or trade.side != "SELL" or trade.size < 1:
            return None
        q = self.quotes.get(market.slug, {}).get(side)
        if not q or trade.price > q.price + 1e-9:
            return None
        avail = q.shares - q.filled
        got = min(avail, trade.size, max(0.0, self.cash) / q.price)
        if got < 1e-9:
            return None
        q.filled += got
        cost = got * q.price
        pos = self.position(market.slug)
        if side == "Up":
            pos.up += got
        else:
            pos.down += got
        pos.spent += cost
        self.cash -= cost
        f = Fill(time.time(), market.slug, side, q.price, got, 0.0, "make", f"print {trade.price:.2f}x{trade.size:.0f}")
        self._record(f)
        if q.filled >= q.shares - 1e-9:
            self.quotes[market.slug].pop(side, None)
        return f

    # ---- settlement
    def settle(self, market) -> float:
        pos = self.positions.get(market.slug)
        if not pos or pos.settled or not market.outcome:
            return 0.0
        payout = pos.up if market.outcome == "Up" else pos.down
        pos.payout = payout
        pos.settled = True
        self.cash += payout
        self.cancel_all(market.slug)
        self._record(Fill(time.time(), market.slug, market.outcome, 1.0, payout, 0.0, "settle",
                          f"pnl={payout - pos.spent - pos.fees:+.2f}"))
        return payout

    # ---- marks
    def open_value(self, marks: dict[str, float]) -> float:
        """Mark unsettled positions at model fair (p_up per slug)."""
        v = 0.0
        for slug, pos in self.positions.items():
            if pos.settled:
                continue
            p = marks.get(slug, 0.5)
            v += pos.up * p + pos.down * (1 - p)
        return v
