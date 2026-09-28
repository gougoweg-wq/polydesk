"""Gamma API: locate the 5-minute Up/Down market for a time slot, read its tokens and resolution."""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
import aiohttp
from .config import settings


@dataclass
class Market:
    slug: str
    condition_id: str
    question: str
    start_ts: int
    end_ts: int
    up_token: str
    down_token: str
    closed: bool = False
    outcome: str | None = None          # "Up" | "Down" | None
    fee_rate: float = settings.fee_rate

    def token(self, side: str) -> str:
        return self.up_token if side == "Up" else self.down_token

    def side_of(self, token: str) -> str | None:
        return "Up" if token == self.up_token else ("Down" if token == self.down_token else None)

    @property
    def state(self) -> str:
        now = time.time()
        if self.outcome:
            return "RESOLVED"
        if now >= self.end_ts:
            return "CLOSED"
        if now >= self.end_ts - settings.twap_seconds:
            return "FINAL_WINDOW"
        if now >= self.start_ts:
            return "OPEN"
        return "PRE"


def slot_ts(now: float | None = None) -> int:
    now = now or time.time()
    return int(now) // settings.duration * settings.duration


def slug_for(ts: int) -> str:
    return f"{settings.slug_prefix}-{ts}"


def _parse(m: dict) -> Market:
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m["clobTokenIds"]
    outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m["outcomes"]
    prices = json.loads(m.get("outcomePrices") or "[]") if isinstance(m.get("outcomePrices"), str) else (m.get("outcomePrices") or [])
    up_i = outcomes.index("Up")
    down_i = outcomes.index("Down")
    ts = int(m["slug"].rsplit("-", 1)[1])
    outcome = None
    if m.get("closed") and len(prices) == 2:
        if float(prices[up_i]) >= 0.99:
            outcome = "Up"
        elif float(prices[down_i]) >= 0.99:
            outcome = "Down"
    return Market(
        slug=m["slug"], condition_id=m["conditionId"], question=m.get("question", ""),
        start_ts=ts, end_ts=ts + settings.duration,
        up_token=tokens[up_i], down_token=tokens[down_i],
        closed=bool(m.get("closed")), outcome=outcome,
        fee_rate=(m.get("feeSchedule") or {}).get("rate", settings.fee_rate),
    )


async def fetch_market(session: aiohttp.ClientSession, slug: str) -> Market | None:
    for extra in ("", "&closed=true"):
        async with session.get(f"{settings.gamma_host}/markets?slug={slug}{extra}", timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                continue
            data = await r.json()
            if data:
                return _parse(data[0])
    return None


async def refresh(session: aiohttp.ClientSession, market: Market) -> Market:
    """Re-read closed/outcome for a market that has ended."""
    fresh = await fetch_market(session, market.slug)
    if fresh:
        market.closed = fresh.closed
        market.outcome = fresh.outcome
    return market
