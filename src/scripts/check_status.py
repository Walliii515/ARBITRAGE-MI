#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.database import db_manager
with db_manager.get_cursor() as cursor:
    cursor.execute('SELECT DISTINCT status FROM mi_trade_order')
    print([r['status'] for r in cursor.fetchall()])
    cursor.execute('SELECT COUNT(*) as cnt FROM mi_trade_order')
    print('Total rows:', cursor.fetchone()['cnt'])
