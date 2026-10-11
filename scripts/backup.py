"""ProfitOS — sauvegarde chiffrée de la base et des documents clients.

Usage :
    python scripts/backup.py                 # base + documents
    python scripts/backup.py --no-uploads    # base seule
    python scripts/backup.py --keep 14

Étapes :
  1. base : PostgreSQL -> pg_dump --format=custom ; SQLite (dev) -> archive
     cohérente (API backup de sqlite3) de la base AUTH et des bases tenant ;
  2. documents : archive tar.gz de PROFITOS_UPLOAD_DIR ;
  3. chiffrement AES-256-CBC (openssl enc -pbkdf2, 200 000 itérations) avec
     la phrase secrète BACKUP_PASSPHRASE (jamais écrite sur disque ni loguée) ;
  4. manifeste JSON (empreintes SHA-256) ;
  5. envoi vers un bucket S3 compatible si BACKUP_S3_BUCKET est défini
     (signature AWS SigV4, PUT simple jusqu'à 5 Go), sinon conservation des
     BACKUP_KEEP dernières sauvegardes dans BACKUP_DIR (disque persistant).

Variables : BACKUP_PASSPHRASE (obligatoire), BACKUP_DIR, BACKUP_KEEP (7),
BACKUP_KEEP_LOCAL_WITH_S3 (2), BACKUP_S3_BUCKET, BACKUP_S3_ENDPOINT,
BACKUP_S3_REGION, BACKUP_S3_ACCESS_KEY_ID, BACKUP_S3_SECRET_ACCESS_KEY,
BACKUP_S3_PREFIX. Restauration : scripts/restore.py et RESTORE.md.
"""
import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OPENSSL_ARGS = ['-aes-256-cbc', '-pbkdf2', '-iter', '200000', '-md', 'sha256']
PREFIX = 'profitos_'


class BackupError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def default_backup_dir(env=None):
    env = os.environ if env is None else env
    if env.get('BACKUP_DIR'):
        return Path(env['BACKUP_DIR'])
    upload = env.get('PROFITOS_UPLOAD_DIR')
    if upload:  # /var/data/uploads -> /var/data/backups (même disque persistant)
        return Path(upload).resolve().parent / 'backups'
    return ROOT / 'backups'


def s3_config(env=None):
    env = os.environ if env is None else env
    bucket = (env.get('BACKUP_S3_BUCKET') or '').strip()
    if not bucket:
        return None
    region = (env.get('BACKUP_S3_REGION') or 'eu-west-3').strip()
    cfg = {
        'bucket': bucket,
        'region': region,
        'endpoint': (env.get('BACKUP_S3_ENDPOINT') or f'https://s3.{region}.amazonaws.com').rstrip('/'),
        'access_key': (env.get('BACKUP_S3_ACCESS_KEY_ID') or '').strip(),
        'secret_key': (env.get('BACKUP_S3_SECRET_ACCESS_KEY') or '').strip(),
        'prefix': (env.get('BACKUP_S3_PREFIX') or 'profitos/').strip(),
    }
    if not cfg['access_key'] or not cfg['secret_key']:
        raise BackupError('BACKUP_S3_ACCESS_KEY_ID et BACKUP_S3_SECRET_ACCESS_KEY sont requis avec BACKUP_S3_BUCKET.')
    if cfg['prefix'] and not cfg['prefix'].endswith('/'):
        cfg['prefix'] += '/'
    return cfg


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _openssl(args, passphrase):
    env = dict(os.environ, PROFITOS_BACKUP_PASS=passphrase)
    try:
        subprocess.run(['openssl', 'enc'] + args + ['-pass', 'env:PROFITOS_BACKUP_PASS'],
                       check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise BackupError("openssl introuvable dans l'environnement d'exécution.")
    except subprocess.CalledProcessError as exc:
        raise BackupError('Chiffrement/déchiffrement openssl en échec (phrase secrète incorrecte ?) : '
                          + (exc.stderr or b'').decode('utf-8', 'replace').strip()[:200])


def encrypt_file(src, passphrase):
    src = Path(src)
    dst = src.with_name(src.name + '.enc')
    _openssl(['-salt'] + OPENSSL_ARGS + ['-in', str(src), '-out', str(dst)], passphrase)
    src.unlink()
    return dst


def decrypt_file(src, dst, passphrase):
    _openssl(['-d'] + OPENSSL_ARGS + ['-in', str(src), '-out', str(dst)], passphrase)
    return Path(dst)


# ---------------------------------------------------------------------------
# Contenu
# ---------------------------------------------------------------------------

def dump_database(dest_dir, stamp, database_url=None, auth_db=None, tenants_dir=None):
    """PostgreSQL : pg_dump custom ; SQLite : archive des bases (copie cohérente)."""
    database_url = database_url if database_url is not None else os.environ.get('DATABASE_URL', '')
    if database_url.startswith('postgres://'):
        database_url = 'postgresql://' + database_url[len('postgres://'):]
    if database_url.startswith('postgresql://'):
        dest = Path(dest_dir) / f'{PREFIX}{stamp}.pgdump'
        cmd = ['pg_dump', '--format=custom', '--no-owner', '--no-privileges', '--file', str(dest), '--dbname', database_url]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=3600)
        except FileNotFoundError:
            raise BackupError("pg_dump introuvable : installez le client PostgreSQL de même version majeure que le serveur.")
        except subprocess.CalledProcessError as exc:
            msg = (exc.stderr or b'').decode('utf-8', 'replace')
            raise BackupError('pg_dump en échec : ' + msg.replace(database_url, '***').strip()[:300])
        return dest, 'postgresql'
    if auth_db is None or tenants_dir is None:
        from profitos import runtime
        auth_db = auth_db or runtime.AUTH_DB
        tenants_dir = tenants_dir or runtime.TENANTS
    dest = Path(dest_dir) / f'{PREFIX}{stamp}.sqlite.tar.gz'
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / 'tenant_data').mkdir()
        sources = [(Path(auth_db), tmp / 'profitos_auth.db')]
        sources += [(p, tmp / 'tenant_data' / p.name) for p in sorted(Path(tenants_dir).glob('org_*.db'))]
        for src, dst in sources:
            if not src.exists():
                continue
            s = sqlite3.connect(str(src)); d = sqlite3.connect(str(dst))
            try:
                s.backup(d)
            finally:
                d.close(); s.close()
        with tarfile.open(dest, 'w:gz') as tar:
            for item in sorted(tmp.rglob('*.db')):
                tar.add(item, arcname=str(item.relative_to(tmp)))
    return dest, 'sqlite'


def archive_uploads(dest_dir, stamp, upload_dir=None):
    if upload_dir is None:
        from profitos.storage import UPLOAD_ROOT
        upload_dir = UPLOAD_ROOT
    upload_dir = Path(upload_dir)
    dest = Path(dest_dir) / f'{PREFIX}{stamp}.uploads.tar.gz'

    def _filter(info):
        return None if info.name.endswith('.part') or '/.preflight-' in info.name else info
    with tarfile.open(dest, 'w:gz') as tar:
        if upload_dir.exists():
            tar.add(upload_dir, arcname='uploads', filter=_filter)
    return dest


# ---------------------------------------------------------------------------
# S3 compatible (AWS SigV4, stdlib + requests)
# ---------------------------------------------------------------------------

def _hmac(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def signing_key(secret_key, datestamp, region, service='s3'):
    k = _hmac(('AWS4' + secret_key).encode('utf-8'), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, 'aws4_request')


def sigv4_headers(method, url, cfg, payload_hash, now=None):
    """En-têtes signés (style chemin : <endpoint>/<bucket>/<clé>)."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    parts = urlsplit(url)
    host = parts.netloc
    canonical_uri = quote(parts.path or '/', safe='/-_.~')
    canonical_headers = f'host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n'
    signed_headers = 'host;x-amz-content-sha256;x-amz-date'
    canonical_request = '\n'.join([method, canonical_uri, parts.query, canonical_headers, signed_headers, payload_hash])
    scope = f"{datestamp}/{cfg['region']}/s3/aws4_request"
    string_to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                                hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()])
    signature = hmac.new(signing_key(cfg['secret_key'], datestamp, cfg['region']),
                         string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    return {
        'x-amz-date': amz_date,
        'x-amz-content-sha256': payload_hash,
        'Authorization': (f"AWS4-HMAC-SHA256 Credential={cfg['access_key']}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
    }


def s3_object_url(cfg, name):
    key = cfg['prefix'] + name
    return f"{cfg['endpoint']}/{cfg['bucket']}/{quote(key, safe='/-_.~')}"


def s3_put(path, cfg, name=None):
    import requests
    path = Path(path)
    size = path.stat().st_size
    if size > 5 * 1024 ** 3:
        raise BackupError('Fichier > 5 Go : un envoi multipart serait nécessaire.')
    url = s3_object_url(cfg, name or path.name)
    headers = sigv4_headers('PUT', url, cfg, sha256_file(path))
    headers['Content-Length'] = str(size)
    with open(path, 'rb') as fh:
        resp = requests.put(url, data=fh, headers=headers, timeout=(10, 600))
    if resp.status_code not in (200, 201):
        raise BackupError(f'Envoi S3 refusé (HTTP {resp.status_code}).')
    return url


def s3_get(cfg, name, dest):
    import requests
    url = s3_object_url(cfg, name)
    empty = hashlib.sha256(b'').hexdigest()
    with requests.get(url, headers=sigv4_headers('GET', url, cfg, empty), stream=True, timeout=(10, 600)) as resp:
        if resp.status_code != 200:
            raise BackupError(f'Téléchargement S3 refusé (HTTP {resp.status_code}).')
        with open(dest, 'wb') as fh:
            for chunk in resp.iter_content(1024 * 1024):
                fh.write(chunk)
    return Path(dest)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def list_backups(backup_dir):
    return sorted(p.name[len(PREFIX):-len('.manifest.json')]
                  for p in Path(backup_dir).glob(f'{PREFIX}*.manifest.json'))


def prune_local(backup_dir, keep):
    stamps = list_backups(backup_dir)
    removed = []
    for stamp in stamps[:-keep] if keep > 0 else stamps:
        for p in Path(backup_dir).glob(f'{PREFIX}{stamp}.*'):
            p.unlink()
        removed.append(stamp)
    return removed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_backup(include_db=True, include_uploads=True, backup_dir=None, keep=None, passphrase=None,
               database_url=None, auth_db=None, tenants_dir=None, upload_dir=None, env=None, now=None):
    env = os.environ if env is None else env
    passphrase = passphrase if passphrase is not None else env.get('BACKUP_PASSPHRASE', '')
    if len(passphrase or '') < 16:
        raise BackupError('BACKUP_PASSPHRASE (16 caractères minimum) est obligatoire : les sauvegardes sont toujours chiffrées.')
    backup_dir = Path(backup_dir or default_backup_dir(env))
    backup_dir.mkdir(parents=True, exist_ok=True)
    s3 = s3_config(env)
    keep = int(keep if keep is not None else (env.get('BACKUP_KEEP_LOCAL_WITH_S3', 2) if s3 else env.get('BACKUP_KEEP', 7)))
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y%m%dT%H%M%SZ')
    files, backend = [], None
    try:
        if include_db:
            path, backend = dump_database(backup_dir, stamp, database_url, auth_db, tenants_dir)
            files.append(encrypt_file(path, passphrase))
        if include_uploads:
            files.append(encrypt_file(archive_uploads(backup_dir, stamp, upload_dir), passphrase))
    except Exception:
        for p in backup_dir.glob(f'{PREFIX}{stamp}.*'):
            p.unlink()
        raise
    manifest = {
        'stamp': stamp,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'database_backend': backend,
        'encryption': 'openssl enc ' + ' '.join(OPENSSL_ARGS),
        'files': [{'name': p.name, 'sha256': sha256_file(p), 'bytes': p.stat().st_size} for p in files],
    }
    manifest_path = backup_dir / f'{PREFIX}{stamp}.manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    uploaded = []
    if s3:
        for p in files + [manifest_path]:
            uploaded.append(s3_put(p, cfg=s3))
    removed = prune_local(backup_dir, keep)
    return {'stamp': stamp, 'files': [p.name for p in files], 'manifest': str(manifest_path),
            'uploaded': len(uploaded), 'pruned': removed}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-db', action='store_true')
    ap.add_argument('--no-uploads', action='store_true')
    ap.add_argument('--keep', type=int)
    ap.add_argument('--dir')
    args = ap.parse_args(argv)
    try:
        result = run_backup(include_db=not args.no_db, include_uploads=not args.no_uploads,
                            backup_dir=args.dir, keep=args.keep)
    except BackupError as exc:
        print(f'[ProfitOS] ERREUR sauvegarde : {exc}')
        return 1
    print(f"[ProfitOS] sauvegarde {result['stamp']} : {', '.join(result['files'])} "
          f"(S3 : {result['uploaded']} fichier(s), rotation : {len(result['pruned'])} supprimée(s))")
    return 0


if __name__ == '__main__':
    sys.exit(main())
