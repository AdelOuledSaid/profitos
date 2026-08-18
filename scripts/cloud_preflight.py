"""ProfitOS V1.2 — validation cloud + initialisation PostgreSQL.

Usage:
  python scripts/cloud_preflight.py
  python scripts/cloud_preflight.py --init-tenants

En production, DATABASE_URL doit être défini. Le script initialise le schéma
public d'authentification, teste la connexion et peut initialiser tous les
schémas org_<id> déjà connus.
"""
import argparse, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profitos.runtime import init_auth_db, initialize_all_tenant_schemas, database_readiness
from profitos import db as dbmod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--init-tenants', action='store_true')
    args = ap.parse_args()

    env = os.environ.get('PROFITOS_ENV','development')
    print(f'[ProfitOS] env={env} backend={dbmod.backend_name()}')
    if env == 'production' and not dbmod.database_url_configured():
        raise SystemExit('[ProfitOS] ERREUR: DATABASE_URL obligatoire en production.')

    init_auth_db()
    ok, backend, err = database_readiness()
    if not ok:
        raise SystemExit(f'[ProfitOS] ERREUR base {backend}: {err}')
    print(f'[ProfitOS] base {backend}: OK')

    if args.init_tenants:
        count = initialize_all_tenant_schemas()
        print(f'[ProfitOS] schémas tenant initialisés: {count}')

    secret = os.environ.get('PROFITOS_SECRET_KEY','')
    if env == 'production' and len(secret) < 32:
        raise SystemExit('[ProfitOS] ERREUR: PROFITOS_SECRET_KEY doit faire au moins 32 caractères.')

    print('[ProfitOS] preflight: OK')

if __name__ == '__main__':
    main()
