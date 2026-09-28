"""Live exchange: same interface as PaperExchange, orders go to the Polymarket CLOB.

Positions/cash are tracked locally from our own fills (mirrors paper), plus a periodic reconcile
against the CLOB open orders. Keys come only from .env; they are never logged.
"""
from __future__ import annotations
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from ..config import settings
from ..feeds.clob_ws import OrderBook, Trade
from .paper import PaperExchange, Fill, Quote

log = logging.getLogger("live")


class LiveExchange(PaperExchange):
    def __init__(self):
        super().__init__(cash=0.0)
        if not settings.private_key or not settings.funder:
            raise RuntimeError("MODE=live needs POLY_PRIVATE_KEY and POLY_FUNDER in .env")
        from py_clob_client.client import ClobClient
        self.client = ClobClient(settings.clob_host, chain_id=settings.chain_id, key=settings.private_key,
                                 signature_type=settings.signature_type, funder=settings.funder)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.order_ids: dict[str, dict[str, str]] = {}      # slug -> side -> order id
        self.fee_bps = 1000
        try:
            bal = self.client.get_balance_allowance()
            self.cash = float(bal.get("balance", 0)) / 1e6
        except Exception as e:  # noqa: BLE001
            log.warning("balance read failed: %s", e)

    def _bg(self, fn, *a):
        asyncio.get_event_loop().run_in_executor(self.pool, fn, *a)

    # ---- taker: FAK market order up to max_price
    def take(self, market, side: str, book: OrderBook, shares: float, max_price: float, note: str = "") -> Fill | None:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        args = MarketOrderArgs(token_id=market.token(side), amount=round(shares * max_price, 2), side="BUY",
                               price=max_price, fee_rate_bps=self.fee_bps, order_type=OrderType.FAK)
        try:
            signed = self.client.create_market_order(args)
            resp = self.client.post_order(signed, OrderType.FAK)
        except Exception as e:  # noqa: BLE001
            log.error("take failed: %s", e)
            return None
        got = float(resp.get("takingAmount") or 0) if isinstance(resp, dict) else 0.0
        paid = float(resp.get("makingAmount") or 0) if isinstance(resp, dict) else 0.0
        if got <= 0:
            log.info("take not filled: %s", resp)
            return None
        avg = paid / got
        from ..model import taker_fee
        fee = taker_fee(got, avg, market.fee_rate)
        pos = self.position(market.slug)
        if side == "Up":
            pos.up += got
        else:
            pos.down += got
        pos.spent += paid
        pos.fees += fee
        self.cash -= paid + fee
        f = Fill(time.time(), market.slug, side, avg, got, fee, "take", note + f" oid={str(resp.get('orderID',''))[:10]}")
        self._record(f)
        return f

    # ---- maker: GTD limit bids, replaced when price/size change
    def set_quote(self, market, side: str, price: float, shares: float) -> None:
        qs = self.quotes.setdefault(market.slug, {})
        q = qs.get(side)
        if shares <= 0:
            if q:
                self._cancel(market.slug, side)
            return
        if q and abs(q.price - price) < 1e-9:
            return
        if q:
            self._cancel(market.slug, side)
        qs[side] = Quote(side, price, shares)
        self._bg(self._place, market, side, price, shares)

    def _place(self, market, side, price, shares):
        from py_clob_client.clob_types import OrderArgs, OrderType
        try:
            args = OrderArgs(token_id=market.token(side), price=price, size=shares, side="BUY",
                             fee_rate_bps=self.fee_bps, expiration=int(market.end_ts) + 60)
            resp = self.client.create_and_post_order(args)
            oid = resp.get("orderID") if isinstance(resp, dict) else None
            if oid:
                self.order_ids.setdefault(market.slug, {})[side] = oid
        except Exception as e:  # noqa: BLE001
            log.error("quote failed %s %s: %s", side, price, e)
            self.quotes.get(market.slug, {}).pop(side, None)

    def _cancel(self, slug: str, side: str) -> None:
        oid = self.order_ids.get(slug, {}).pop(side, None)
        self.quotes.get(slug, {}).pop(side, None)
        if oid:
            self._bg(self._cancel_id, oid)

    def _cancel_id(self, oid):
        try:
            self.client.cancel(oid)
        except Exception as e:  # noqa: BLE001
            log.warning("cancel %s: %s", oid[:10], e)

    def cancel_all(self, slug: str) -> None:
        for side in list(self.order_ids.get(slug, {})):
            self._cancel(slug, side)
        self.quotes.pop(slug, None)

    def on_trade(self, market, trade: Trade) -> Fill | None:
        # live fills are confirmed by the CLOB; we mirror the paper heuristic and reconcile later
        return super().on_trade(market, trade)

    def settle(self, market) -> float:
        # winning shares are redeemable on-chain; cash credit mirrors what redemption will return
        return super().settle(market)
