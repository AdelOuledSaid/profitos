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
for _name in _EXTERNAL_SERVICE_VARS:
    # Chaîne vide plutôt que suppression : même si un load_dotenv non intercepté
    # s'exécutait, il ne remplacerait pas une variable déjà définie.
    os.environ[_name] = ''
os.environ['PROFITOS_ENV'] = 'development'

import os
# Les tests tournent explicitement en environnement de développement : sans cette
# valeur, l'application se considère en production et refuse de démarrer sans clé.
os.environ.setdefault('PROFITOS_ENV', 'development')
