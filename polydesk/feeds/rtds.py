"""Polymarket Real-Time Data Socket: Binance spot (fast, leads) and Chainlink (what settles the market)."""
from __future__ import annotations
import asyncio
import bisect
import json
import logging
import time
from collections import deque
import websockets
from ..config import settings

log = logging.getLogger("rtds")


class Series:
    """Time series with O(log n) lookups; keeps ~2h of ticks."""

    def __init__(self, maxlen: int = 200_000):
        self.ts: deque[float] = deque(maxlen=maxlen)
        self.px: deque[float] = deque(maxlen=maxlen)

    def add(self, t: float, p: float) -> None:
        if self.ts and t < self.ts[-1]:
            return
        self.ts.append(t)
        self.px.append(p)

    def last(self) -> tuple[float, float] | None:
        return (self.ts[-1], self.px[-1]) if self.ts else None

    def at(self, t: float) -> float | None:
        if not self.ts:
            return None
        ts = list(self.ts)
        i = bisect.bisect_right(ts, t) - 1
        return self.px[i] if i >= 0 else None

    def window(self, a: float, b: float) -> list[tuple[float, float]]:
        ts = list(self.ts)
        i = bisect.bisect_left(ts, a)
        j = bisect.bisect_right(ts, b)
        px = list(self.px)
        return list(zip(ts[i:j], px[i:j]))

    def twap(self, a: float, b: float) -> float | None:
        """Time-weighted average over [a, b], carrying the last price forward across gaps."""
        pts = self.window(a, b)
        prev = self.at(a)
        if prev is not None:
            pts = [(a, prev)] + pts
        if not pts:
            return None
        total = 0.0
        weight = 0.0
        for k, (t, p) in enumerate(pts):
            nxt = pts[k + 1][0] if k + 1 < len(pts) else b
            dt = max(0.0, min(nxt, b) - max(t, a))
            total += p * dt
            weight += dt
        return total / weight if weight > 0 else pts[-1][1]


class PriceFeed:
    def __init__(self):
        self.binance = Series()
        self.chainlink = Series()
        self.connected = False
        self.msg_count = 0
        self.last_msg_at = 0.0
        self.chainlink_polls = 0

    def _sub(self) -> str:
        # Binance topic ignores symbol filters (streams every pair) — we filter by symbol on receipt.
        return json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices", "type": "update"},
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": settings.symbol_chainlink}, separators=(",", ":"))},
            {"topic": "crypto_prices_chainlink", "type": "update", "filters": json.dumps({"symbol": settings.symbol_chainlink}, separators=(",", ":"))},
        ]})

    async def run(self) -> None:
        while True:
            try:
                async with websockets.connect(settings.rtds_url, ping_interval=None, max_size=2**22) as ws:
                    await ws.send(self._sub())
                    self.connected = True
                    log.info("rtds connected")
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self._handle(raw)
                    finally:
                        pinger.cancel()
            except Exception as e:  # noqa: BLE001
                self.connected = False
                log.warning("rtds reconnect: %s", e)
                await asyncio.sleep(2)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(5)
            await ws.send("PING")
            last = self.chainlink.last()
            if not last or time.time() - last[0] > 10:
                # no live Chainlink stream: re-subscribing returns the last ~60s as a batch
                await ws.send(self._sub())
                self.chainlink_polls += 1

    def _handle(self, raw: str | bytes) -> None:
        if not raw or raw in ("PONG", b"PONG"):
            return
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            p = m.get("payload") or {}
            if not isinstance(p, dict):
                continue
            symbol = (p.get("symbol") or "").lower()
            # the server labels Chainlink history batches with topic "crypto_prices"; the symbol is what identifies the source
            if symbol == settings.symbol_chainlink:
                series = self.chainlink
            elif symbol == settings.symbol_binance:
                series = self.binance
            else:
                continue
            points = p.get("data") if isinstance(p.get("data"), list) else [p]
            for pt in points:
                try:
                    value = float(pt.get("value") if pt.get("value") is not None else pt.get("price"))
                    t = float(pt.get("timestamp") or m.get("timestamp") or time.time() * 1000)
                except (TypeError, ValueError, AttributeError):
                    continue
                if t > 1e11:
                    t /= 1000.0
                series.add(t, value)
            self.msg_count += 1
            self.last_msg_at = time.time()
