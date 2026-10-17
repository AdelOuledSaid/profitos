"""
ProfitOS V1.3.6 - Vérification d'une sauvegarde PostgreSQL custom-format.

Usage:
    python scripts/check_backup.py backups/profitos_YYYYMMDDTHHMMSSZ.dump
"""
import sys
import subprocess
from pathlib import Path

def main():
    if len(sys.argv)!=2:
        print("Usage: python scripts/check_backup.py <fichier.dump>")
        return 1
    path=Path(sys.argv[1])
    if not path.exists():
        print("ERREUR : fichier introuvable.")
        return 1
    try:
        result=subprocess.run(["pg_restore","--list",str(path)],check=True,capture_output=True,text=True)
    except FileNotFoundError:
        print("ERREUR : pg_restore n'est pas installé ou n'est pas dans PATH.")
        return 2
    except subprocess.CalledProcessError as exc:
        print("ERREUR : sauvegarde invalide ou illisible.")
        print((exc.stderr or "")[:500])
        return 3
    lines=[ln for ln in result.stdout.splitlines() if ln and not ln.startswith(";")]
    print("OK : archive lisible.")
    print("Objets listés :", len(lines))
    return 0

if __name__=="__main__":
    sys.exit(main())
