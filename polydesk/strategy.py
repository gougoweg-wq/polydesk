"""Two layers on one market:

taker  — when the model's favoured side trades below fair by more than fee + margin, lift the ask.
maker  — rest bids on both sides `margin` below fair, so a matched Up+Down set costs < $1;
         skew the quotes toward pairing when inventory is one-sided, cap the unpaired residual.
Positions are held to resolution: a complete set redeems $1, the winner redeems $1, the loser $0.
"""
from __future__ import annotations
from dataclasses import dataclass
from .config import settings
from .feeds.clob_ws import OrderBook
from .model import Fair, taker_fee


@dataclass
class Intent:
    kind: str            # take | quote | cancel_all
    side: str = ""       # Up | Down
    price: float = 0.0
    shares: float = 0.0
    reason: str = ""


def _round_tick(p: float) -> float:
    return max(0.01, min(0.99, round(p, 2)))


class Strategy:
    def __init__(self, s=settings):
        self.s = s

    def decide(self, market, fair: Fair, book_up: OrderBook, book_down: OrderBook,
               pos_up: float, pos_down: float, spent: float, now: float,
               equity: float | None = None, cash: float | None = None) -> list[Intent]:
        s = self.s
        out: list[Intent] = []
        t_left = market.end_ts - now
        if t_left <= 0 or fair.ref is None or fair.cur is None:
            return [Intent("cancel_all", reason="market over / no fair")]
        cap = s.max_market_notional
        if equity is not None:
            cap = min(cap, s.market_fraction * max(0.0, equity))   # ставка — доля капитала, как на реальном счёте
        budget = max(0.0, cap - spent)
        if cash is not None:
            budget = min(budget, max(0.0, cash))                   # нет денег — нет сделки

        # ---- taker layer
        if t_left <= s.taker_start_before_end and budget > 1:
            fav = "Up" if fair.p_up >= 0.5 else "Down"
            p_fav = fair.p_up if fav == "Up" else 1 - fair.p_up
            book = book_up if fav == "Up" else book_down
            ask = book.best_ask()
            if p_fav >= s.taker_min_p and ask and s.taker_min_price <= ask[0] < s.taker_max_price:
                price, size = ask
                edge = p_fav - price - taker_fee(1, price, market.fee_rate)
                if edge >= s.taker_min_edge:
                    shares = min(s.taker_max_shares, size, budget / price)
                    if shares >= 5:
                        out.append(Intent("take", fav, price, shares,
                                          f"p={p_fav:.3f} ask={price:.2f} edge={edge*100:.1f}c"))
                        budget -= shares * price               # остаток бюджета — лимитным заявкам

        # ---- maker layer
        if s.maker_enabled and t_left > s.maker_stop_before_end and budget > 1:
            imbalance = pos_up - pos_down                      # >0: long Up residual
            for side in ("Up", "Down"):
                p_side = fair.p_up if side == "Up" else 1 - fair.p_up
                lean = -s.skew * imbalance if side == "Up" else s.skew * imbalance
                bid = _round_tick(p_side - s.maker_margin + lean)
                my_res = imbalance if side == "Up" else -imbalance
                shares = s.maker_shares
                if my_res >= s.residual_max:
                    shares = 0.0                               # already too long this side
                if shares * bid > budget / 2:                  # бюджет делится на обе стороны
                    shares = budget / 2 / bid
                if shares >= 5 and 0.01 <= bid <= 0.97:
                    out.append(Intent("quote", side, bid, shares, f"fair={p_side:.3f}"))
                else:
                    out.append(Intent("quote", side, bid, 0.0, "no size"))
        elif s.maker_enabled:
            out.append(Intent("cancel_all", reason="maker window closed"))
        return out
