"""Учёт: траты видны в базе до исхода, неразрешённые рынки не теряются, перезапуск восстанавливает позиции."""
from polydesk.execution.paper import PaperExchange, Position
from polydesk.ledger import Ledger
from polydesk.markets import Market
from polydesk import engine as eng


def mk(start=1000):
    return Market("btc-updown-5m-%d" % start, "0xc", "q", start, start + 300, "UP", "DOWN")


def test_ledger_records_spend_before_settlement(tmp_path):
    lg = Ledger(str(tmp_path / "l.db"))
    m = mk()
    pos = Position(m.slug, up=100, down=0, spent=60.0, fees=1.0)
    lg.upsert_market(m, pos)
    row = lg.db.execute("select up, spent, fees, pnl, settled_at from markets where slug=?", (m.slug,)).fetchone()
    assert row == (100, 60.0, 1.0, None, None)


def test_unresolved_market_is_kept_pending_not_dropped():
    m = mk(1000)
    assert eng.market_action(now=1000 + 300 + 3600, m=m) == "pending"
    assert eng.market_action(now=1000 + 300 + 30, m=m) == "wait"
    m.outcome = "Up"
    assert eng.market_action(now=1000 + 300 + 200, m=m) == "retire"
    m2 = mk(2000)
    assert eng.market_action(now=2000 + 300 + 3 * 86400, m=m2) == "give_up"


def test_restore_unsettled_positions_and_cash(tmp_path):
    lg = Ledger(str(tmp_path / "l.db"))
    done, open_ = mk(1000), mk(2000)
    p1 = Position(done.slug, up=100, spent=50.0, fees=1.0, settled=True, payout=100.0)
    lg.upsert_market(done, p1)
    lg.upsert_market(open_, Position(open_.slug, down=80, spent=40.0, fees=0.5))
    ex = PaperExchange(cash=0)
    pending = eng.restore_positions(lg, ex, starting_cash=1000)
    assert set(pending) == {open_.slug}
    assert ex.positions[open_.slug].down == 80 and ex.positions[open_.slug].spent == 40.0
    # 1000 + реализовано (100 − 50 − 1) − потрачено в открытой позиции (40 + 0.5)
    assert abs(ex.cash - (1000 + 49 - 40.5)) < 1e-9


def test_each_fill_updates_market_row(tmp_path):
    lg = Ledger(str(tmp_path / "l.db"))
    m = mk(3000)
    ex = PaperExchange(cash=100)
    pos = ex.position(m.slug)
    pos.up, pos.spent, pos.fees = 50, 25.0, 0.3
    eng.persist_position(lg, m, ex)
    assert lg.db.execute("select spent from markets where slug=?", (m.slug,)).fetchone()[0] == 25.0


def test_rebuild_rows_from_fills(tmp_path):
    from polydesk.execution.paper import Fill
    lg = Ledger(str(tmp_path / "l.db"))
    m = mk(4000)
    lg.upsert_market(m)                      # строка без трат, как у выброшенных рынков
    lg.fill(Fill(1.0, m.slug, "Up", 0.5, 100, 0.5, "take"))
    lg.fill(Fill(2.0, m.slug, "Down", 0.4, 50, 0.0, "make"))
    fixed = eng.rebuild_from_fills(lg)
    assert fixed == [m.slug]
    row = lg.db.execute("select up, down, spent, fees, settled_at from markets where slug=?", (m.slug,)).fetchone()
    assert row == (100, 50, 70.0, 0.5, None)
    assert eng.rebuild_from_fills(lg) == []   # повторный запуск ничего не меняет


def test_ledger_epoch_starts_fresh_once(tmp_path):
    lg = Ledger(str(tmp_path / "l.db"))
    m = mk(5000)
    lg.upsert_market(m, Position(m.slug, up=10, spent=5.0, fees=0, settled=True, payout=10))
    assert lg.stats()["markets_settled"] == 1
    assert lg.apply_epoch("v3.1") is True                  # новая эпоха — журнал с нуля
    assert lg.stats()["markets_settled"] == 0
    lg.upsert_market(m, Position(m.slug, up=10, spent=5.0, fees=0, settled=True, payout=10))
    assert lg.apply_epoch("v3.1") is False                 # та же эпоха — ничего не трогаем
    assert lg.stats()["markets_settled"] == 1
