"""SQLite ledger: markets, fills, equity curve, event log. Plain sqlite3 — one writer, small rows."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
  slug TEXT PRIMARY KEY, start_ts INTEGER, end_ts INTEGER, outcome TEXT,
  up REAL DEFAULT 0, down REAL DEFAULT 0, spent REAL DEFAULT 0, fees REAL DEFAULT 0,
  payout REAL DEFAULT 0, pnl REAL, ref REAL, final REAL, settled_at REAL
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, slug TEXT, side TEXT, price REAL,
  shares REAL, fee REAL, kind TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, cash REAL, open_value REAL, equity REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS ticks (
  ts REAL, slug TEXT, p_up REAL, ref REAL, cur REAL, est REAL, sd REAL,
  bid_up REAL, ask_up REAL, bid_down REAL, ask_down REAL, last_up REAL
);
CREATE INDEX IF NOT EXISTS ix_ticks_slug ON ticks(slug);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Ledger:
    def __init__(self, path: str | None = None):
        p = Path(path or settings.db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def apply_epoch(self, epoch: str) -> bool:
        """Новая версия правил — чистый бумажный журнал (старый архивируется в отдельной ветке git)."""
        if not epoch:
            return False
        r = self.db.execute("SELECT v FROM meta WHERE k='epoch'").fetchone()
        if r and r[0] == epoch:
            return False
        for t in ("markets", "fills", "equity", "events", "ticks"):
            self.db.execute(f"DELETE FROM {t}")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('epoch', ?)", (epoch,))
        self.db.commit()
        return True

    def event(self, level: str, msg: str) -> None:
        self.db.execute("INSERT INTO events(ts,level,msg) VALUES(?,?,?)", (time.time(), level, msg))
        self.db.commit()

    def fill(self, f) -> None:
        self.db.execute("INSERT INTO fills(ts,slug,side,price,shares,fee,kind,note) VALUES(?,?,?,?,?,?,?,?)",
                        (f.ts, f.slug, f.side, f.price, f.shares, f.fee, f.kind, f.note))
        self.db.commit()

    def upsert_market(self, m, pos=None, ref=None, final=None) -> None:
        pnl = None
        if pos and pos.settled:
            pnl = pos.payout - pos.spent - pos.fees
        self.db.execute("""INSERT INTO markets(slug,start_ts,end_ts,outcome,up,down,spent,fees,payout,pnl,ref,final,settled_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(slug) DO UPDATE SET outcome=excluded.outcome, up=excluded.up, down=excluded.down,
              spent=excluded.spent, fees=excluded.fees, payout=excluded.payout, pnl=excluded.pnl,
              ref=COALESCE(excluded.ref, markets.ref), final=COALESCE(excluded.final, markets.final),
              settled_at=COALESCE(excluded.settled_at, markets.settled_at)""",
            (m.slug, m.start_ts, m.end_ts, m.outcome,
             pos.up if pos else 0, pos.down if pos else 0, pos.spent if pos else 0, pos.fees if pos else 0,
             pos.payout if pos else 0, pnl, ref, final, time.time() if (pos and pos.settled) else None))
        self.db.commit()

    def equity(self, cash: float, open_value: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO equity(ts,cash,open_value,equity) VALUES(?,?,?,?)",
                        (round(time.time()), cash, open_value, cash + open_value))
        self.db.commit()

    def tick(self, slug: str, fair, bu, au, bd, ad, last_up) -> None:
        self.db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (time.time(), slug, fair.p_up, fair.ref, fair.cur, fair.est_final, fair.sd, bu, au, bd, ad, last_up))

    def commit(self) -> None:
        self.db.commit()

    # ---- reads for the dashboard
    def recent_markets(self, n: int = 48) -> list[dict]:
        cur = self.db.execute("SELECT slug,start_ts,end_ts,outcome,up,down,spent,fees,payout,pnl,ref,final FROM markets ORDER BY start_ts DESC LIMIT ?", (n,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def recent_fills(self, n: int = 60) -> list[dict]:
        cur = self.db.execute("SELECT ts,slug,side,price,shares,fee,kind,note FROM fills ORDER BY id DESC LIMIT ?", (n,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def equity_curve(self, n: int = 2000) -> list[list[float]]:
        cur = self.db.execute("SELECT ts,equity FROM (SELECT ts,equity FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts", (n,))
        return [list(r) for r in cur.fetchall()]

    def recent_events(self, n: int = 40) -> list[dict]:
        cur = self.db.execute("SELECT ts,level,msg FROM events ORDER BY id DESC LIMIT ?", (n,))
        return [dict(ts=r[0], level=r[1], msg=r[2]) for r in cur.fetchall()]

    def stats(self) -> dict:
        r = self.db.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0), SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), COALESCE(SUM(fees),0), COALESCE(SUM(spent),0) FROM markets WHERE pnl IS NOT NULL").fetchone()
        f = self.db.execute("SELECT COUNT(*), COALESCE(SUM(shares*price),0) FROM fills WHERE kind IN ('take','make')").fetchone()
        f24 = self.db.execute("SELECT COUNT(*) FROM fills WHERE kind IN ('take','make') AND ts > ?", (time.time() - 86400,)).fetchone()
        return dict(markets_settled=r[0], realized_pnl=r[1], markets_won=r[2] or 0, fees=r[3], notional=r[4],
                    fills=f[0], fill_notional=f[1], fills_24h=f24[0])
