from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Settings:
    mode: str = os.getenv("MODE", "paper")                  # paper | live
    asset: str = os.getenv("ASSET", "btc")
    symbol_binance: str = os.getenv("SYMBOL_BINANCE", "btcusdt")
    symbol_chainlink: str = os.getenv("SYMBOL_CHAINLINK", "btc/usd")
    slug_prefix: str = os.getenv("SLUG_PREFIX", "btc-updown-5m")
    duration: int = _i("DURATION", 300)                     # market length, s
    twap_seconds: int = _i("TWAP_SECONDS", 60)              # Chainlink TWAP lookback used at open and close
    fee_rate: float = _f("FEE_RATE", 0.07)                  # taker fee = shares * rate * p * (1-p)

    # taker layer: buy the side the model favours when it is cheaper than fair by more than fee + margin
    taker_min_edge: float = _f("TAKER_MIN_EDGE", 0.015)     # $/share net of fee
    taker_min_p: float = _f("TAKER_MIN_P", 0.90)            # only act when the model is this sure
    taker_max_shares: float = _f("TAKER_MAX_SHARES", 200)
    taker_start_before_end: int = _i("TAKER_START_BEFORE_END", 150)

    # maker layer: rest bids on both sides below fair so a complete set costs < $1
    # v3: лимитные заявки выключены — на данных они проигрывали (неблагоприятный отбор, −6% на $)
    maker_enabled: bool = os.getenv("MAKER_ENABLED", "0") == "1"
    maker_margin: float = _f("MAKER_MARGIN", 0.03)          # distance below fair for each bid
    maker_shares: float = _f("MAKER_SHARES", 50)
    maker_stop_before_end: int = _i("MAKER_STOP_BEFORE_END", 20)
    residual_max: float = _f("RESIDUAL_MAX", 50)            # max unpaired shares (directional residual)
    skew: float = _f("SKEW", 0.0006)                        # $ per share of imbalance to lean quotes toward pairing

    max_market_notional: float = _f("MAX_MARKET_NOTIONAL", 150)  # $ per market, both layers
    market_fraction: float = _f("MARKET_FRACTION", 0.05)          # и не больше этой доли капитала на рынок
    # v3: покупки по рынку только в коридоре цены; дешевле 0.30 модель ошибалась (−68% на $),
    # выше 0.95 — нечего заработать. Проверено на отложенной половине данных: +14.5% на $.
    # бумага честнее: заявка доходит через секунду и исполняется по свежему стакану (решение совета 28.09)
    taker_latency: float = _f("TAKER_LATENCY", 1.0)
    taker_min_price: float = _f("TAKER_MIN_PRICE", 0.30)
    taker_max_price: float = _f("TAKER_MAX_PRICE", 0.95)
    starting_cash: float = _f("STARTING_CASH", 1000)

    tick_seconds: float = _f("TICK_SECONDS", 0.25)
    db_path: str = os.getenv("DB_PATH", str(ROOT / "data" / "polydesk.db"))
    dashboard_port: int = _i("DASHBOARD_PORT", 8747)

    # live only — never logged
    private_key: str = os.getenv("POLY_PRIVATE_KEY", "")
    funder: str = os.getenv("POLY_FUNDER", "")
    signature_type: int = _i("POLY_SIGNATURE_TYPE", 1)
    clob_host: str = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
    chain_id: int = _i("CHAIN_ID", 137)

    gamma_host: str = "https://gamma-api.polymarket.com"
    rtds_url: str = "wss://ws-live-data.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    def public(self) -> dict:
        d = self.__dict__.copy()
        for k in ("private_key", "funder"):
            d[k] = "set" if d[k] else ""
        return d


settings = Settings()
