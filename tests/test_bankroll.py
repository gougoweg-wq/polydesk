"""Правила реального счёта: ставка на рынок — доля капитала, наличные не уходят в минус."""
from polydesk.feeds.clob_ws import OrderBook
from polydesk.execution.paper import PaperExchange
from polydesk.markets import Market
from polydesk.model import Fair
from polydesk.strategy import Strategy


def _m():
    return Market("btc-updown-5m-1000", "0xc", "q", 1000, 1300, "UP", "DOWN")


def _books():
    return OrderBook("UP", bids={0.90: 1000}, asks={0.92: 1000}), OrderBook("DOWN", bids={0.07: 1000}, asks={0.09: 1000})


FAIR = Fair(p_up=0.98, ref=1, cur=1, est_final=1, sd=1, known_w=0.5, sigma1=1e-4, source="t")


def test_budget_is_fraction_of_equity():
    bu, bd = _books()
    out = Strategy().decide(_m(), FAIR, bu, bd, 0, 0, 0, now=1240, equity=200, cash=200)
    spend = sum(i.shares * i.price for i in out if i.kind in ("take", "quote"))
    assert 0 < spend <= 0.05 * 200 + 1e-6            # 5% от $200 = $10 на рынок


def test_no_money_no_trades():
    bu, bd = _books()
    out = Strategy().decide(_m(), FAIR, bu, bd, 0, 0, 0, now=1240, equity=500, cash=0)
    assert not [i for i in out if i.kind in ("take", "quote") and i.shares > 0]


def test_paper_take_never_goes_negative():
    ex = PaperExchange(cash=5)
    bu, _ = _books()
    f = ex.take(_m(), "Up", bu, shares=100, max_price=0.95)
    assert ex.cash >= -1e-9 and (f is None or f.shares * f.price <= 5 + 1e-9)
