"""Задержка исполнения: заявка доходит через delay, исполняется по свежему стакану и только до лимита."""
import asyncio
from polydesk.execution.latency import DelayedTaker
from polydesk.execution.paper import PaperExchange
from polydesk.feeds.clob_ws import OrderBook
from polydesk.markets import Market


def _m():
    return Market("btc-updown-5m-1000", "0xc", "q", 1000, 1300, "UP", "DOWN")


def test_price_moved_away_during_delay_no_fill():
    ex = PaperExchange(cash=1000)
    book = {"b": OrderBook("UP", bids={0.59: 100}, asks={0.60: 100})}
    dt = DelayedTaker(ex, delay=0.01)

    async def go():
        task = asyncio.create_task(dt.take(_m(), "Up", lambda: book["b"], 50, 0.62, "t"))
        book["b"] = OrderBook("UP", bids={0.69: 100}, asks={0.70: 100})     # пока шла заявка, цена ушла
        return await task
    assert asyncio.run(go()) is None and ex.cash == 1000


def test_fills_on_fresh_book_and_blocks_duplicates():
    ex = PaperExchange(cash=1000)
    book = OrderBook("UP", bids={0.59: 100}, asks={0.60: 100})
    dt = DelayedTaker(ex, delay=0.01)

    async def go():
        task = asyncio.create_task(dt.take(_m(), "Up", lambda: book, 50, 0.62, "t"))
        await asyncio.sleep(0)
        busy = dt.busy(_m().slug, "Up")
        f = await task
        return busy, f, dt.busy(_m().slug, "Up")
    busy, f, after = asyncio.run(go())
    assert busy is True and f is not None and f.shares == 50 and after is False
