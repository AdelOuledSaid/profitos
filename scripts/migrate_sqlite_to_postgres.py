"""Copie une installation locale SQLite ProfitOS vers PostgreSQL.

Pré-requis :
  DATABASE_URL=postgresql://...

Le script conserve les IDs pour maintenir memberships/action relations.
Il est conçu pour une migration initiale. Faites une sauvegarde avant usage.
"""
import os, sys, sqlite3, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not os.environ.get('DATABASE_URL'):
    raise SystemExit('DATABASE_URL est obligatoire.')

from profitos import db as dbmod
from profitos.runtime import AUTH_DB, TENANTS, init_auth_db, init_tenant_db


def sqlite_rows(path, table):
    cx=sqlite3.connect(path); cx.row_factory=sqlite3.Row
    try:
        cols=[r['name'] for r in cx.execute(f'PRAGMA table_info({table})')]
        rows=[dict(r) for r in cx.execute(f'SELECT * FROM {table}')]
        return cols, rows
    finally: cx.close()


def sqlite_tables(path):
    cx=sqlite3.connect(path)
    try:
        return [r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    finally: cx.close()


def copy_table(path, pg_conn, table):
    cols, rows = sqlite_rows(path, table)
    if not cols or not rows:
        return 0
    qcols=', '.join(cols)
    marks=', '.join('?' for _ in cols)
    copied=0
    for row in rows:
        vals=tuple(row[c] for c in cols)
        try:
            pg_conn.execute(f'INSERT INTO {table} ({qcols}) VALUES ({marks}) ON CONFLICT DO NOTHING', vals)
            copied += 1
        except Exception as e:
            raise RuntimeError(f'{table}: {e}') from e
    pg_conn.commit()
    return copied


def reset_serials(pg_conn, tables):
    # SERIAL sequences can lag after explicit-ID inserts. pg_get_serial_sequence
    # returns NULL for tables without SERIAL ids, so those are ignored.
    for table in tables:
        try:
            pg_conn.execute("SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE((SELECT MAX(id) FROM %s),1), true)" % table, (table,))
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()


def main():
    if not AUTH_DB.exists():
        raise SystemExit(f'Base SQLite auth introuvable: {AUTH_DB}')
    if not dbmod.USE_POSTGRES:
        raise SystemExit('DATABASE_URL invalide ou non PostgreSQL.')

    init_auth_db()
    public=dbmod.connect_auth(AUTH_DB)
    auth_tables=sqlite_tables(AUTH_DB)
    for t in auth_tables:
        n=copy_table(AUTH_DB, public, t); print(f'public.{t}: {n}')
    reset_serials(public, auth_tables); public.close()

    # Les organisations doivent maintenant exister dans Postgres.
    pg_auth=dbmod.connect_auth(AUTH_DB)
    org_ids=[int(r['id']) for r in pg_auth.execute('SELECT id FROM organizations ORDER BY id').fetchall()]
    pg_auth.close()

    for oid in org_ids:
        local=TENANTS/f'org_{oid}.db'
        if not local.exists():
            print(f'org_{oid}: aucun SQLite local, ignoré')
            continue
        init_tenant_db(oid)
        tc=dbmod.connect_tenant(oid, local)
        tables=sqlite_tables(local)
        for t in tables:
            n=copy_table(local, tc, t); print(f'org_{oid}.{t}: {n}')
        reset_serials(tc, tables); tc.close()

    print('Migration terminée. Vérifier /readyz et effectuer des tests métier avant bascule.')

if __name__=='__main__':
    main()
