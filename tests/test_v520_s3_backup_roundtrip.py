"""v520 — Exercice de sauvegarde et de restauration contre un vrai serveur S3.

Les contrôles existants (v406) remplaçaient ``requests.put`` par une fonction factice :
ils prouvaient que la requête était *formée*, jamais qu'elle serait *acceptée*. Une
signature SigV4 fausse serait passée inaperçue jusqu'au jour d'un sinistre.

Ici un serveur S3 minimal tourne en local. Il recalcule la signature à partir de la
requête reçue, exactement comme le ferait Amazon ou Scaleway, et répond 403 si elle ne
correspond pas. Le test fait ensuite le chemin complet : sauvegarde → envoi → effacement
du disque local → téléchargement depuis le bucket → déchiffrement → restauration, et
vérifie que les données retrouvées sont les données d'origine.
"""
import hashlib
import hmac
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import pytest

import scripts.backup as bk
import scripts.restore as rs

PASS = 'phrase-secrete-de-sauvegarde-520'
ACCESS_KEY = 'AKIAIOSFODNN7EXAMPLE'
SECRET_KEY = 'wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY'
REGION = 'fr-par'
BUCKET = 'veqalio-sauvegardes'


# ---------------------------------------------------------------------------
# Serveur S3 minimal qui vérifie réellement la signature
# ---------------------------------------------------------------------------

class _Store:
    def __init__(self):
        self.objects = {}
        self.rejected = []
        self.deleted = []
        self.page_size = 2        # volontairement petit : force la pagination


def _expected_signature(method, path, query, headers, payload_hash):
    """Recalcule la signature côté serveur, à partir de la seule requête reçue."""
    auth = headers.get('Authorization', '')
    scope = auth.split('Credential=', 1)[1].split(', ', 1)[0].split('/', 1)[1]
    datestamp, region, service, _ = scope.split('/')
    signed_headers = auth.split('SignedHeaders=', 1)[1].split(', ', 1)[0]
    canonical_headers = ''.join(
        f'{h}:{headers.get(h, "")}\n' for h in signed_headers.split(';'))
    canonical_request = '\n'.join([method, path, query, canonical_headers,
                                   signed_headers, payload_hash])
    string_to_sign = '\n'.join(['AWS4-HMAC-SHA256', headers.get('x-amz-date', ''), scope,
                                hashlib.sha256(canonical_request.encode()).hexdigest()])
    key = bk.signing_key(SECRET_KEY, datestamp, region, service)
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    store = None

    def log_message(self, *a):  # pas de bruit dans la sortie des tests
        pass

    def _check(self, payload_hash):
        parts = urlsplit(self.path)
        headers = {k.lower(): v for k, v in self.headers.items()}
        headers['Authorization'] = self.headers.get('Authorization', '')
        given = headers['Authorization'].split('Signature=', 1)[-1]
        expected = _expected_signature(self.command, parts.path, parts.query,
                                       headers, payload_hash)
        if not hmac.compare_digest(given, expected):
            self.store.rejected.append(self.path)
            self.send_response(403)
            self.end_headers()
            return None
        return parts.path

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        path = self._check(hashlib.sha256(body).hexdigest())
        if path is None:
            return
        self.store.objects[path] = body
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_DELETE(self):
        path = self._check(hashlib.sha256(b'').hexdigest())
        if path is None:
            return
        self.store.objects.pop(path, None)
        self.store.deleted.append(path)
        self.send_response(204)
        self.end_headers()

    def _list(self, parts):
        """ListObjectsV2 minimal, avec pagination pour éprouver le jeton de suite."""
        params = dict(p.split('=', 1) for p in parts.query.split('&') if '=' in p)
        prefix = unquote(params.get('prefix', ''))
        token = unquote(params.get('continuation-token', ''))
        base = f'/{BUCKET}/'
        keys = sorted(k[len(base):] for k in self.store.objects if k.startswith(base + prefix))
        start = keys.index(token) if token and token in keys else 0
        page, rest = keys[start:start + self.store.page_size], keys[start + self.store.page_size:]
        body = ['<?xml version="1.0" encoding="UTF-8"?>',
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
        body += [f'<Contents><Key>{k}</Key></Contents>' for k in page]
        body.append('<IsTruncated>true</IsTruncated>' if rest else '<IsTruncated>false</IsTruncated>')
        if rest:
            body.append(f'<NextContinuationToken>{rest[0]}</NextContinuationToken>')
        body.append('</ListBucketResult>')
        raw = ''.join(body).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self._check(hashlib.sha256(b'').hexdigest())
        if path is None:
            return
        parts = urlsplit(self.path)
        if 'list-type=2' in parts.query:
            return self._list(parts)
        body = self.store.objects.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def bucket(monkeypatch):
    store = _Store()
    handler = type('H', (_Handler,), {'store': store})
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f'127.0.0.1:{server.server_address[1]}'
    # le proxy sortant ne doit pas s'interposer sur la boucle locale
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    store.env = {
        'BACKUP_S3_BUCKET': BUCKET,
        'BACKUP_S3_REGION': REGION,
        'BACKUP_S3_ENDPOINT': f'http://{host}',
        'BACKUP_S3_ACCESS_KEY_ID': ACCESS_KEY,
        'BACKUP_S3_SECRET_ACCESS_KEY': SECRET_KEY,
        'BACKUP_S3_PREFIX': 'production/',
    }
    try:
        yield store
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Jeu de données source
# ---------------------------------------------------------------------------

def _source(tmp_path):
    auth = tmp_path / 'auth.db'
    tenants = tmp_path / 'tenants'
    uploads = tmp_path / 'uploads'
    tenants.mkdir()
    uploads.mkdir()
    c = sqlite3.connect(auth)
    c.execute('CREATE TABLE organizations (id INTEGER PRIMARY KEY, name TEXT)')
    c.execute("INSERT INTO organizations VALUES (1, 'Atelier Martin')")
    c.commit()
    c.close()
    t = sqlite3.connect(tenants / 'org_1.db')
    t.execute('CREATE TABLE outgoing_invoices (id INTEGER PRIMARY KEY, number TEXT, total_ttc REAL)')
    t.execute("INSERT INTO outgoing_invoices VALUES (1, 'FA-2026-008', 1200.0)")
    t.commit()
    t.close()
    (uploads / 'facture.pdf').write_bytes(b'%PDF-1.4 contenu de test')
    return auth, tenants, uploads


# ---------------------------------------------------------------------------
# Les contrôles
# ---------------------------------------------------------------------------

def test_signature_is_accepted_by_a_server_that_verifies_it(tmp_path, bucket):
    """Si la signature était fausse, le serveur répondrait 403 et rien ne monterait."""
    auth, tenants, uploads = _source(tmp_path)
    result = bk.run_backup(backup_dir=tmp_path / 'b', passphrase=PASS, database_url='',
                           auth_db=auth, tenants_dir=tenants, upload_dir=uploads,
                           env=bucket.env)
    assert bucket.rejected == []
    assert result['uploaded'] == 3           # base + documents + manifeste
    assert len(bucket.objects) == 3
    assert all(k.startswith(f'/{BUCKET}/production/profitos_') for k in bucket.objects)


def test_a_tampered_signature_is_refused(tmp_path, bucket):
    """Contre-épreuve : le serveur n'accepte pas n'importe quoi."""
    auth, tenants, uploads = _source(tmp_path)
    env = dict(bucket.env, BACKUP_S3_SECRET_ACCESS_KEY='mauvaise-cle')
    with pytest.raises(bk.BackupError):
        bk.run_backup(backup_dir=tmp_path / 'b', passphrase=PASS, database_url='',
                      auth_db=auth, tenants_dir=tenants, upload_dir=uploads, env=env)
    assert bucket.rejected, 'le serveur aurait dû refuser la signature'


def test_full_drill_backup_to_s3_then_restore_from_s3(tmp_path, bucket):
    """L'exercice complet : le disque local est effacé, tout revient du bucket."""
    auth, tenants, uploads = _source(tmp_path)
    local = tmp_path / 'b'
    result = bk.run_backup(backup_dir=local, passphrase=PASS, database_url='',
                           auth_db=auth, tenants_dir=tenants, upload_dir=uploads,
                           env=bucket.env)
    stamp = result['stamp']

    # sinistre : le serveur et son disque persistant disparaissent
    for p in local.glob('*'):
        p.unlink()
    assert not list(local.glob('*'))

    # reconstruction depuis le bucket seul
    cfg = bk.s3_config(bucket.env)
    work = tmp_path / 'restauration'
    work.mkdir()
    manifest_name = f'{bk.PREFIX}{stamp}.manifest.json'
    bk.s3_get(cfg, manifest_name, work / manifest_name)
    manifest = json.loads((work / manifest_name).read_text(encoding='utf-8'))
    for entry in manifest['files']:
        bk.s3_get(cfg, entry['name'], work / entry['name'])
        # l'empreinte du manifeste doit correspondre à l'octet près
        assert bk.sha256_file(work / entry['name']) == entry['sha256']

    rs.restore(work / manifest_name, PASS, sqlite_dir=tmp_path / 'db',
               uploads_dir=tmp_path / 'up', yes=True)

    # les données sont bien celles du départ
    c = sqlite3.connect(tmp_path / 'db' / 'profitos_auth.db')
    assert c.execute('SELECT name FROM organizations').fetchone()[0] == 'Atelier Martin'
    c.close()
    t = sqlite3.connect(tmp_path / 'db' / 'tenant_data' / 'org_1.db')
    assert t.execute('SELECT number, total_ttc FROM outgoing_invoices').fetchone() == ('FA-2026-008', 1200.0)
    t.close()
    # uploads_dir est la racine des documents : l'archive y dépose son contenu, pas un
    # sous-dossier « uploads » de plus.
    assert (tmp_path / 'up' / 'facture.pdf').read_bytes() == b'%PDF-1.4 contenu de test'


def _backup(tmp_path, bucket, env, n):
    """n sauvegardes successives, horodatées à des instants distincts."""
    from datetime import datetime, timedelta, timezone as tz
    auth, tenants, uploads = _source(tmp_path)
    base = datetime(2026, 11, 1, 2, 0, tzinfo=tz.utc)
    stamps = []
    for i in range(n):
        r = bk.run_backup(backup_dir=tmp_path / 'b', passphrase=PASS, database_url='',
                          auth_db=auth, tenants_dir=tenants, upload_dir=uploads,
                          env=env, now=base + timedelta(days=i))
        stamps.append(r['stamp'])
    return stamps


def test_s3_rotation_keeps_the_last_n_and_deletes_whole_backups(tmp_path, bucket):
    """Sans purge, le bucket grossit indéfiniment. Avec BACKUP_S3_KEEP, il ne garde que
    les N dernières sauvegardes — et chaque suppression emporte les trois fichiers."""
    env = dict(bucket.env, BACKUP_S3_KEEP='2')
    stamps = _backup(tmp_path, bucket, env, 4)
    restants = sorted({k.split('profitos_')[1].split('.')[0] for k in bucket.objects})
    assert restants == sorted(stamps[-2:]), 'seules les deux dernières doivent rester'
    assert len(bucket.objects) == 6          # 2 sauvegardes × 3 fichiers
    assert len(bucket.deleted) == 6          # 2 sauvegardes × 3 fichiers supprimés


def test_s3_rotation_is_disabled_by_default(tmp_path, bucket):
    """Par défaut rien n'est supprimé dans le bucket : un effacement de trop coûte
    infiniment plus cher que quelques euros de stockage."""
    stamps = _backup(tmp_path, bucket, bucket.env, 3)
    assert len(bucket.objects) == 9
    assert bucket.deleted == []
    assert len(stamps) == 3


def test_s3_rotation_never_touches_a_foreign_object(tmp_path, bucket):
    """Le bucket peut être partagé : rien qui ne porte pas le nom d'une sauvegarde
    Veqalio ne doit disparaître."""
    env = dict(bucket.env, BACKUP_S3_KEEP='1')
    bucket.objects[f'/{BUCKET}/production/documents-importants.zip'] = b'ne pas supprimer'
    _backup(tmp_path, bucket, env, 3)
    assert f'/{BUCKET}/production/documents-importants.zip' in bucket.objects


def test_s3_rotation_ignores_an_interrupted_backup(tmp_path, bucket):
    """Une sauvegarde sans manifeste est incomplète : elle ne compte pas parmi les N
    conservées, sinon une interruption chasserait une sauvegarde saine du bucket."""
    env = dict(bucket.env, BACKUP_S3_KEEP='2')
    bucket.objects[f'/{BUCKET}/production/profitos_20991231T235959Z.sqlite.tar.gz.enc'] = b'partielle'
    stamps = _backup(tmp_path, bucket, env, 3)
    restants = sorted({k.split('profitos_')[1].split('.')[0] for k in bucket.objects})
    assert '20991231T235959Z' in restants, "l'orpheline n'est pas supprimée à l'aveugle"
    assert all(s in restants for s in stamps[-2:]), 'les deux dernières saines sont là'


def test_backup_result_reports_offsite_copy(tmp_path, bucket):
    """Le résultat doit permettre de dire, dans le journal, si la copie hors site a eu
    lieu — sans quoi une sauvegarde locale se lit comme une sauvegarde complète."""
    auth, tenants, uploads = _source(tmp_path)
    sans = bk.run_backup(backup_dir=tmp_path / 'b1', passphrase=PASS, database_url='',
                         auth_db=auth, tenants_dir=tenants, upload_dir=uploads, env={})
    assert sans['s3_configured'] is False and sans['uploaded'] == 0
    avec = bk.run_backup(backup_dir=tmp_path / 'b2', passphrase=PASS, database_url='',
                         auth_db=auth, tenants_dir=tenants, upload_dir=uploads, env=bucket.env)
    assert avec['s3_configured'] is True and avec['uploaded'] == 3


def test_object_key_is_encoded_once(tmp_path, bucket):
    """Un préfixe contenant un caractère à encoder ne doit pas être encodé deux fois.

    s3_object_url encode déjà la clé ; si sigv4_headers ré-encodait le chemin, le « % »
    deviendrait « %25 » et l'objet serait rangé sous un nom différent de celui demandé —
    invisible tant que les noms restent alphanumériques, fatal le jour où ils ne le sont
    plus (un préfixe avec un espace, par exemple)."""
    cfg = bk.s3_config(dict(bucket.env, BACKUP_S3_PREFIX='sauvegardes prod/'))
    url = bk.s3_object_url(cfg, 'profitos_20261119T200000Z.manifest.json')
    path = urlsplit(url).path
    assert '%2520' not in path
    # la clé telle que le serveur la lira doit redonner le chemin demandé
    assert unquote(path) == f'/{BUCKET}/sauvegardes prod/profitos_20261119T200000Z.manifest.json'
    headers = bk.sigv4_headers('PUT', url, cfg, hashlib.sha256(b'').hexdigest())
    signed_path = quote(unquote(path), safe='/-_.~')
    assert signed_path == path, 'le chemin signé diffère du chemin envoyé'
    assert headers['Authorization'].startswith('AWS4-HMAC-SHA256 ')
