"""The loop: feeds → market lifecycle → fair value → strategy → execution → ledger."""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
import aiohttp
from .config import settings
from .feeds.rtds import PriceFeed
from .feeds.clob_ws import BookFeed, Trade
from .markets import Market, fetch_market, refresh, slot_ts, slug_for
from .model import FairValue, Fair
from .strategy import Strategy, Intent
from .execution.paper import PaperExchange, Position
from .execution.latency import DelayedTaker
from .ledger import Ledger

log = logging.getLogger("engine")

PENDING_RETRY = 60            # как часто переспрашивать исход зависшего рынка, с
GIVE_UP_AFTER = 2 * 86400     # после двух суток без исхода перестаём спрашивать (позиция остаётся в базе)


def market_action(now: float, m: Market) -> str:
    """Что делать с рынком после конца окна. Никогда не выбрасываем рынок с деньгами молча:
    исход иногда приходит позже 30 минут (разрешение UMA), а позиция и траты должны дождаться его."""
    if m.outcome:
        return "retire" if now > m.end_ts + 120 else "wait"
    if now > m.end_ts + GIVE_UP_AFTER:
        return "give_up"
    if now > m.end_ts + 1800:
        return "pending"
    return "wait"


def persist_position(ledger: Ledger, m: Market, exchange) -> None:
    """Траты видны в базе сразу после сделки, а не только после исхода."""
    ledger.upsert_market(m, exchange.positions.get(m.slug))


def restore_positions(ledger: Ledger, exchange, starting_cash: float) -> list[str]:
    """После перезапуска: вернуть неразрешённые позиции из базы и честно посчитать наличные."""
    realized = ledger.stats()["realized_pnl"]
    rows = ledger.db.execute("""SELECT slug, up, down, spent, fees FROM markets
                                WHERE settled_at IS NULL AND (spent > 0 OR up > 0 OR down > 0)""").fetchall()
    open_cost = 0.0
    for slug, up, down, spent, fees in rows:
        exchange.positions[slug] = Position(slug, up=up or 0, down=down or 0, spent=spent or 0, fees=fees or 0)
        open_cost += (spent or 0) + (fees or 0)
    exchange.cash = starting_cash + realized - open_cost
    return [r[0] for r in rows]


def rebuild_from_fills(ledger: Ledger) -> list[str]:
    """Починка старых данных: у рынков, выброшенных до исхода, траты есть только в fills.
    Переписываем такие строки по сделкам; движок потом дождётся исхода и рассчитается."""
    rows = ledger.db.execute("""
        WITH f AS (SELECT slug,
            SUM(CASE WHEN kind IN ('take','make') AND side='Up' THEN shares ELSE 0 END) up,
            SUM(CASE WHEN kind IN ('take','make') AND side='Down' THEN shares ELSE 0 END) down,
            SUM(CASE WHEN kind IN ('take','make') THEN price*shares ELSE 0 END) spent,
            SUM(CASE WHEN kind IN ('take','make') THEN fee ELSE 0 END) fees,
            SUM(CASE WHEN kind='settle' THEN 1 ELSE 0 END) settles
          FROM fills GROUP BY slug)
        SELECT f.slug, f.up, f.down, f.spent, f.fees FROM f JOIN markets m ON m.slug=f.slug
        WHERE m.settled_at IS NULL AND f.settles=0 AND ABS(f.spent - COALESCE(m.spent,0)) > 0.01""").fetchall()
    for slug, up, down, spent, fees in rows:
        ledger.db.execute("UPDATE markets SET up=?, down=?, spent=?, fees=? WHERE slug=?", (up, down, spent, fees, slug))
    ledger.db.commit()
    return [r[0] for r in rows]


class Engine:
    def __init__(self):
        self.prices = PriceFeed()
        self.books = BookFeed()
        self.model = FairValue(self.prices)
        self.strategy = Strategy()
        self.ledger = Ledger()
        self.exchange = self._make_exchange()
        self.delayed = DelayedTaker(self.exchange, settings.taker_latency)
        self.pending: dict[str, float] = {}           # slug -> когда спрашивали исход последний раз
        self.pending_markets: dict[str, Market] = {}
        if settings.mode == "paper":
            # перезапуск восстанавливает и реализованный результат, и открытые позиции
            fixed = rebuild_from_fills(self.ledger)
            if fixed:
                self.ledger.event("repair", f"rebuilt {len(fixed)} market rows from fills")
            for slug in restore_positions(self.ledger, self.exchange, settings.starting_cash):
                self.pending[slug] = 0.0
        self.markets: dict[str, Market] = {}          # slug -> market (current, next, unresolved past)
        self.fair: dict[str, Fair] = {}
        self.last_intents: list[Intent] = []
        self.started_at = time.time()
        self.session: aiohttp.ClientSession | None = None
        self.books.on_trade.append(self._on_trade)
        self.exchange.on_fill.append(self._on_fill)
        self.tick_n = 0
        self.errors: list[str] = []

    def _make_exchange(self):
        if settings.mode == "live":
            from .execution.live import LiveExchange
            return LiveExchange()
        return PaperExchange()

    # ---- callbacks
    def _on_trade(self, t: Trade) -> None:
        for m in self.markets.values():
            if m.side_of(t.token) and not m.outcome:
                self.exchange.on_trade(m, t)

    def _on_fill(self, f) -> None:
        self.ledger.fill(f)
        self.ledger.event("fill", f"{f.kind} {f.side} {f.shares:.0f}@{f.price:.2f} {f.note}")
        m = self.markets.get(f.slug)
        if m and f.kind in ("take", "make"):
            persist_position(self.ledger, m, self.exchange)

    # ---- lifecycle
    async def _ensure_markets(self) -> None:
        now = time.time()
        cur = slot_ts(now)
        for ts in (cur, cur + settings.duration):
            slug = slug_for(ts)
            if slug not in self.markets:
                m = await fetch_market(self.session, slug)
                if m:
                    self.markets[slug] = m
                    self.books.subscribe([m.up_token, m.down_token])
                    self.ledger.upsert_market(m)
                    self.ledger.event("market", f"loaded {m.slug} ({m.question})")
        # resolve finished ones
        for slug, m in list(self.markets.items()):
            if now >= m.end_ts and not m.outcome and int(now) % 5 == 0:
                await refresh(self.session, m)
                if m.outcome:
                    self.exchange.settle(m)
                    pos = self.exchange.positions.get(slug)
                    final = self.prices.chainlink.twap(m.end_ts - settings.twap_seconds, m.end_ts)
                    self.ledger.upsert_market(m, pos, self.model.opening_ref(m), final)
                    self.ledger.event("resolve", f"{m.slug} → {m.outcome}" + (f" pnl={pos.payout - pos.spent - pos.fees:+.2f}" if pos else ""))
            action = market_action(now, m)
            if action == "retire":
                self.books.unsubscribe([m.up_token, m.down_token])
                self.markets.pop(slug, None)
                self.fair.pop(slug, None)
            elif action == "pending":
                # исход задерживается: торговлю по рынку закончили, но позицию и деньги ждём дальше
                self.ledger.event("warn", f"{m.slug} unresolved after 30 min — waiting for outcome")
                self.books.unsubscribe([m.up_token, m.down_token])
                self.markets.pop(slug, None)
                self.fair.pop(slug, None)
                self.pending[slug] = now
                self.pending_markets[slug] = m
        await self._resolve_pending(now)

    async def _resolve_pending(self, now: float) -> None:
        """Раз в минуту переспрашиваем исход зависших рынков и рассчитываемся."""
        for slug, last in list(self.pending.items()):
            if now - last < PENDING_RETRY:
                continue
            self.pending[slug] = now
            m = self.pending_markets.get(slug) or await fetch_market(self.session, slug)
            if not m:
                continue
            self.pending_markets[slug] = m
            if not m.outcome:
                await refresh(self.session, m)
            if m.outcome:
                self.exchange.settle(m)
                pos = self.exchange.positions.get(slug)
                self.ledger.upsert_market(m, pos)
                self.ledger.event("resolve", f"{slug} → {m.outcome} (late)" + (f" pnl={pos.payout - pos.spent - pos.fees:+.2f}" if pos else ""))
                self.pending.pop(slug, None)
                self.pending_markets.pop(slug, None)
            elif market_action(now, m) == "give_up":
                self.ledger.event("warn", f"{slug} no outcome after 2 days — stop asking, position kept in ledger")
                self.pending.pop(slug, None)
                self.pending_markets.pop(slug, None)

    def _act(self, m: Market, intents: list[Intent]) -> None:
        bu = self.books.books.get(m.up_token)
        bd = self.books.books.get(m.down_token)
        for it in intents:
            if it.kind == "take":
                if settings.mode == "paper" and settings.taker_latency > 0:
                    if not self.delayed.busy(m.slug, it.side):
                        token = m.up_token if it.side == "Up" else m.down_token
                        asyncio.create_task(self._take_later(m, it, token))
                    continue
                book = bu if it.side == "Up" else bd
                f = self.exchange.take(m, it.side, book, it.shares, it.price, it.reason)
                if f:
                    log.info("TAKE %s %s %.0f@%.3f %s", m.slug, it.side, f.shares, f.price, it.reason)
            elif it.kind == "quote":
                self.exchange.set_quote(m, it.side, it.price, it.shares)
            elif it.kind == "cancel_all":
                self.exchange.cancel_all(m.slug)

    async def _take_later(self, m: Market, it, token: str) -> None:
        f = await self.delayed.take(m, it.side, lambda: self.books.books.get(token), it.shares, it.price, it.reason)
        if f:
            log.info("TAKE %s %s %.0f@%.3f %s (after %.1fs)", m.slug, it.side, f.shares, f.price, it.reason,
                     settings.taker_latency)

    async def _tick(self) -> None:
        now = time.time()
        for slug, m in self.markets.items():
            if m.outcome:
                continue
            fair = self.model.evaluate(m, now)
            self.fair[slug] = fair
            bu = self.books.books.get(m.up_token)
            bd = self.books.books.get(m.down_token)
            if not bu or not bd or not bu.updated or not bd.updated:
                continue
            if m.state in ("OPEN", "FINAL_WINDOW"):
                pos = self.exchange.position(slug)
                equity = self.exchange.cash + self.exchange.open_value({s_: f.p_up for s_, f in self.fair.items()})
                intents = self.strategy.decide(m, fair, bu, bd, pos.up, pos.down, pos.spent, now,
                                               equity=equity, cash=self.exchange.cash)
                if slug == slug_for(slot_ts(now)):
                    self.last_intents = intents
                self._act(m, intents)
            elif m.state == "CLOSED":
                self.exchange.cancel_all(slug)
            if self.tick_n % 4 == 0:  # ~1/s
                ba = bu.best_bid(); aa = bu.best_ask(); bb = bd.best_bid(); ab = bd.best_ask()
                last = next((t.price for t in reversed(self.books.trades) if t.token == m.up_token), None)
                self.ledger.tick(slug, fair, ba[0] if ba else None, aa[0] if aa else None,
                                 bb[0] if bb else None, ab[0] if ab else None, last)
        if self.tick_n % 4 == 0:
            self.ledger.equity(self.exchange.cash, self.exchange.open_value({s: f.p_up for s, f in self.fair.items()}))
            self.ledger.commit()

    async def run(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ledger.event("start", f"engine start mode={settings.mode} asset={settings.asset}")
        asyncio.create_task(self.prices.run())
        asyncio.create_task(self.books.run())
        last_markets = 0.0
        while True:
            try:
                if time.time() - last_markets >= 1.0:
                    await self._ensure_markets()
                    last_markets = time.time()
                await self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("tick failed: %s", e)
                self.errors.append(f"{time.strftime('%H:%M:%S')} {e}")
                self.errors = self.errors[-20:]
            self.tick_n += 1
            await asyncio.sleep(settings.tick_seconds)

    # ---- snapshot for the dashboard
    def snapshot(self) -> dict:
        now = time.time()
        cur_slug = slug_for(slot_ts(now))
        mk = []
        for slug, m in sorted(self.markets.items(), key=lambda kv: kv[1].start_ts):
            f = self.fair.get(slug)
            bu = self.books.books.get(m.up_token)
            bd = self.books.books.get(m.down_token)
            pos = self.exchange.positions.get(slug)
            q = getattr(self.exchange, "quotes", {}).get(slug, {})
            mk.append(dict(
                slug=slug, question=m.question, start_ts=m.start_ts, end_ts=m.end_ts, state=m.state,
                outcome=m.outcome, is_current=slug == cur_slug,
                fair=dict(p_up=f.p_up, ref=f.ref, cur=f.cur, est=f.est_final, sd=f.sd, w=f.known_w, sigma1=f.sigma1, src=f.source) if f else None,
                book=dict(
                    up=dict(bids=bu.bids_sorted()[:6], asks=bu.asks_sorted()[:6]) if bu else None,
                    down=dict(bids=bd.bids_sorted()[:6], asks=bd.asks_sorted()[:6]) if bd else None),
                pos=dict(up=pos.up, down=pos.down, spent=pos.spent, fees=pos.fees, sets=pos.sets, residual=pos.residual, settled=pos.settled, payout=pos.payout) if pos else None,
                quotes={s: dict(price=x.price, shares=x.shares, filled=x.filled) for s, x in q.items()},
            ))
        binance = list(self.prices.binance.window(now - 900, now))[-900:]
        chain = list(self.prices.chainlink.window(now - 900, now))[-900:]
        open_value = self.exchange.open_value({s: f.p_up for s, f in self.fair.items()})
        st = self.ledger.stats()
        return dict(
            now=now, mode=settings.mode, asset=settings.asset, started_at=self.started_at,
            feeds=dict(rtds=self.prices.connected, clob=self.books.connected, rtds_msgs=self.prices.msg_count,
                       clob_msgs=self.books.msg_count, rtds_age=now - self.prices.last_msg_at if self.prices.last_msg_at else None),
            account=dict(cash=self.exchange.cash, open_value=open_value, equity=self.exchange.cash + open_value,
                         starting=settings.starting_cash, **st),
            markets=mk,
            intents=[dict(kind=i.kind, side=i.side, price=i.price, shares=i.shares, reason=i.reason) for i in self.last_intents],
            binance=[[round(t, 2), p] for t, p in binance[::max(1, len(binance)//600)]],
            chainlink=[[round(t, 2), p] for t, p in chain[::max(1, len(chain)//600)]],
            prints=[dict(ts=t.ts, side=t.side, price=t.price, size=t.size, tok=("Up" if any(m.up_token == t.token for m in self.markets.values()) else "Down")) for t in self.books.trades[-40:]],
            history=self.ledger.recent_markets(48),
            fills=self.ledger.recent_fills(40),
            equity=self.ledger.equity_curve(1500),
            events=self.ledger.recent_events(30),
            errors=self.errors[-5:],
            settings=settings.public(),
        )
