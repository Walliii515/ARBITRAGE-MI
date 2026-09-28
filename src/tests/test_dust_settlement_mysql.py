"""Opt-in MySQL tests using ONLY connection-local TEMPORARY tables.

DUST_MYSQL_TEST=1 PYTHONPATH=src pytest -q src/tests/test_dust_settlement_mysql.py
The configured database is never altered; temporary tables shadow its names.
"""
import json
import os
import re
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pymysql
import pytest

from calc.dust_conversion import event_time
from calc.dust_settlement import DustSettlement, PENDING
from calc.exchange_desync_remediator import ExchangeDesyncRemediator, ExchangeDesyncRemediationConfig
from common.database import db_manager
from tests.test_dust_settlement import conversion, ledger, task

pytestmark = pytest.mark.skipif(os.environ.get('DUST_MYSQL_TEST') != '1', reason='Opt-in temporary MySQL tables')


@pytest.fixture
def database(monkeypatch):
    conn = pymysql.connect(**db_manager.config)
    source = Path(__file__).resolve().parents[1]
    schema = (source / 'schema/init_empty_database.sql').read_text()
    names = ('mi_trade_order', 'mi_trade_position', 'mi_capital_snapshot', 'mi_capital_daily_summary')
    with conn.cursor() as c:
        for name in names:
            statement = re.search(r'CREATE TABLE `' + name + r'` .*?;', schema, re.S).group()
            c.execute(statement.replace('CREATE TABLE', 'CREATE TEMPORARY TABLE', 1))
        migration = (source / 'migrations/039_create_dust_conversion_task.sql').read_text()
        start = migration.index('CREATE TABLE')
        statement = migration[start:migration.index(';', start)]
        c.execute(statement.replace('CREATE TABLE IF NOT EXISTS', 'CREATE TEMPORARY TABLE', 1))
    conn.commit()

    @contextmanager
    def cursor():
        try:
            with conn.cursor() as c:
                yield c
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    monkeypatch.setattr(db_manager, 'get_cursor', cursor)
    monkeypatch.setattr('calc.dust_settlement.database_lock', lambda *a, **k: nullcontext(True))
    try:
        yield cursor
    finally:
        conn.close()


def seed(database, *, event_ms=1788000000000, qty='0.1'):
    original = task('confirmed')
    original['conversion_json'] = conversion(qty=qty, at=event_ms)
    at = event_time(original['conversion_json'])
    with database() as c:
        c.execute("""INSERT INTO mi_trade_position
            (id,order_uuid,base_asset,spot_symbol,future_contract,status,opened_at,closed_at,
             spot_open_qty,spot_open_price,spot_open_amount,future_open_qty,future_open_price,
             future_open_contracts,future_quanto_multiplier,open_spread_bps,exchange_risk_type,
             realized_pnl,realized_pnl_spot,realized_pnl_future,spot_close_amount,future_close_amount)
            VALUES (1,'test-source','TST','TSTUSDT','TST_USDT','closed',%s,%s,
                    1,1,1,1,1.2,10,0.1,0,%s,0.1,0,0.1,0.9,1.1)""", (at-timedelta(days=1),at-timedelta(hours=1),PENDING))
        for i, order in enumerate(ledger()):
            c.execute("""INSERT INTO mi_trade_order
                (position_id,order_uuid,base_asset,order_side,market_type,trade_direction,status,
                 target_qty,target_amount,exec_qty,exec_amount,fee_amount_usdt,executed_at)
                VALUES (1,%s,'TST',%s,%s,'sell','executed',%s,%s,%s,%s,0,%s)""",
                (str(i),order['order_side'],order['market_type'],order['exec_qty'],order['exec_amount'],
                 order['exec_qty'],order['exec_amount'],at-timedelta(hours=1)))
        c.execute("""INSERT INTO mi_dust_conversion_task
            (id,batch_uuid,base_asset,status,requested_at,positions_json,expected_qty,conversion_json,transaction_id,event_at)
            VALUES (1,'test-batch','TST','confirmed',%s,%s,0.1,%s,'123456',%s)""",
            (at,json.dumps(original['positions_json']),json.dumps(original['conversion_json']),at))
        for stamp in (at-timedelta(minutes=1), at+timedelta(minutes=1), at+timedelta(days=1)):
            for exchange in ('binance','gate','total'):
                c.execute("""INSERT INTO mi_capital_snapshot
                    (snapshot_at,exchange,equity_usdt,realized_pnl_usdt,funding_pnl_usdt,fee_cost_usdt,total_pnl_usdt,detail)
                    VALUES (%s,%s,100,0.1,0,0,0.1,'{"source":"exchange_api"}')""", (stamp,exchange))
    remediator = ExchangeDesyncRemediator(MagicMock(), ExchangeDesyncRemediationConfig(enabled=True))
    remediator.executor.contract_meta = {'TST': {'quanto_multiplier': 0.1}}
    remediator.executor.fetch_binance_bnb_event_price.return_value = 600
    return DustSettlement(remediator), original, at


def test_atomic_accounting_actual_schema_duplicate_safe(database):
    service, original, at = seed(database)
    service._account(original)
    service._account(original)
    with database() as c:
        c.execute("SELECT * FROM mi_trade_order WHERE exchange_order_id='dust:123456'")
        rows = c.fetchall()
        assert len(rows) == 1
        assert rows[0]['exec_amount'] == Decimal('0.1224')
        assert rows[0]['fee_amount_usdt'] == Decimal('0.0024')
        assert rows[0]['executed_at'] == at
        c.execute('SELECT * FROM mi_trade_position WHERE id=1')
        pos = c.fetchone()
        assert pos['status'] == 'closed' and pos['exchange_risk_type'] is None
        assert pos['realized_pnl_spot'] == Decimal('0.0224')
        assert pos['total_pnl'] == Decimal('0.12')
        c.execute('SELECT * FROM mi_dust_conversion_task WHERE id=1')
        settled = c.fetchone()
        assert settled['status'] == 'accounted'
        assert settled['net_delta_usdt'] == Decimal('0.02')
        c.execute("SELECT total_pnl_usdt FROM mi_capital_snapshot WHERE exchange='total' ORDER BY snapshot_at")
        assert [r['total_pnl_usdt'] for r in c.fetchall()] == [Decimal('0.1'),Decimal('0.12'),Decimal('0.12')]
        c.execute("SELECT total_pnl_usdt FROM mi_capital_snapshot WHERE exchange='gate' ORDER BY snapshot_at")
        assert all(r['total_pnl_usdt'] == Decimal('0.1') for r in c.fetchall())


def test_failure_after_order_insert_rolls_back_then_restart_accounts_once(database, monkeypatch):
    service, original, _ = seed(database)
    correct = service._correct_snapshots
    monkeypatch.setattr(service, '_correct_snapshots', MagicMock(side_effect=RuntimeError('database disconnected')))
    with pytest.raises(RuntimeError):
        service._account(original)
    with database() as c:
        c.execute("SELECT COUNT(*) AS n FROM mi_trade_order WHERE exchange_order_id LIKE 'dust:%%'")
        assert c.fetchone()['n'] == 0
        c.execute('SELECT status FROM mi_dust_conversion_task WHERE id=1')
        assert c.fetchone()['status'] == 'confirmed'
        c.execute('SELECT realized_pnl_spot FROM mi_trade_position WHERE id=1')
        assert c.fetchone()['realized_pnl_spot'] == 0
    monkeypatch.setattr(service, '_correct_snapshots', correct)
    service._account(original)
    with database() as c:
        c.execute("SELECT COUNT(*) AS n FROM mi_trade_order WHERE exchange_order_id LIKE 'dust:%%'")
        assert c.fetchone()['n'] == 1


def test_partial_receipt_retains_remaining_cost_and_pending_marker(database):
    service, original, _ = seed(database, qty='0.05')
    service._account(original)
    with database() as c:
        c.execute('SELECT * FROM mi_trade_position WHERE id=1')
        pos = c.fetchone()
        assert pos['exchange_risk_type'] == PENDING
        assert pos['realized_pnl_spot'] == Decimal('0.0724')
        c.execute("SELECT SUM(IF(order_side='open',exec_qty,-exec_qty)) AS remaining FROM mi_trade_order WHERE market_type='spot'")
        assert c.fetchone()['remaining'] == Decimal('0.05')


@pytest.mark.parametrize('mutation', [
    "UPDATE mi_trade_order SET exec_amount=NULL WHERE market_type='spot' AND order_side='open'",
    "UPDATE mi_trade_order SET exec_qty=0.8 WHERE market_type='spot' AND order_side='close'",
    "UPDATE mi_trade_order SET exec_qty=0.8 WHERE market_type='future' AND order_side='close'",
])
def test_missing_cost_or_changed_inventory_does_not_write(database, mutation):
    service, original, _ = seed(database)
    with database() as c:
        c.execute(mutation)
    with pytest.raises(ValueError):
        service._account(original)
    with database() as c:
        c.execute("SELECT COUNT(*) AS n FROM mi_trade_order WHERE exchange_order_id LIKE 'dust:%%'")
        assert c.fetchone()['n'] == 0


def test_midnight_conversion_not_lost_or_counted_next_day(database):
    # 2026-08-30 00:00:30 Shanghai; first daily snapshot at 00:01:30.
    from api.trading_api import _dust_day_open_adjustment
    service, original, at = seed(database, event_ms=1788019230000)
    assert at == datetime(2026,8,30,0,0,30)
    service._account(original)
    prefix = _dust_day_open_adjustment('s.snapshot_at', 's.exchange')
    with database() as c:
        c.execute(f"SELECT snapshot_at,{prefix} AS prefix FROM mi_capital_snapshot s WHERE exchange='total' ORDER BY snapshot_at")
        assert [r['prefix'] for r in c.fetchall()] == [0,Decimal('0.02'),0]


def test_unique_receipt_per_asset_enforced_by_database(database):
    seed(database)
    with pytest.raises(pymysql.err.IntegrityError):
        with database() as c:
            c.execute("""INSERT INTO mi_dust_conversion_task
                (batch_uuid,base_asset,status,requested_at,positions_json,expected_qty,transaction_id)
                VALUES ('other-batch','TST','confirmed',NOW(),'[]',0.1,'123456')""")


@pytest.mark.parametrize('event_ms', [
    1788000000000,  # Event between the day's first and last snapshots.
    1788019230000,  # 00:00:30, before the day's first snapshot.
    1788105599000,  # 23:59:59, after the day's last snapshot.
])
def test_daily_summary_event_attribution_and_equity_preserved(database, event_ms):
    from api.trading_api import _dust_day_open_adjustment

    service, original, at = seed(database, event_ms=event_ms)
    day = at.replace(hour=0, minute=0, second=0, microsecond=0)
    with database() as c:
        for offset in (-1, 0, 1):
            date = day + timedelta(days=offset)
            c.execute("""INSERT INTO mi_capital_daily_summary
                (summary_date,first_snapshot_at,last_snapshot_at,first_equity_usdt,
                 last_equity_usdt,equity_sum_usdt,sample_count,first_gross_pnl_usdt,last_gross_pnl_usdt)
                VALUES (%s,%s,%s,100,101,201,2,0.1,0.1)""",
                (date.date(),date+timedelta(minutes=1),date+timedelta(hours=23,minutes=59)))
    service._account(original)
    service._account(original)
    prefix = _dust_day_open_adjustment('d.first_snapshot_at')
    with database() as c:
        c.execute(f"SELECT d.*,{prefix} AS prefix FROM mi_capital_daily_summary d ORDER BY summary_date")
        rows = c.fetchall()
        for row in rows:
            first_delta = Decimal('0.02') if row['first_snapshot_at'] >= at else Decimal(0)
            last_delta = Decimal('0.02') if row['last_snapshot_at'] >= at else Decimal(0)
            assert row['first_gross_pnl_usdt'] == Decimal('0.1') + first_delta
            assert row['last_gross_pnl_usdt'] == Decimal('0.1') + last_delta
            assert row['first_equity_usdt'] == 100
            assert row['last_equity_usdt'] == 101
            assert row['equity_sum_usdt'] == 201 and row['sample_count'] == 2
        # The midnight prefix belongs only to the event's calendar day.
        assert [r['prefix'] for r in rows] == [0, Decimal('0.02') if at < day+timedelta(minutes=1) else 0, 0]
        c.execute('SELECT equity_usdt,funding_pnl_usdt FROM mi_capital_snapshot')
        assert all(r['equity_usdt'] == 100 and r['funding_pnl_usdt'] == 0 for r in c.fetchall())
