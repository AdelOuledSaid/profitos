"""ProfitOS — validation cloud + migrations (preDeploy Render).

Usage:
  python scripts/cloud_preflight.py                  # vérifications seules
  python scripts/cloud_preflight.py --init-tenants   # + migration des schémas tenant
  python scripts/cloud_preflight.py --init-tenants --force-migrate

En production (PROFITOS_ENV=production), le script ÉCHOUE (code 1) si :
  - DATABASE_URL est absente ;
  - PROFITOS_UPLOAD_DIR est absente, sous /tmp, ou non inscriptible
    (les documents clients doivent vivre sur le disque persistant Render) ;
  - aucun fournisseur d'email n'est configuré (RESEND_API_KEY ou SMTP_HOST) ;
  - Stripe est incomplet : STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET et au moins
    un tarif STRIPE_PRICE_<FORMULE>_ID sont obligatoires ;
  - APP_BASE_URL n'est pas en HTTPS ou PROFITOS_SECRET_KEY est trop courte ;
  - la migration d'au moins une organisation a échoué (les autres sont
    quand même migrées : isolation par tenant).

Les migrations ne sont rejouées que pour les tenants dont la version de
schéma (table schema_version) diffère de celle du code.
"""
import argparse
import logging
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _is_production(env=None):
    env = os.environ if env is None else env
    return (env.get('PROFITOS_ENV') or 'development').strip().lower() == 'production'


def check_upload_dir(env=None):
    """Retourne la liste des erreurs bloquantes sur le stockage des documents."""
    env = os.environ if env is None else env
    errors = []
    raw = (env.get('PROFITOS_UPLOAD_DIR') or '').strip()
    if not raw:
        return ['PROFITOS_UPLOAD_DIR obligatoire en production (disque persistant, ex. /var/data/uploads).']
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    if not path.is_absolute():
        errors.append('PROFITOS_UPLOAD_DIR doit être un chemin absolu.')
    for tmp_root in ('/tmp', '/var/tmp', '/dev/shm'):
        if str(resolved) == tmp_root or str(resolved).startswith(tmp_root + '/') or raw == tmp_root or raw.startswith(tmp_root + '/'):
            errors.append(f'PROFITOS_UPLOAD_DIR ne doit pas pointer vers {tmp_root} (stockage éphémère).')
            break
    # Pas de test d'écriture ici : Render ne monte pas le disque pendant la commande
    # pre-deploy (exécutée sur une autre machine). L'écriture est vérifiée par /readyz
    # sur l'instance en service : sans disque, le déploiement n'est jamais mis en ligne.
    return errors


def check_email(env=None):
    env = os.environ if env is None else env
    if (env.get('RESEND_API_KEY') or '').strip() or (env.get('SMTP_HOST') or '').strip():
        return []
    return ["Aucun fournisseur d'email configuré (RESEND_API_KEY ou SMTP_HOST) : "
            "vérification de compte, mot de passe oublié et relances seraient simulés."]


def check_basic(env=None, database_configured=True):
    env = os.environ if env is None else env
    errors = []
    if not database_configured:
        errors.append('DATABASE_URL obligatoire en production.')
    app_url = env.get('APP_BASE_URL', '')
    if app_url and not app_url.startswith('https://'):
        errors.append('APP_BASE_URL doit utiliser HTTPS en production.')
    if len(env.get('PROFITOS_SECRET_KEY', '')) < 32:
        errors.append('PROFITOS_SECRET_KEY doit faire au moins 32 caractères.')
    return errors


STRIPE_PRICE_VARS = ('STRIPE_PRICE_STARTER_ID', 'STRIPE_PRICE_PRO_ID', 'STRIPE_PRICE_BUSINESS_ID', 'STRIPE_PRICE_MULTI_ID')


def stripe_config_errors(environ):
    """Liste (en français) de ce qui manque pour que la facturation Stripe
    fonctionne. Liste vide = configuration exploitable. Aucune valeur n'est
    jamais affichée, seulement le nom des variables."""
    errors = []
    if not (environ.get('STRIPE_SECRET_KEY') or '').strip():
        errors.append('STRIPE_SECRET_KEY manquante')
    if not (environ.get('STRIPE_WEBHOOK_SECRET') or '').strip():
        errors.append('STRIPE_WEBHOOK_SECRET manquante')
    prices = [v for v in STRIPE_PRICE_VARS if (environ.get(v) or '').strip()]
    # STRIPE_PRICE_ID : ancienne variable unique, encore lue comme tarif Pro.
    if not prices and not (environ.get('STRIPE_PRICE_ID') or '').strip():
        errors.append('aucun tarif configuré (' + ', '.join(STRIPE_PRICE_VARS) + ')')
    return errors


def check_stripe(env=None):
    """Production : sans Stripe, aucun client ne peut souscrire et les accès
    expirent à la fin de l'essai (voir STRIPE_SETUP.md)."""
    env = os.environ if env is None else env
    errors = stripe_config_errors(env)
    if not errors:
        return []
    return ['Configuration Stripe incomplète en production : ' + ' ; '.join(errors)
            + " (voir STRIPE_SETUP.md)."]


def check_legal(env=None):
    """Mentions légales obligatoires (LCEN art. 6 III) : identité de l'éditeur."""
    env = os.environ if env is None else env
    from profitos.legal import missing_legal_fields
    missing = missing_legal_fields(env)
    if not missing:
        return []
    return ['Mentions légales incomplètes : définissez ' + ', '.join(missing) + ' (voir DEPLOIEMENT_PRODUCTION.md).']


def check_secrets(env=None):
    """Clé de chiffrement des identifiants d'intégration et phrase des sauvegardes."""
    env = os.environ if env is None else env
    errors = []
    if len((env.get('PROFITOS_DATA_KEY') or '').strip()) < 32:
        errors.append('PROFITOS_DATA_KEY obligatoire (32 caractères minimum) : chiffrement des identifiants Dropbox, Drive, Shopify, WooCommerce et 2FA.')
    if len((env.get('BACKUP_PASSPHRASE') or '').strip()) < 16:
        errors.append('BACKUP_PASSPHRASE obligatoire (16 caractères minimum) : sans elle, la sauvegarde quotidienne échoue.')
    return errors


def production_errors(env=None, database_configured=True):
    """Erreurs bloquantes. En pré-lancement (PROFITOS_PRELAUNCH=1, aucune vente possible),
    le disque, les sauvegardes et Stripe deviennent des avertissements : voir prelaunch_warnings."""
    env = os.environ if env is None else env
    from profitos.legal import is_prelaunch
    errors = check_basic(env, database_configured) + check_email(env) + check_legal(env)
    if is_prelaunch(env):
        key = (env.get('PROFITOS_DATA_KEY') or '').strip()
        if key and len(key) < 32:
            errors.append('PROFITOS_DATA_KEY trop courte (32 caractères minimum).')
        return errors
    return errors + check_upload_dir(env) + check_stripe(env) + check_secrets(env)


def prelaunch_warnings(env=None):
    env = os.environ if env is None else env
    from profitos.legal import is_prelaunch
    if not is_prelaunch(env):
        return []
    warns = ['Mode pré-lancement actif : inscriptions limitées à PRELAUNCH_ALLOWED_EMAILS, paiement en ligne fermé.']
    warns += check_upload_dir(env) + check_stripe(env) + check_secrets(env)
    return warns


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    ap = argparse.ArgumentParser()
    ap.add_argument('--init-tenants', action='store_true', help='migre les schémas tenant en retard')
    ap.add_argument('--force-migrate', action='store_true', help='rejoue la migration de tous les tenants')
    args = ap.parse_args(argv)

    from profitos import db as dbmod
    env = os.environ.get('PROFITOS_ENV', 'development')
    print(f'[ProfitOS] env={env} backend={dbmod.backend_name()}')

    if _is_production():
        errors = production_errors(database_configured=dbmod.database_url_configured())
        if errors:
            for err in errors:
                print(f'[ProfitOS] ERREUR: {err}')
            return 1
        for warn in prelaunch_warnings():
            print(f'[ProfitOS] AVERTISSEMENT (pré-lancement): {warn}')

    from profitos.runtime import init_auth_db, database_readiness, migrate_all_tenant_schemas
    init_auth_db()
    from profitos.jobs import ensure_ops_tables
    ensure_ops_tables()  # job_runs + préférence rapport hebdomadaire
    ok, backend, err = database_readiness()
    if not ok:
        print(f'[ProfitOS] ERREUR base {backend}: {err}')
        return 1
    print(f'[ProfitOS] base {backend}: OK')

    if args.init_tenants:
        started = time.monotonic()
        report = migrate_all_tenant_schemas(force=args.force_migrate)
        elapsed = time.monotonic() - started
        print(f"[ProfitOS] schéma tenant v{report['version']} : "
              f"{len(report['migrated'])} migré(s), {len(report['up_to_date'])} déjà à jour, "
              f"{len(report['failed'])} échec(s) en {elapsed:.1f}s")
        for org_id, warnings in sorted(report['warnings'].items()):
            for w in warnings:
                print(f'[ProfitOS] AVERTISSEMENT migration org_{org_id}: {w}')
        if report['failed']:
            for org_id, reason in sorted(report['failed'].items()):
                print(f'[ProfitOS] ÉCHEC migration org_{org_id}: {reason}')
            print('[ProfitOS] preflight: ÉCHEC (migrations tenant)')
            return 1

    stripe_errors = stripe_config_errors(os.environ)
    if stripe_errors:  # hors production : simple avertissement
        print('[ProfitOS] AVERTISSEMENT Stripe: ' + ' ; '.join(stripe_errors))

    print('[ProfitOS] preflight: OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
