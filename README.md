# polydesk

Polymarket **BTC 5-minute Up/Down** desk. A fair-probability model against the Chainlink-settled quote,
two execution layers, paper or live, and a terminal to watch it.

> Research tool. Paper mode simulates fills against the live order book. Live mode trades a wallet you
> fund yourself. Nothing here is a recommendation. Short-horizon crypto markets are a latency race.

## What it does

Every 300 s Polymarket opens `btc-updown-5m-<unix>`: resolves **Up** if the Chainlink 60-second TWAP at the
end of the window is ≥ the price at the start, else **Down**. Two tokens (Up/Down) trade on the CLOB; a
matched pair always redeems for exactly $1.

```
RTDS ws ─ Binance spot (leads) ──┐
RTDS ws ─ Chainlink btc/usd ─────┤→ fair P(Up) = Φ((E[final TWAP] − open ref) / σ√t)
CLOB ws ─ books + prints ────────┤
                                 ▼
          taker: favoured side trades below fair by > fee + margin → lift the ask (FAK)
          maker: bids on both sides `margin` below fair → matched set costs < $1;
                 skew toward pairing when inventory is one-sided; cap the unpaired residual
                                 ▼
          hold to resolution → winner redeems $1 → ledger (SQLite) → terminal
```

Fees: takers pay `shares × 0.07 × p × (1 − p)` (1.75¢/share at 50¢, 0.6¢ at 90¢); makers pay nothing.
The paper exchange charges the real fee on takes and fills a resting bid **only when a print goes through
it** (taker SELL at ≤ our price, capped at the printed size).

## Data check that motivated this (22 Sep 2026, 59 markets)

Joining Polymarket prints with Binance 1-s candles: at T−60 s a crude model was ≥97 % sure in 70 % of
markets, 39/39 correct, and the winning side still traded at ~0.975 → **+2.4¢/share net of fee**.
At T−120 s: +3.6¢ in 39 % of markets. Real, thin, and shared with every other bot doing the same.

## Run

```bash
cp .env.example .env            # paper mode by default
./run.sh                        # engine + terminal → http://localhost:8747
.venv/bin/python -m pytest tests
```

Live: put a **dedicated hot wallet** key and your funder address in `.env`, `MODE=live`, small
`MAX_MARKET_NOTIONAL`. Keys are read from `.env` only and never logged.

## Layout

- `polydesk/feeds/` — RTDS (Binance + Chainlink) and CLOB market websocket
- `polydesk/model.py` — fair value; `strategy.py` — taker + maker intents
- `polydesk/execution/paper.py` / `live.py` — same interface, simulated vs CLOB
- `polydesk/engine.py` — market lifecycle, tick loop, snapshot; `ledger.py` — SQLite
- `dashboard/index.html` — the terminal (polls `/api/state` once a second)
