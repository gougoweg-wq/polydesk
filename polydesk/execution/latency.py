"""Бумажное исполнение с задержкой: реальная заявка доходит до биржи не мгновенно.

Через `delay` секунд берётся свежий стакан, и покупка исполняется только по цене не выше лимита.
Если за это время выгодные аски исчезли (их забрали быстрее нас), сделки нет. Пока заявка в пути,
повторная такая же не отправляется.
"""
import asyncio


class DelayedTaker:
    def __init__(self, exchange, delay=1.0):
        self.exchange = exchange
        self.delay = delay
        self.pending = set()

    def busy(self, slug, side):
        return (slug, side) in self.pending

    async def take(self, market, side, get_book, shares, limit, note=""):
        key = (market.slug, side)
        self.pending.add(key)
        try:
            await asyncio.sleep(self.delay)
            book = get_book()
            if book is None:
                return None
            return self.exchange.take(market, side, book, shares, limit, note)
        finally:
            self.pending.discard(key)
