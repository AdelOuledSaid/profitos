"""Exercice de sauvegarde / restauration (chemin SQLite) + signature S3."""
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import backup as bk  # noqa: E402
import restore as rs  # noqa: E402

PASS = 'phrase-secrete-de-test-1234'
pytestmark = pytest.mark.skipif(shutil.which('openssl') is None, reason='openssl requis')


def _source(tmp_path):
    auth = tmp_path / 'src' / 'profitos_auth.db'
    tenants = tmp_path / 'src' / 'tenant_data'
    uploads = tmp_path / 'src' / 'uploads'
    tenants.mkdir(parents=True)
    c = sqlite3.connect(auth); c.execute('CREATE TABLE organizations(id INTEGER, name TEXT)')
    c.execute("INSERT INTO organizations VALUES(1,'Dupont')"); c.commit(); c.close()
    c = sqlite3.connect(tenants / 'org_1.db'); c.execute('CREATE TABLE invoices(n TEXT)')
    c.execute("INSERT INTO invoices VALUES('FA-1')"); c.commit(); c.close()
    (uploads / 'purchase_documents' / '1').mkdir(parents=True)
    (uploads / 'purchase_documents' / '1' / 'a.pdf').write_bytes(b'%PDF-1.4 facture')
    (uploads / 'purchase_documents' / '1' / 'tmp.pdf.part').write_bytes(b'partiel')
    return auth, tenants, uploads


def test_backup_then_restore_drill_sqlite(tmp_path):
    auth, tenants, uploads = _source(tmp_path)
    out = tmp_path / 'backups'
    result = bk.run_backup(backup_dir=out, passphrase=PASS, database_url='', auth_db=auth,
                           tenants_dir=tenants, upload_dir=uploads, env={})
    names = result['files']
    assert all(n.endswith('.enc') for n in names) and len(names) == 2
    # aucun fichier en clair ne reste sur le disque
    assert not [p for p in out.iterdir() if p.suffix in ('.gz', '.db')]
    assert b'Dupont' not in (out / names[0]).read_bytes()
    manifest = json.loads(Path(result['manifest']).read_text())
    assert manifest['database_backend'] == 'sqlite' and len(manifest['files']) == 2

    sqlite_dir, up_dir = tmp_path / 'r' / 'sqlite', tmp_path / 'r' / 'uploads'
    done = rs.restore(result['manifest'], PASS, sqlite_dir=sqlite_dir, uploads_dir=up_dir)
    assert sorted(done) == ['database', 'uploads']
    c = sqlite3.connect(sqlite_dir / 'profitos_auth.db')
    assert c.execute('SELECT name FROM organizations').fetchone()[0] == 'Dupont'; c.close()
    c = sqlite3.connect(sqlite_dir / 'tenant_data' / 'org_1.db')
    assert c.execute('SELECT n FROM invoices').fetchone()[0] == 'FA-1'; c.close()
    assert (up_dir / 'purchase_documents' / '1' / 'a.pdf').read_bytes() == b'%PDF-1.4 facture'
    assert not (up_dir / 'purchase_documents' / '1' / 'tmp.pdf.part').exists()


def test_restore_rejects_wrong_passphrase_tampering_and_non_empty_target(tmp_path):
    auth, tenants, uploads = _source(tmp_path)
    out = tmp_path / 'backups'
    result = bk.run_backup(backup_dir=out, passphrase=PASS, database_url='', auth_db=auth,
                           tenants_dir=tenants, upload_dir=uploads, env={})
    with pytest.raises(bk.BackupError):
        rs.restore(result['manifest'], 'mauvaise-phrase-secrete', sqlite_dir=tmp_path / 'x')
    target = tmp_path / 'occupied'; target.mkdir(); (target / 'f').write_text('x')
    with pytest.raises(bk.BackupError):
        rs.restore(result['manifest'], PASS, sqlite_dir=target)
    enc = out / result['files'][0]
    enc.write_bytes(enc.read_bytes() + b'x')
    with pytest.raises(bk.BackupError, match='SHA-256'):
        rs.restore(result['manifest'], PASS, sqlite_dir=tmp_path / 'y')


def test_passphrase_is_mandatory(tmp_path):
    with pytest.raises(bk.BackupError):
        bk.run_backup(backup_dir=tmp_path, passphrase='', env={})


def test_local_rotation_keeps_last_n(tmp_path):
    auth, tenants, uploads = _source(tmp_path)
    out = tmp_path / 'backups'
    for day in range(1, 5):
        bk.run_backup(backup_dir=out, passphrase=PASS, database_url='', auth_db=auth, tenants_dir=tenants,
                      upload_dir=uploads, keep=2, env={}, now=datetime(2026, 10, day, 2, tzinfo=timezone.utc))
    assert bk.list_backups(out) == ['20261003T020000Z', '20261004T020000Z']


def test_backup_dir_defaults_to_persistent_disk():
    assert bk.default_backup_dir({'PROFITOS_UPLOAD_DIR': '/var/data/uploads'}) == Path('/var/data/backups')
    assert bk.default_backup_dir({'BACKUP_DIR': '/x'}) == Path('/x')


def test_sigv4_signing_key_matches_aws_reference_vector():
    # Vecteur publié par AWS (« Deriving the signing key », service iam)
    key = bk.signing_key('wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY', '20120215', 'us-east-1', 'iam')
    assert key.hex() == 'f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d'


def test_s3_upload_when_configured(tmp_path, monkeypatch):
    auth, tenants, uploads = _source(tmp_path)
    puts = []

    class Resp:
        status_code = 200
    import requests

    def fake_put(url, data=None, headers=None, timeout=None):
        puts.append((url, headers, data.read()))
        return Resp()
    monkeypatch.setattr(requests, 'put', fake_put)
    env = {'BACKUP_S3_BUCKET': 'profitos-backups', 'BACKUP_S3_REGION': 'fr-par',
           'BACKUP_S3_ENDPOINT': 'https://s3.fr-par.scw.cloud', 'BACKUP_S3_ACCESS_KEY_ID': 'AK',
           'BACKUP_S3_SECRET_ACCESS_KEY': 'SK', 'BACKUP_S3_PREFIX': 'prod'}
    result = bk.run_backup(backup_dir=tmp_path / 'b', passphrase=PASS, database_url='', auth_db=auth,
                           tenants_dir=tenants, upload_dir=uploads, env=env)
    assert result['uploaded'] == 3  # 2 archives chiffrées + manifeste
    url, headers, body = puts[0]
    assert url.startswith('https://s3.fr-par.scw.cloud/profitos-backups/prod/profitos_')
    assert headers['Authorization'].startswith('AWS4-HMAC-SHA256 Credential=AK/')
    assert '/fr-par/s3/aws4_request' in headers['Authorization']
    assert headers['x-amz-content-sha256'] == bk.hashlib.sha256(body).hexdigest()
    assert headers['Content-Length'] == str(len(body))


def test_s3_requires_credentials():
    with pytest.raises(bk.BackupError):
        bk.s3_config({'BACKUP_S3_BUCKET': 'b'})


def test_backup_job_runs_script(tmp_path, monkeypatch):
    import profitos.runtime as rt
    from profitos import create_app, jobs, storage
    from profitos.config import DevelopmentConfig
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    monkeypatch.setenv('BACKUP_PASSPHRASE', PASS)
    monkeypatch.setenv('BACKUP_DIR', str(tmp_path / 'bk'))
    monkeypatch.delenv('BACKUP_S3_BUCKET', raising=False)
    monkeypatch.setenv('DATABASE_URL', '')
    app = create_app(DevelopmentConfig)
    report = jobs.run_job(app, 'backup')
    assert report['ok'] == [0]
    assert len(bk.list_backups(tmp_path / 'bk')) == 1
