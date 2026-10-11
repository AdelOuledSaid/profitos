"""Pool de connexions PostgreSQL (profitos/db.py) testé avec un faux pool.

PostgreSQL n'est pas disponible dans l'environnement de test : on vérifie le
protocole (nombre de checkouts, CREATE SCHEMA unique, SET search_path une fois
par checkout, restitution au teardown) avec des doubles en mémoire.
"""
import threading

import pytest
from flask import Flask

from profitos import db as dbmod


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.conn.log.append(sql)

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeRaw:
    _ids = 0

    def __init__(self):
        FakeRaw._ids += 1
        self.id = FakeRaw._ids
        self.log = []
        self.closed = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = 1


class PoolError(Exception):
    pass


class FakePool:
    def __init__(self, maxconn=10):
        self.maxconn = maxconn
        self.free = []
        self.used = set()
        self.getconn_calls = 0
        self.lock = threading.Lock()

    def getconn(self):
        with self.lock:
            self.getconn_calls += 1
            if len(self.used) >= self.maxconn:
                raise PoolError('connection pool exhausted')
            raw = self.free.pop() if self.free else FakeRaw()
            self.used.add(raw)
            return raw

    def putconn(self, raw, close=False):
        with self.lock:
            self.used.discard(raw)
            if close:
                raw.closed = 1
            else:
                self.free.append(raw)

    def closeall(self):
        pass


class FakePsycopg2:
    def __init__(self):
        self.direct = []

    def connect(self, *a, **k):
        raw = FakeRaw()
        self.direct.append(raw)
        return raw


@pytest.fixture
def fake_pool(monkeypatch):
    pool = FakePool()
    dbmod.reset_pool()
    monkeypatch.setattr(dbmod, '_make_pool', lambda: pool)
    monkeypatch.setattr(dbmod, 'psycopg2', FakePsycopg2(), raising=False)
    yield pool
    dbmod.reset_pool()


def _schema_creates(raw, schema):
    return [s for s in raw.log if s.startswith('CREATE SCHEMA') and schema in s]


def test_outside_flask_close_returns_connection_to_pool(fake_pool):
    c = dbmod.PGConnection(schema='org_7')
    raw = c._conn
    assert raw in fake_pool.used
    c.execute('SELECT 1')
    c.close()
    assert raw not in fake_pool.used and raw in fake_pool.free
    # double close inoffensif
    c.close()
    assert fake_pool.free.count(raw) == 1


def test_create_schema_only_once_per_process_and_search_path_once_per_checkout(fake_pool):
    for _ in range(5):
        c = dbmod.PGConnection(schema='org_7')
        c.execute('SELECT 1')
        c.execute('SELECT 2')
        c.close()
    all_logs = [s for raw in fake_pool.free for s in raw.log]
    assert len([s for s in all_logs if s.startswith('CREATE SCHEMA')]) == 1
    # 5 checkouts -> 5 SET search_path, et pas un par requête (10 requêtes)
    assert len([s for s in all_logs if s.startswith('SET search_path')]) == 5


def test_request_reuses_one_physical_connection_and_releases_at_teardown(fake_pool):
    app = Flask(__name__)
    dbmod.init_app(app)
    with app.test_request_context('/'):
        raws = set()
        for _ in range(12):  # ~ une page ProfitOS : 11-14 cx()/close()
            c = dbmod.PGConnection(schema='org_3')
            c.execute('SELECT 1')
            raws.add(c._conn)
            c.close()
        assert len(raws) == 1
        assert fake_pool.getconn_calls == 1
        assert len(fake_pool.used) == 1  # gardée pour la requête
        held = next(iter(raws))
        assert len([s for s in held.log if s.startswith('SET search_path')]) == 1
    # teardown_appcontext -> restituée
    assert not fake_pool.used
    assert held in fake_pool.free


def test_concurrent_connections_in_same_request_stay_distinct(fake_pool):
    app = Flask(__name__)
    dbmod.init_app(app)
    with app.test_request_context('/'):
        a = dbmod.PGConnection(schema='org_3')
        b = dbmod.PGConnection(schema='org_3')
        assert a._conn is not b._conn  # transactions indépendantes comme en SQLite
        a.close(); b.close()
        reused = dbmod.PGConnection(schema='org_3')
        assert reused._conn in (a._conn, b._conn)  # réutilisation après close
        auth = dbmod.PGConnection(schema='public')
        assert auth._conn is not reused._conn  # jamais partagée entre schémas
        reused.close(); auth.close()
    assert not fake_pool.used


def test_close_rolls_back_uncommitted_work(fake_pool):
    c = dbmod.PGConnection(schema='org_1')
    raw = c._conn
    before = raw.rollbacks
    c.close()
    assert raw.rollbacks > before


def test_pool_exhausted_falls_back_to_direct_connection(fake_pool, monkeypatch):
    monkeypatch.setattr(fake_pool, 'maxconn', 1)
    a = dbmod.PGConnection(schema='org_1')
    b = dbmod.PGConnection(schema='org_1')
    assert b._origin == 'direct'
    b.close()
    assert b._conn.closed == 1  # connexion directe fermée, jamais mise au pool
    a.close()
    assert b._conn not in fake_pool.free


def test_closed_connection_from_pool_is_replaced(fake_pool):
    dead = FakeRaw(); dead.closed = 1
    fake_pool.free.append(dead)
    c = dbmod.PGConnection(schema='org_2')
    assert c._conn is not dead
    c.close()


def test_pg_connection_context_manager_sqlite(tmp_path):
    with dbmod.pg_connection(None, tmp_path / 'auth.db') as c:
        assert c.execute('SELECT 1 AS ok').fetchone()['ok'] == 1


def test_pool_size_from_env_default():
    assert dbmod.PG_POOL_MAX >= 1
    src = open(dbmod.__file__, encoding='utf-8').read()
    assert "PG_POOL_MAX', '10'" in src
    assert 'ThreadedConnectionPool' in src


def test_create_app_registers_teardown():
    src = open(dbmod.__file__.replace('db.py', '__init__.py'), encoding='utf-8').read()
    assert '_dbmod.init_app(app)' in src
