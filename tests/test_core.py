import math
import time
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polydesk.feeds.rtds import Series, PriceFeed
from polydesk.feeds.clob_ws import OrderBook, Trade
from polydesk.markets import Market
from polydesk.model import FairValue, taker_fee
from polydesk.strategy import Strategy
from polydesk.execution.paper import PaperExchange
from polydesk.config import settings


def test_twap_carries_forward():
    s = Series()
    s.add(0, 100); s.add(10, 110)
    assert abs(s.twap(0, 20) - 105) < 1e-9        # 10s at 100, 10s at 110
    assert abs(s.twap(5, 15) - 105) < 1e-9


def test_fee_matches_docs():
    assert abs(taker_fee(100, 0.5) - 1.75) < 1e-9


def _market(start):
    return Market("btc-updown-5m-%d" % start, "0xc", "q", start, start + 300, "UP", "DOWN")


def test_fair_is_certain_when_price_far_from_ref():
    feed = PriceFeed()
    start = 1_000_000
    for t in range(start - 60, start + 1):
        feed.chainlink.add(t, 100_000.0); feed.binance.add(t, 100_000.0)
    for t in range(start + 1, start + 250):
        feed.chainlink.add(t, 100_300.0); feed.binance.add(t, 100_300.0)
    fv = FairValue(feed)
    f = fv.evaluate(_market(start), now=start + 250)
    assert f.ref == 100_000.0
    assert f.p_up > 0.99
    # same distance below → certain Down
    feed2 = PriceFeed()
    for t in range(start - 60, start + 1):
        feed2.chainlink.add(t, 100_000.0); feed2.binance.add(t, 100_000.0)
    for t in range(start + 1, start + 250):
        feed2.chainlink.add(t, 99_700.0); feed2.binance.add(t, 99_700.0)
    assert FairValue(feed2).evaluate(_market(start), now=start + 250).p_up < 0.01


def test_paper_take_and_settle():
    m = _market(1000)
    ex = PaperExchange(cash=100)
    book = OrderBook("UP", asks={0.90: 50, 0.91: 100})
    f = ex.take(m, "Up", book, 80, 0.91)
    assert abs(f.shares - 80) < 1e-9
    assert abs(f.price - (50 * 0.90 + 30 * 0.91) / 80) < 1e-9
    m.outcome = "Up"
    ex.settle(m)
    pos = ex.positions[m.slug]
    pnl = pos.payout - pos.spent - pos.fees
    assert pnl > 0 and abs(ex.cash - (100 + pnl)) < 1e-9


def test_paper_maker_fills_only_on_print_through():
    m = _market(1000)
    ex = PaperExchange(cash=100)
    ex.set_quote(m, "Down", 0.40, 50)
    assert ex.on_trade(m, Trade("DOWN", 0.41, 20, "SELL", 0)) is None     # print above our bid
    assert ex.on_trade(m, Trade("DOWN", 0.40, 20, "BUY", 0)) is None      # taker bought, not sold
    f = ex.on_trade(m, Trade("DOWN", 0.40, 20, "SELL", 0))
    assert f and f.shares == 20 and ex.positions[m.slug].down == 20
    assert abs(ex.cash - (100 - 8)) < 1e-9


def test_strategy_takes_cheap_favourite_and_quotes_both_sides():
    from polydesk.model import Fair
    m = _market(1000)
    fair = Fair(p_up=0.98, ref=1, cur=1, est_final=1, sd=1, known_w=0.5, sigma1=1e-4, source="t")
    bu = OrderBook("UP", bids={0.90: 100}, asks={0.92: 100})
    bd = OrderBook("DOWN", bids={0.07: 100}, asks={0.09: 100})
    s = Strategy()
    out = s.decide(m, fair, bu, bd, 0, 0, 0, now=1000 + 300 - 60)
    kinds = {(i.kind, i.side) for i in out}
    assert ("take", "Up") in kinds
    take = next(i for i in out if i.kind == "take")
    assert take.price == 0.92 and take.shares > 0
    quotes = [i for i in out if i.kind == "quote"]
    assert {q.side for q in quotes} == {"Up", "Down"}
    up_q = next(q for q in quotes if q.side == "Up")
    assert up_q.price <= 0.98 - settings.maker_margin + 1e-9
    # inside the last 20s: cancel, no quotes
    out2 = s.decide(m, fair, bu, bd, 0, 0, 0, now=1000 + 300 - 10)
    assert any(i.kind == "cancel_all" for i in out2) and not [i for i in out2 if i.kind == "quote"]


def test_residual_cap_stops_quoting_the_long_side():
    from polydesk.model import Fair
    m = _market(1000)
    fair = Fair(p_up=0.5, ref=1, cur=1, est_final=1, sd=1, known_w=0, sigma1=1e-4, source="t")
    bu = OrderBook("UP", bids={0.49: 100}, asks={0.51: 100})
    bd = OrderBook("DOWN", bids={0.49: 100}, asks={0.51: 100})
    out = Strategy().decide(m, fair, bu, bd, pos_up=settings.residual_max + 10, pos_down=0, spent=30, now=1100)
    up_q = next(i for i in out if i.kind == "quote" and i.side == "Up")
    dn_q = next(i for i in out if i.kind == "quote" and i.side == "Down")
    assert up_q.shares == 0 and dn_q.shares > 0 and dn_q.price > up_q.price   # lean toward pairing
