"""v3: только покупки по рынку в коридоре цены 0.30–0.95 (проверено на отложенной половине данных)."""
from polydesk.config import settings
from polydesk.feeds.clob_ws import OrderBook
from polydesk.markets import Market
from polydesk.model import Fair
from polydesk.strategy import Strategy


def _m():
    return Market("btc-updown-5m-1000", "0xc", "q", 1000, 1300, "UP", "DOWN")


def _decide(ask_up):
    fair = Fair(p_up=0.98, ref=1, cur=1, est_final=1, sd=1, known_w=0.5, sigma1=1e-4, source="t")
    bu = OrderBook("UP", bids={round(ask_up - 0.01, 2): 1000}, asks={ask_up: 1000})
    bd = OrderBook("DOWN", bids={0.05: 1000}, asks={0.06: 1000})
    return Strategy().decide(_m(), fair, bu, bd, 0, 0, 0, now=1240, equity=1000, cash=1000)


def test_no_cheap_lottery_takes():
    assert not [i for i in _decide(0.15) if i.kind == "take"]       # модель 98%, рынок 15% — модель ошибается


def test_takes_inside_corridor():
    assert [i for i in _decide(0.60) if i.kind == "take"]


def test_no_takes_above_095():
    assert not [i for i in _decide(0.96) if i.kind == "take"]


def test_maker_off_by_default():
    from polydesk import config as cfg
    src = open(cfg.__file__).read()
    assert 'os.getenv("MAKER_ENABLED", "0")' in src                   # по умолчанию выключено
