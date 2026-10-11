"""ProfitOS — restauration d'une sauvegarde produite par scripts/backup.py.

Voir RESTORE.md pour la procédure complète et l'exercice de restauration.

Exemples :
  # Sauvegarde locale (disque persistant) vers une base de test
  python scripts/restore.py --manifest /var/data/backups/profitos_20261009T020000Z.manifest.json \\
      --target-db "$RESTORE_DATABASE_URL" --uploads-dir /tmp/restore/uploads

  # Depuis le bucket S3 (BACKUP_S3_* définies)
  python scripts/restore.py --from-s3 20261009T020000Z --work-dir /tmp/restore \\
      --target-db "$RESTORE_DATABASE_URL" --uploads-dir /tmp/restore/uploads

  # Développement (SQLite)
  python scripts/restore.py --manifest backups/profitos_….manifest.json \\
      --sqlite-dir /tmp/restore/sqlite --uploads-dir /tmp/restore/uploads

La phrase secrète est lue dans BACKUP_PASSPHRASE. Les cibles non vides sont
refusées sans --yes (aucune écrasement accidentel de la production).
"""
import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup as bk  # noqa: E402


def _safe_extract(archive, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as tar:
        root = dest.resolve()
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(root)) or member.issym() or member.islnk():
                raise bk.BackupError(f'Entrée d\'archive refusée : {member.name}')
        try:
            tar.extractall(dest, filter='data')
        except TypeError:  # Python < 3.12
            tar.extractall(dest)


def _non_empty(path):
    p = Path(path)
    return p.exists() and any(p.iterdir())


def restore(manifest_path, passphrase, target_db=None, sqlite_dir=None, uploads_dir=None, yes=False, work_dir=None):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    src_dir = manifest_path.parent
    done = []
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        tmp = Path(tmp)
        for entry in manifest['files']:
            enc = src_dir / entry['name']
            if not enc.exists():
                raise bk.BackupError(f"Fichier manquant : {entry['name']}")
            if bk.sha256_file(enc) != entry['sha256']:
                raise bk.BackupError(f"Empreinte SHA-256 invalide : {entry['name']} (fichier altéré)")
            plain = bk.decrypt_file(enc, tmp / entry['name'][:-len('.enc')], passphrase)
            name = plain.name
            if name.endswith('.pgdump'):
                if not target_db:
                    continue
                cmd = ['pg_restore', '--clean', '--if-exists', '--no-owner', '--no-privileges',
                       '--dbname', target_db, str(plain)]
                try:
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except FileNotFoundError:
                    raise bk.BackupError('pg_restore introuvable.')
                except subprocess.CalledProcessError as exc:
                    msg = (exc.stderr or b'').decode('utf-8', 'replace').replace(target_db, '***')
                    raise bk.BackupError('pg_restore en échec : ' + msg.strip()[:300])
                done.append('database')
            elif name.endswith('.sqlite.tar.gz'):
                if not sqlite_dir:
                    continue
                if _non_empty(sqlite_dir) and not yes:
                    raise bk.BackupError(f'{sqlite_dir} n\'est pas vide (ajoutez --yes pour écraser).')
                _safe_extract(plain, sqlite_dir)
                done.append('database')
            elif name.endswith('.uploads.tar.gz'):
                if not uploads_dir:
                    continue
                if _non_empty(uploads_dir) and not yes:
                    raise bk.BackupError(f'{uploads_dir} n\'est pas vide (ajoutez --yes pour écraser).')
                with tempfile.TemporaryDirectory(dir=tmp) as staging:
                    _safe_extract(plain, staging)
                    src = Path(staging) / 'uploads'
                    Path(uploads_dir).mkdir(parents=True, exist_ok=True)
                    if src.exists():
                        for item in src.iterdir():
                            dest = Path(uploads_dir) / item.name
                            if dest.exists() and yes:
                                import shutil
                                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
                            item.replace(dest)
                done.append('uploads')
    return done


def main(argv=None):
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--manifest')
    src.add_argument('--from-s3', metavar='STAMP')
    ap.add_argument('--work-dir')
    ap.add_argument('--target-db', help='URL PostgreSQL cible (jamais la production sans --yes)')
    ap.add_argument('--sqlite-dir')
    ap.add_argument('--uploads-dir')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args(argv)
    passphrase = os.environ.get('BACKUP_PASSPHRASE', '')
    if not passphrase:
        print('[ProfitOS] ERREUR : BACKUP_PASSPHRASE requise.')
        return 1
    if args.target_db and args.target_db == os.environ.get('DATABASE_URL') and not args.yes:
        print('[ProfitOS] ERREUR : la cible est la base de production ; ajoutez --yes si c\'est voulu.')
        return 1
    try:
        manifest = args.manifest
        if args.from_s3:
            cfg = bk.s3_config()
            if not cfg:
                raise bk.BackupError('BACKUP_S3_BUCKET non configuré.')
            work = Path(args.work_dir or tempfile.mkdtemp(prefix='profitos-restore-'))
            work.mkdir(parents=True, exist_ok=True)
            mname = f'{bk.PREFIX}{args.from_s3}.manifest.json'
            bk.s3_get(cfg, mname, work / mname)
            for entry in json.loads((work / mname).read_text(encoding='utf-8'))['files']:
                bk.s3_get(cfg, entry['name'], work / entry['name'])
            manifest = work / mname
        done = restore(manifest, passphrase, args.target_db, args.sqlite_dir, args.uploads_dir, args.yes, args.work_dir)
    except bk.BackupError as exc:
        print(f'[ProfitOS] ERREUR restauration : {exc}')
        return 1
    print(f"[ProfitOS] restauration terminée : {', '.join(done) or 'rien (aucune cible indiquée)'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
