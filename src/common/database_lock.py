"""Connection-scoped locks shared by accounting and snapshot collection."""
from contextlib import contextmanager

from common.database import db_manager


@contextmanager
def database_lock(name, timeout=0):
    with db_manager.get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT GET_LOCK(%s, %s) AS acquired', (name, timeout))
            acquired = (cursor.fetchone() or {}).get('acquired') == 1
            try:
                yield acquired
            finally:
                if acquired:
                    cursor.execute('SELECT RELEASE_LOCK(%s)', (name,))
