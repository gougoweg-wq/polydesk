"""Fair value of "Up": P(final 60s Chainlink TWAP >= opening TWAP) given the price path so far.

Binance leads Chainlink by a few hundred ms to seconds; we estimate the current Chainlink-equivalent
price as Binance * EWMA(chainlink/binance) and treat the remaining path as Brownian with realized vol.
"""
from __future__ import annotations
import math
import time
from dataclasses import dataclass
from .config import settings
from .feeds.rtds import PriceFeed
from .markets import Market


def Phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass
class Fair:
    p_up: float
    ref: float | None
    cur: float | None
    est_final: float | None
    sd: float | None
    known_w: float        # share of the final TWAP window already observed
    sigma1: float         # per-second log vol
    source: str


class FairValue:
    def __init__(self, feed: PriceFeed):
        self.feed = feed
        self.sigma1 = 0.5e-4           # 0.5 bp/s — bootstrap until we have data
        self.basis = 1.0               # chainlink / binance
        self._last_sig_t = 0.0
        self._last_px: float | None = None
        self._var = self.sigma1 ** 2

    def update_stats(self) -> None:
        """EWMA of 1s returns from Binance, and of the chainlink/binance ratio."""
        lb = self.feed.binance.last()
        if not lb:
            return
        t, p = lb
        if self._last_px and t - self._last_sig_t >= 1.0:
            dt = t - self._last_sig_t
            r = math.log(p / self._last_px)
            v = r * r / dt
            self._var = 0.98 * self._var + 0.02 * v
            self.sigma1 = max(math.sqrt(self._var), 0.1e-4)
        if t - self._last_sig_t >= 1.0:
            self._last_sig_t = t
            self._last_px = p
        lc = self.feed.chainlink.last()
        if lc and lc[1] > 0 and p > 0 and t - lc[0] < 30:
            self.basis = 0.95 * self.basis + 0.05 * (lc[1] / p)

    def current_price(self, now: float | None = None) -> tuple[float | None, str]:
        lb = self.feed.binance.last()
        lc = self.feed.chainlink.last()
        now = now or time.time()
        if lb and now - lb[0] < 10:
            return lb[1] * self.basis, "binance*basis"
        if lc and now - lc[0] < 30:
            return lc[1], "chainlink"
        return None, "none"

    def opening_ref(self, m: Market) -> float | None:
        w = settings.twap_seconds
        ref = self.feed.chainlink.twap(m.start_ts - w, m.start_ts)
        if ref is None:
            ref = self.feed.binance.twap(m.start_ts - w, m.start_ts)
            if ref is not None:
                ref *= self.basis
        return ref

    def evaluate(self, m: Market, now: float | None = None) -> Fair:
        now = now or time.time()
        self.update_stats()
        ref = self.opening_ref(m)
        cur, src = self.current_price(now)
        w_len = settings.twap_seconds
        win_start = m.end_ts - w_len
        if ref is None or cur is None:
            return Fair(0.5, ref, cur, None, None, 0.0, self.sigma1, src)
        if now >= m.end_ts:
            known = self.feed.chainlink.twap(win_start, m.end_ts) or cur
            return Fair(1.0 if known >= ref else 0.0, ref, cur, known, 0.0, 1.0, self.sigma1, src)
        if now >= win_start:
            known = self.feed.chainlink.twap(win_start, now)
            if known is None:
                known = cur
            w = (now - win_start) / w_len
            rem = m.end_ts - now
            est = w * known + (1 - w) * cur
            sd = cur * self.sigma1 * math.sqrt(max(rem, 0.5) / 3.0) * (1 - w)
        else:
            w = 0.0
            rem = win_start - now
            est = cur
            sd = cur * self.sigma1 * math.sqrt(rem + w_len / 3.0)
        if sd <= 0:
            p = 1.0 if est >= ref else 0.0
        else:
            p = Phi((est - ref) / sd)
        return Fair(p, ref, cur, est, sd, w, self.sigma1, src)


def taker_fee(shares: float, price: float, rate: float = settings.fee_rate) -> float:
    return shares * rate * price * (1.0 - price)
