"""
ProfitOS V1.3.5 - Initialisation sûre de la table security_events.

Usage depuis la racine du projet :
    python scripts\init_security_events.py

Le script :
- charge DATABASE_URL depuis l'environnement ou .env local ;
- crée security_events uniquement si elle n'existe pas ;
- crée quelques index utiles ;
- ne supprime ni ne modifie aucune donnée existante ;
- vérifie ensuite que la table est accessible.
"""

import os
import sys
from pathlib import Path


def load_local_env():
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
        print("Ajoute DATABASE_URL dans ton .env local, sans me l'envoyer.")
        return 1

    try:
        import psycopg
    except ImportError:
        print('ERREUR : psycopg n’est pas installé.')
        print('Exécute : python -m pip install "psycopg[binary]"')
        return 1

    ddl = """
    CREATE TABLE IF NOT EXISTS security_events (
        id BIGSERIAL PRIMARY KEY,
        organization_id BIGINT,
        user_id BIGINT,
        event_type TEXT NOT NULL,
        outcome TEXT NOT NULL,
        target TEXT,
        ip_hash TEXT,
        user_agent TEXT,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_security_events_created_at
        ON security_events (created_at);

    CREATE INDEX IF NOT EXISTS idx_security_events_event_type
        ON security_events (event_type);

    CREATE INDEX IF NOT EXISTS idx_security_events_user_id
        ON security_events (user_id);

    CREATE INDEX IF NOT EXISTS idx_security_events_org_id
        ON security_events (organization_id);
    """

    try:
        with psycopg.connect(database_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
                cur.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public'
                      AND table_name='security_events'
                    ORDER BY ordinal_position
                """)
                columns = [row[0] for row in cur.fetchall()]
            conn.commit()
    except Exception as exc:
        print("ERREUR PostgreSQL :", type(exc).__name__ + ":", str(exc))
        print("Aucun secret n'a été affiché par le script.")
        return 2

    expected = [
        "id", "organization_id", "user_id", "event_type",
        "outcome", "target", "ip_hash", "user_agent", "created_at"
    ]

    missing = [c for c in expected if c not in columns]
    if missing:
        print("ERREUR : table créée mais colonnes manquantes :", ", ".join(missing))
        return 3

    print("OK : table security_events disponible.")
    print("Colonnes :", ", ".join(columns))
    print("Aucune donnée existante n'a été supprimée ou modifiée.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
