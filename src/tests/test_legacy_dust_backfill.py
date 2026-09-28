import importlib
import os
from decimal import Decimal
from pathlib import Path

import pytest

from calc.dust_conversion import value_receipt
from tests.test_dust_settlement_mysql import database as temporary_database, seed

database = temporary_database


@pytest.fixture
def repair_module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts'))
    return importlib.import_module('backfill_legacy_dust')


def test_rounding_preserves_approved_soph_net(repair_module):
    gross, fee = repair_module.receipt_amounts({
        'exec_amount_usdt':'2.8974490838','service_charge_usdt':'0.0579449726'})
    assert gross == Decimal('2.95539405')
    assert gross-fee == Decimal('2.89744908')


mysql = pytest.mark.skipif(os.environ.get('DUST_MYSQL_TEST') != '1', reason='Opt-in temporary MySQL tables')


def legacy_case(database):
    service, original, at = seed(database)
    service._account(original)
    with database() as c:
        c.execute('DELETE FROM mi_dust_conversion_task')
        c.execute("SELECT id FROM mi_trade_order WHERE exchange_order_id='dust:123456'")
        oid = c.fetchone()['id']
    entry = {'asset':'TST','transaction_id':'123456','order_ids':[oid],'position_ids':[1],
             'receipt':value_receipt(original['conversion_json'],590)}
    return entry,(oid,1,Decimal('0.12'),Decimal('0.118')),at


@mysql
def test_backfill_updates_original_only_and_second_run_is_noop(database, repair_module):
    entry, approved, at = legacy_case(database)
    with database() as c:
        c.execute('SELECT COUNT(*) n FROM mi_trade_order')
        count = c.fetchone()['n']
        assert repair_module.repair(c,entry,approved)['delta'] == Decimal('-0.002')
    with database() as c:
        assert repair_module.repair(c,entry,approved)['status'] == 'already_applied'
        c.execute('SELECT COUNT(*) n FROM mi_trade_order')
        assert c.fetchone()['n'] == count
        c.execute('SELECT * FROM mi_trade_position WHERE id=1')
        pos = c.fetchone()
        assert pos['total_pnl'] == Decimal('0.118')
        assert pos['realized_pnl_spot'] == Decimal('0.02036')
        assert pos['funding_total_pnl'] == 0
        c.execute('SELECT * FROM mi_dust_conversion_task')
        tasks = c.fetchall()
        assert len(tasks) == 1 and tasks[0]['status'] == 'accounted'
        assert tasks[0]['event_at'] == at and tasks[0]['net_delta_usdt'] == Decimal('-0.002')
        c.execute("SELECT total_pnl_usdt,equity_usdt FROM mi_capital_snapshot WHERE exchange='total' ORDER BY snapshot_at")
        rows = c.fetchall()
        assert [r['total_pnl_usdt'] for r in rows] == [Decimal('0.1'),Decimal('0.118'),Decimal('0.118')]
        assert all(r['equity_usdt'] == 100 for r in rows)


@mysql
def test_backfill_failure_rolls_back_original_and_journal(database, repair_module, monkeypatch):
    entry, approved, _ = legacy_case(database)
    def fail(*args):
        raise RuntimeError('injected database failure')
    monkeypatch.setattr(repair_module.DustSettlement,'_correct_snapshots',fail)
    with pytest.raises(RuntimeError):
        with database() as c:
            repair_module.repair(c,entry,approved)
    with database() as c:
        c.execute('SELECT exec_amount-fee_amount_usdt AS net FROM mi_trade_order WHERE id=%s',(approved[0],))
        assert c.fetchone()['net'] == Decimal('0.12')
        c.execute('SELECT COUNT(*) n FROM mi_dust_conversion_task')
        assert c.fetchone()['n'] == 0


@mysql
def test_backfill_refuses_changed_original(database, repair_module):
    entry, approved, _ = legacy_case(database)
    with database() as c:
        c.execute('UPDATE mi_trade_order SET exec_amount=5 WHERE id=%s',(approved[0],))
    with pytest.raises(ValueError,match='original_valuation_changed'):
        with database() as c:
            repair_module.repair(c,entry,approved)
