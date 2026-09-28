"""CLOB market websocket: order books and prints for the tokens we trade."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable
import websockets
from ..config import settings

log = logging.getLogger("clob")


@dataclass
class Trade:
    token: str
    price: float
    size: float
    side: str          # taker side: BUY | SELL
    ts: float


@dataclass
class OrderBook:
    token: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    updated: float = 0.0

    def best_bid(self) -> tuple[float, float] | None:
        return max(self.bids.items()) if self.bids else None

    def best_ask(self) -> tuple[float, float] | None:
        return min(self.asks.items()) if self.asks else None

    def asks_sorted(self) -> list[tuple[float, float]]:
        return sorted(self.asks.items())

    def bids_sorted(self) -> list[tuple[float, float]]:
        return sorted(self.bids.items(), reverse=True)

    def cost_to_buy(self, shares: float) -> tuple[float, float]:
        """Walk the asks: returns (shares filled, total $)."""
        left = shares
        cost = 0.0
        for p, s in self.asks_sorted():
            take = min(left, s)
            cost += take * p
            left -= take
            if left <= 1e-9:
                break
        return shares - left, cost


class BookFeed:
    def __init__(self):
        self.books: dict[str, OrderBook] = {}
        self.tokens: set[str] = set()
        self.trades: list[Trade] = []
        self.on_trade: list[Callable[[Trade], None]] = []
        self.connected = False
        self._ws = None
        self.msg_count = 0

    def subscribe(self, tokens: list[str]) -> None:
        new = [t for t in tokens if t not in self.tokens]
        self.tokens.update(tokens)
        for t in new:
            self.books.setdefault(t, OrderBook(t))
        if new and self._ws:
            asyncio.create_task(self._send({"assets_ids": new, "type": "market", "operation": "subscribe"}))

    def unsubscribe(self, tokens: list[str]) -> None:
        gone = [t for t in tokens if t in self.tokens]
        for t in gone:
            self.tokens.discard(t)
            self.books.pop(t, None)
        if gone and self._ws:
            asyncio.create_task(self._send({"assets_ids": gone, "type": "market", "operation": "unsubscribe"}))

    async def _send(self, obj: dict) -> None:
        try:
            await self._ws.send(json.dumps(obj))
        except Exception as e:  # noqa: BLE001
            log.warning("clob send failed: %s", e)

    async def run(self) -> None:
        while True:
            if not self.tokens:
                await asyncio.sleep(0.5)
                continue
            try:
                async with websockets.connect(settings.clob_ws_url, ping_interval=None, max_size=2**23) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"assets_ids": sorted(self.tokens), "type": "market"}))
                    self.connected = True
                    log.info("clob ws connected, %d tokens", len(self.tokens))
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self._handle(raw)
                    finally:
                        pinger.cancel()
            except Exception as e:  # noqa: BLE001
                self.connected = False
                self._ws = None
                log.warning("clob ws reconnect: %s", e)
                await asyncio.sleep(2)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    def _handle(self, raw: str | bytes) -> None:
        if raw in ("PONG", b"PONG"):
            return
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            return
        for m in (msg if isinstance(msg, list) else [msg]):
            if not isinstance(m, dict):
                continue
            et = m.get("event_type")
            if et == "book":
                self._on_book(m)
            elif et == "price_change":
                self._on_price_change(m)
            elif et == "last_trade_price":
                self._on_trade(m)
            self.msg_count += 1

    def _on_book(self, m: dict) -> None:
        tok = m.get("asset_id")
        if tok not in self.books:
            return
        b = self.books[tok]
        b.bids = {float(x["price"]): float(x["size"]) for x in m.get("bids", []) if float(x["size"]) > 0}
        b.asks = {float(x["price"]): float(x["size"]) for x in m.get("asks", []) if float(x["size"]) > 0}
        b.updated = time.time()

    def _on_price_change(self, m: dict) -> None:
        changes = m.get("price_changes") or m.get("changes") or []
        default_tok = m.get("asset_id")
        for c in changes:
            tok = c.get("asset_id", default_tok)
            if tok not in self.books:
                continue
            b = self.books[tok]
            side = (c.get("side") or "").upper()
            price = float(c["price"])
            size = float(c["size"])
            levels = b.bids if side == "BUY" else b.asks
            if size <= 0:
                levels.pop(price, None)
            else:
                levels[price] = size
            b.updated = time.time()

    def _on_trade(self, m: dict) -> None:
        tok = m.get("asset_id")
        if tok not in self.books:
            return
        try:
            t = Trade(tok, float(m["price"]), float(m.get("size") or 0), (m.get("side") or "").upper(),
                      float(m.get("timestamp") or time.time() * 1000) / (1000 if float(m.get("timestamp") or 0) > 1e11 else 1))
        except (KeyError, ValueError):
            return
        self.trades.append(t)
        if len(self.trades) > 5000:
            del self.trades[:2500]
        for cb in self.on_trade:
            try:
                cb(t)
            except Exception as e:  # noqa: BLE001
                log.exception("on_trade callback: %s", e)
