"""
ProfitOS V1.3.6 - Sauvegarde logique PostgreSQL.

Usage:
    python scripts/backup_postgres.py

Pré-requis:
- DATABASE_URL dans l'environnement ou .env
- pg_dump disponible dans le PATH

Le script crée un fichier .dump local horodaté dans backups/.
Aucun secret n'est affiché.
"""
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime, timezone


def load_env():
    if os.environ.get("DATABASE_URL"):
        return
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line=raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k,v=line.split("=",1)
        k=k.strip(); v=v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k]=v


def main():
    load_env()
    url=os.environ.get("DATABASE_URL")
    if not url:
        print("ERREUR : DATABASE_URL introuvable.")
        return 1

    backup_dir=Path(__file__).resolve().parents[1]/"backups"
    backup_dir.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest=backup_dir/f"profitos_{stamp}.dump"

    cmd=["pg_dump","--format=custom","--no-owner","--no-privileges","--file",str(dest),url]
    try:
        subprocess.run(cmd,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    except FileNotFoundError:
        print("ERREUR : pg_dump n'est pas installé ou n'est pas dans PATH.")
        return 2
    except subprocess.CalledProcessError as exc:
        print("ERREUR pg_dump :", (exc.stderr or "échec").strip()[:500])
        return 3

    print("OK : sauvegarde créée :", dest)
    print("Taille :", dest.stat().st_size, "octets")
    return 0


if __name__=="__main__":
    sys.exit(main())
