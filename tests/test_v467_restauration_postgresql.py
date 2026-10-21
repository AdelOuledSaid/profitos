"""v467 — Exercice de sauvegarde puis restauration sur PostgreSQL réel.

La base source (celle des parcours v461 / v462, donc remplie par de vrais parcours
clients) est sauvegardée par scripts/backup.py (pg_dump chiffré), puis restaurée par
scripts/restore.py dans une base neuve. Toutes les tables de tous les schémas
(authentification et une par entreprise) doivent être identiques ligne pour ligne, et les
documents déposés restaurés à l'octet près. Ignoré sans PROFITOS_E2E_DATABASE_URL.
"""
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

PG_URL = os.environ.get('PROFITOS_E2E_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not PG_URL.startswith(('postgres://', 'postgresql://')),
                                reason='exercice PostgreSQL : définir PROFITOS_E2E_DATABASE_URL')
ROOT = Path(__file__).resolve().parents[1]
PASSPHRASE = 'exercice-restauration-' + 'x' * 8


def _with_db(url, name):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, '/' + name, parts.query, parts.fragment))


def fingerprint(url):
    psycopg2 = pytest.importorskip('psycopg2')
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("""SELECT table_schema, table_name FROM information_schema.tables
                   WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema')
                   ORDER BY 1, 2""")
    out = {}
    for schema, table in cur.fetchall():
        cur.execute(f'SELECT * FROM "{schema}"."{table}"')
        rows = sorted(repr(r) for r in cur.fetchall())
        out[f'{schema}.{table}'] = (len(rows), hashlib.sha256('\n'.join(rows).encode()).hexdigest())
    conn.close()
    return out


def test_sauvegarde_puis_restauration_postgresql(tmp_path):
    psycopg2 = pytest.importorskip('psycopg2')
    sys.path.insert(0, str(ROOT / 'scripts'))
    import backup as bk
    import restore as rs
    src = fingerprint(PG_URL)
    orgs = sorted({k.split('.')[0] for k in src if k.startswith('org_')})
    assert orgs, 'base source vide : lancer d\'abord les parcours v461 / v462'
    assert sum(n for (n, _h) in src.values()) > 100

    uploads = tmp_path / 'uploads'
    (uploads / 'purchase_documents' / orgs[0].split('_')[1]).mkdir(parents=True)
    doc = uploads / 'purchase_documents' / orgs[0].split('_')[1] / 'facture.pdf'
    doc.write_bytes(b'%PDF-1.4\n' + os.urandom(2048))
    result = bk.run_backup(backup_dir=tmp_path / 'backups', passphrase=PASSPHRASE, database_url=PG_URL,
                           upload_dir=uploads, env={})
    assert len(result['files']) == 2 and all(f.endswith('.enc') for f in result['files'])

    target_name = urlsplit(PG_URL).path.lstrip('/') + '_restore'
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    admin.cursor().execute(f'DROP DATABASE IF EXISTS "{target_name}"')
    admin.cursor().execute(f'CREATE DATABASE "{target_name}"')
    target = _with_db(PG_URL, target_name)
    restored_uploads = tmp_path / 'restored_uploads'
    try:
        # Mauvaise phrase de passe : refus, rien n'est restauré.
        with pytest.raises(bk.BackupError):
            rs.restore(result['manifest'], 'mauvaise-phrase-de-passe', target_db=target, uploads_dir=restored_uploads, yes=True)
        rs.restore(result['manifest'], PASSPHRASE, target_db=target, uploads_dir=restored_uploads, yes=True)
        assert fingerprint(target) == src
        restored = restored_uploads / doc.relative_to(uploads)
        assert restored.read_bytes() == doc.read_bytes()
    finally:
        admin.cursor().execute(f'DROP DATABASE IF EXISTS "{target_name}"')
        admin.close()
