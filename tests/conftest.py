import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Isolation des tests — AVANT tout import de l'application.
#
# profitos/runtime.py appelle load_dotenv() : sans cette protection, pytest lit
# le .env (ou les variables Windows) et tourne sur la base PostgreSQL de
# PRODUCTION, le Redis de production, et peut envoyer de vrais e-mails/SMS ou
# appeler Stripe/WeInvoice. Chaque lancement créait alors de vrais comptes de test
# dans la base clients.
#
# Ici : le .env n'est pas chargé, et chaque service externe est neutralisé.
# Les tests tournent toujours sur SQLite locale, en mode développement.
# ---------------------------------------------------------------------------
try:
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
except ImportError:
    pass

_EXTERNAL_SERVICE_VARS = (
    'DATABASE_URL', 'REDIS_URL',
    'RESEND_API_KEY', 'SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD',
    'STRIPE_SECRET_KEY', 'STRIPE_PUBLISHABLE_KEY', 'STRIPE_WEBHOOK_SECRET',
    'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'WEINVOICE_CLIENT_ID', 'WEINVOICE_CLIENT_SECRET',
    'WEINVOICE_INVOICE_CLIENT_ID', 'WEINVOICE_INVOICE_CLIENT_SECRET',
    'SWAN_CLIENT_ID', 'SWAN_CLIENT_SECRET',
    'POWENS_CLIENT_ID', 'POWENS_CLIENT_SECRET',
    'GOCARDLESS_ACCESS_TOKEN',
    'DROPBOX_CLIENT_ID', 'DROPBOX_CLIENT_SECRET',
    'GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET',
    'OPS_HEALTH_TOKEN',
)
_PG_TEST_URL = os.environ.get('PROFITOS_TEST_DATABASE_URL', '').strip()
for _name in _EXTERNAL_SERVICE_VARS:
    # Chaîne vide plutôt que suppression : même si un load_dotenv non intercepté
    # s'exécutait, il ne remplacerait pas une variable déjà définie.
    os.environ[_name] = ''
# Exécution volontaire sur PostgreSQL (base de test vide et dédiée, jamais la production) :
# PROFITOS_TEST_DATABASE_URL=postgresql://... pytest tests/test_v467_isolation_audit.py
# Le DATABASE_URL éventuellement présent dans l'environnement n'est jamais utilisé.
if _PG_TEST_URL.startswith(('postgres://', 'postgresql://')):
    os.environ['DATABASE_URL'] = _PG_TEST_URL
os.environ['PROFITOS_ENV'] = 'development'

import os
# Les tests tournent explicitement en environnement de développement : sans cette
# valeur, l'application se considère en production et refuse de démarrer sans clé.
os.environ.setdefault('PROFITOS_ENV', 'development')


if _PG_TEST_URL.startswith(('postgres://', 'postgresql://')):
    import pytest

    @pytest.fixture(autouse=True)
    def _postgres_clean_database():
        """Mode PostgreSQL : chaque test repart d'une base vide (schémas des entreprises et
        tables d'authentification supprimés), comme chaque test SQLite repart de fichiers
        temporaires neufs. Ne s'exécute qu'avec PROFITOS_TEST_DATABASE_URL."""
        from profitos import db as dbmod
        dbmod.reset_pool()          # aucune connexion du test précédent ne garde de verrou
        conn = dbmod.connect_auth(None)
        try:
            # Connexions oubliées par le test précédent (« idle in transaction ») : elles
            # bloqueraient DROP SCHEMA indéfiniment. Base de test dédiée : on les coupe.
            conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE datname=current_database() AND pid<>pg_backend_pid()")
            conn.execute("SET lock_timeout = '20s'")
            schemas = [r['nspname'] for r in conn.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE ? OR nspname='public'", ('org\\_%',)).fetchall()]
            for name in schemas:
                conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            conn.execute('CREATE SCHEMA public')
            conn.commit()
        finally:
            conn.close()
        # Caches du processus liés aux schémas supprimés (un serveur réel ne voit jamais sa
        # base effacée sous ses pieds) : on les vide, et on repart d'un pool neuf.
        import profitos.runtime as _rt
        dbmod._ensured_schemas.clear()
        _rt._tenant_schema_checked.clear()
        _rt._aging_refreshed.clear()
        dbmod.reset_pool()
        yield
