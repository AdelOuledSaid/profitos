"""
ProfitOS V1.3.5 - Vérification locale du journal security_events.

Usage (CMD Windows):
    python scripts\check_security_events.py

Le script utilise DATABASE_URL depuis l'environnement ou le fichier .env
du projet. Il n'affiche ni mot de passe, ni token, ni IP/hash d'IP.
"""

import os
import sys
from pathlib import Path


def load_local_env():
    """Charge .env sans dépendance externe si DATABASE_URL n'est pas déjà définie."""
    if os.environ.get("DATABASE_URL"):
        return

    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def main():
    load_local_env()
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        print("ERREUR : DATABASE_URL est introuvable.")
        print("Ajoute DATABASE_URL dans ton fichier .env local, sans me l'envoyer.")
        return 1

    try:
        import psycopg
    except ImportError:
        print("ERREUR : le paquet psycopg n'est pas installé.")
        print('Exécute : pip install "psycopg[binary]"')
        return 1

    query = """
        SELECT id, event_type, outcome, user_id, organization_id, target, created_at
        FROM security_events
        ORDER BY id DESC
        LIMIT 20
    """

    try:
        with psycopg.connect(database_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(query)
                rows = cur.fetchall()
    except Exception as exc:
        print("ERREUR de connexion/requête PostgreSQL :")
        print(type(exc).__name__ + ":", str(exc))
        print("\nAucun secret n'a été affiché par le script.")
        return 2

    if not rows:
        print("La table security_events existe, mais aucun événement n'est encore enregistré.")
        return 0

    print("\n20 derniers événements de sécurité ProfitOS")
    print("-" * 105)
    print(f"{'ID':<6} {'EVENT':<25} {'OUTCOME':<10} {'USER':<8} {'ORG':<8} {'TARGET':<18} CREATED_AT")
    print("-" * 105)

    for row in rows:
        ident, event, outcome, user_id, org_id, target, created_at = row
        print(
            f"{str(ident):<6} "
            f"{str(event or ''):<25.25} "
            f"{str(outcome or ''):<10.10} "
            f"{str(user_id or '-'):<8.8} "
            f"{str(org_id or '-'):<8.8} "
            f"{str(target or '-'):<18.18} "
            f"{created_at}"
        )

    events = {(str(r[1]), str(r[2])) for r in rows}
    print("\nValidation V1.3.5 :")
    print("LOGIN_SUCCESS :", "OK" if ("LOGIN_SUCCESS", "SUCCESS") in events else "NON TROUVÉ")
    print("LOGIN_FAILED  :", "OK" if ("LOGIN_FAILED", "FAILURE") in events else "NON TROUVÉ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
