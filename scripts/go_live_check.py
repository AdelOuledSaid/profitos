"""Contrôle avant la mise en vente (v465 à v468), à lancer dans le shell du service web
Render :

    python scripts/go_live_check.py            # configuration seulement
    python scripts/go_live_check.py --network  # + connexions réelles (lecture seule)

Complète scripts/cloud_preflight.py (qui bloque le déploiement) : ici, on vérifie que
chaque service tiers est en mode PRODUCTION et répond, sans rien créer ni modifier :
WeInvoice (facture électronique), Swan / Powens (banque), Stripe (abonnements), Resend
(e-mails), sauvegardes (phrase de passe, S3, dernière sauvegarde réussie), mentions
légales, ouverture du module fiscal. Aucun secret n'est affiché.

Code de sortie : 0 si aucun point bloquant, 1 sinon.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, FAIL, INFO = 'ok', 'warn', 'fail', 'info'
SYMBOL = {OK: '✓', WARN: '⚠', FAIL: '✗', INFO: '•'}


def _v(env, name):
    return (env.get(name) or '').strip()


def _item(section, level, label, detail=''):
    return {'section': section, 'level': level, 'label': label, 'detail': detail}


# ------------------------------------------------------------------ configuration
def check_weinvoice(env):
    s = 'Facture électronique (WeInvoice)'
    out = []
    if not (_v(env, 'WEINVOICE_CLIENT_ID') and _v(env, 'WEINVOICE_CLIENT_SECRET')):
        return [_item(s, WARN, 'Non configurée', "WEINVOICE_CLIENT_ID / WEINVOICE_CLIENT_SECRET absents : l'envoi par plateforme agréée est indisponible.")]
    out.append(_item(s, OK if _v(env, 'WEINVOICE_ENV') == 'production' else FAIL,
                     'Environnement production' if _v(env, 'WEINVOICE_ENV') == 'production' else 'Environnement sandbox',
                     '' if _v(env, 'WEINVOICE_ENV') == 'production' else 'WEINVOICE_ENV=production avec les identifiants de production.'))
    out.append(_item(s, OK if _v(env, 'WEINVOICE_INVOICE_CLIENT_ID') and _v(env, 'WEINVOICE_INVOICE_CLIENT_SECRET') else FAIL,
                     "Clé d'émission des factures", 'WEINVOICE_INVOICE_CLIENT_ID / _SECRET (seconde paire de clés).'))
    for name, label in (('WEINVOICE_WEBHOOK_SECRET', 'Signature des notifications (portefeuille)'),
                        ('WEINVOICE_INVOICE_WEBHOOK_SECRET', 'Signature des notifications (factures)')):
        out.append(_item(s, OK if _v(env, name) else FAIL, label, name))
    return out


def check_banking(env):
    s = 'Banque (Swan / Powens)'
    out = []
    swan = bool(_v(env, 'SWAN_CLIENT_ID') and _v(env, 'SWAN_CLIENT_SECRET'))
    powens = bool(_v(env, 'POWENS_CLIENT_ID') and _v(env, 'POWENS_CLIENT_SECRET') and _v(env, 'POWENS_DOMAIN'))
    if not swan and not powens:
        return [_item(s, WARN, 'Aucune connexion bancaire configurée', "Les clients importent leurs relevés (CSV, OFX) : fonctionnel, mais sans synchronisation.")]
    if swan:
        live = (_v(env, 'SWAN_ENVIRONMENT') or _v(env, 'SWAN_ENV') or 'sandbox').lower() == 'live'
        out.append(_item(s, OK if live else FAIL, 'Swan : environnement live' if live else 'Swan : environnement sandbox',
                         '' if live else 'SWAN_ENVIRONMENT=live avec les identifiants live.'))
        out.append(_item(s, OK if _v(env, 'SWAN_WEBHOOK_SECRET') else FAIL, 'Swan : signature des notifications', 'SWAN_WEBHOOK_SECRET'))
        redirect, base = _v(env, 'SWAN_OAUTH_REDIRECT_URI'), _v(env, 'APP_BASE_URL').rstrip('/')
        good = bool(redirect and base and redirect.startswith(base + '/'))
        out.append(_item(s, OK if good else FAIL, 'Swan : adresse de retour OAuth',
                         '' if good else "SWAN_OAUTH_REDIRECT_URI doit commencer par APP_BASE_URL (et être déclarée chez Swan)."))
        out.append(_item(s, INFO, 'Swan : ouverture de comptes et cartes',
                         'activée' if _v(env, 'SWAN_ENABLE_ACCOUNT_CARD_REQUESTS').lower() in ('1', 'true', 'yes', 'on')
                         else 'désactivée (lecture seule) tant que SWAN_ENABLE_ACCOUNT_CARD_REQUESTS n\'est pas à 1'))
    if powens:
        sandbox = 'sandbox' in _v(env, 'POWENS_DOMAIN').lower() or _v(env, 'POWENS_DOMAIN').lower().startswith('demo')
        out.append(_item(s, FAIL if sandbox else OK, 'Powens : domaine de production' if not sandbox else 'Powens : domaine de test',
                         _v(env, 'POWENS_DOMAIN') if sandbox else ''))
    return out


def check_stripe(env):
    s = 'Abonnements (Stripe)'
    key = _v(env, 'STRIPE_SECRET_KEY')
    if not key:
        return [_item(s, FAIL, 'Non configuré', 'STRIPE_SECRET_KEY absente : aucun client ne peut souscrire.')]
    out = [_item(s, OK if key.startswith(('sk_live_', 'rk_live_')) else FAIL,
                 'Clé live' if key.startswith(('sk_live_', 'rk_live_')) else 'Clé de test',
                 '' if key.startswith(('sk_live_', 'rk_live_')) else 'Remplacez par la clé secrète live (sk_live_…).')]
    out.append(_item(s, OK if _v(env, 'STRIPE_WEBHOOK_SECRET').startswith('whsec_') else FAIL, 'Secret du webhook',
                     "STRIPE_WEBHOOK_SECRET du point de terminaison live (/webhooks/stripe)."))
    prices = [n for n in ('STRIPE_PRICE_STARTER_ID', 'STRIPE_PRICE_PRO_ID', 'STRIPE_PRICE_BUSINESS_ID', 'STRIPE_PRICE_MULTI_ID') if _v(env, n)]
    out.append(_item(s, OK if len(prices) == 4 else (WARN if prices else FAIL), f'Tarifs configurés : {len(prices)} sur 4',
                     '' if len(prices) == 4 else 'Les tarifs live ont des identifiants différents des tarifs de test.'))
    return out


def check_email(env):
    s = 'E-mails (Resend)'
    if not (_v(env, 'RESEND_API_KEY') or _v(env, 'SMTP_HOST')):
        return [_item(s, FAIL, 'Aucun fournisseur', 'RESEND_API_KEY (ou SMTP_HOST) : sans lui, ni vérification de compte ni relances.')]
    sender = _v(env, 'RESEND_FROM_EMAIL') or 'ProfitOS <noreply@profitos.fr>'
    domain = sender.rsplit('@', 1)[-1].strip('> ').lower()
    bad = domain in ('example.com', 'resend.dev', 'localhost') or not domain
    return [_item(s, OK, 'Fournisseur configuré'),
            _item(s, FAIL if bad else OK, f'Expéditeur : domaine {domain}',
                  'Domaine de test : vérifiez votre domaine dans Resend (SPF, DKIM).' if bad else 'Domaine à vérifier dans Resend (SPF, DKIM).')]


def check_backups(env):
    s = 'Sauvegardes'
    out = [_item(s, OK if len(_v(env, 'BACKUP_PASSPHRASE')) >= 16 else FAIL, 'Phrase de passe (16 caractères minimum)',
                 'Conservez-la hors de Render : sans elle, aucune restauration possible.')]
    import importlib.util
    spec = importlib.util.spec_from_file_location('profitos_backup_script', ROOT / 'scripts' / 'backup.py')
    bk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bk)
    try:
        cfg = bk.s3_config(env)
    except Exception as exc:          # configuration S3 partielle
        cfg, err = None, str(exc)
    else:
        err = ''
    out.append(_item(s, OK if cfg else WARN, 'Copie hors site (S3)' if cfg else 'Pas de copie hors site',
                     err or ('' if cfg else 'BACKUP_S3_* : sans copie externe, une perte du disque Render emporte aussi les sauvegardes.')))
    return out


def last_backup(auth_conn, now=None):
    """Dernière exécution réussie de la tâche de sauvegarde (table job_runs)."""
    now = now or datetime.now(timezone.utc)
    try:
        row = auth_conn.execute("SELECT finished_at FROM job_runs WHERE job_name='backup' AND status='success' "
                                "ORDER BY finished_at DESC LIMIT 1").fetchone()
    except Exception:
        return _item('Sauvegardes', WARN, 'Historique des sauvegardes illisible')
    if not row or not row['finished_at']:
        return _item('Sauvegardes', FAIL, 'Aucune sauvegarde réussie enregistrée', 'Lancez : python scripts/run_job.py backup --force')
    when = datetime.fromisoformat(str(row['finished_at']).replace('Z', '+00:00'))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = now - when
    level = OK if age <= timedelta(hours=26) else FAIL
    return _item('Sauvegardes', level, f"Dernière sauvegarde réussie : il y a {int(age.total_seconds() // 3600)} h",
                 '' if level == OK else 'La sauvegarde quotidienne ne tourne plus : vérifiez le cron et les journaux.')


def check_legal_and_launch(env):
    s = 'Mise en vente'
    out = []
    try:
        from profitos.legal import legal_identity_errors, missing_legal_fields, is_prelaunch
        missing = missing_legal_fields(env)
        errors = legal_identity_errors(env)
        out.append(_item(s, FAIL if (missing or errors) else OK, 'Mentions légales de l\'éditeur',
                         ', '.join(missing + errors) if (missing or errors) else ''))
        out.append(_item(s, WARN if is_prelaunch(env) else OK,
                         'Mode pré-lancement actif' if is_prelaunch(env) else 'Inscriptions et paiement ouverts',
                         'PROFITOS_PRELAUNCH=1 : inscriptions limitées, paiement fermé.' if is_prelaunch(env) else ''))
    except Exception as exc:
        out.append(_item(s, WARN, 'Mentions légales non vérifiées', type(exc).__name__))
    if _v(env, 'FISCAL_DECLARATIONS_ENABLED') == '1':
        out.append(_item(s, WARN, 'Déclarations fiscales ouvertes à tous',
                         'Seulement après signature de la grille de validation fiscale par un expert-comptable.'))
    elif _v(env, 'FISCAL_DECLARATIONS_PILOT_ORGS'):
        out.append(_item(s, INFO, 'Déclarations fiscales ouvertes aux pilotes', _v(env, 'FISCAL_DECLARATIONS_PILOT_ORGS')))
    else:
        out.append(_item(s, INFO, 'Déclarations fiscales fermées', 'Montants consultables à titre indicatif.'))
    if _v(env, 'EDI_SIMULATION_ENABLED') == '1':
        out.append(_item(s, FAIL, 'Simulation EDI active en production', 'Retirez EDI_SIMULATION_ENABLED.'))
    return out


def configuration_checks(env):
    return (check_weinvoice(env) + check_banking(env) + check_stripe(env) + check_email(env) + check_backups(env)
            + check_legal_and_launch(env))


# ---------------------------------------------------------------- connexions réelles
def network_checks(env):
    """Appels en lecture seule. Chaque service non configuré est ignoré."""
    out = []
    if _v(env, 'WEINVOICE_CLIENT_ID'):
        from profitos import weinvoice
        for cs, label in (('management', 'portefeuille'), ('invoicing', 'émission')):
            if not weinvoice.is_configured(cs):
                continue
            try:
                weinvoice.fetch_access_token(credential_set=cs)
                out.append(_item('Facture électronique (WeInvoice)', OK, f'Connexion ({label}) : jeton obtenu'))
            except Exception as exc:
                out.append(_item('Facture électronique (WeInvoice)', FAIL, f'Connexion ({label}) refusée', str(exc)[:200]))
    if _v(env, 'SWAN_CLIENT_ID'):
        from profitos import swan_baas
        try:
            swan_baas.get_server_token()
            out.append(_item('Banque (Swan / Powens)', OK, f'Swan ({swan_baas.current_environment()}) : jeton obtenu'))
        except Exception as exc:
            out.append(_item('Banque (Swan / Powens)', FAIL, 'Swan : authentification refusée', str(exc)[:200]))
    if _v(env, 'STRIPE_SECRET_KEY'):
        try:
            import stripe
            stripe.api_key = _v(env, 'STRIPE_SECRET_KEY')
            acct = stripe.Account.retrieve()
            charges = bool(acct.get('charges_enabled'))
            out.append(_item('Abonnements (Stripe)', OK if charges else FAIL,
                             'Compte prêt à encaisser' if charges else 'Encaissements désactivés sur le compte Stripe',
                             '' if charges else 'Terminez l\'activation du compte dans le tableau de bord Stripe.'))
            for name in ('STRIPE_PRICE_STARTER_ID', 'STRIPE_PRICE_PRO_ID', 'STRIPE_PRICE_BUSINESS_ID', 'STRIPE_PRICE_MULTI_ID'):
                if _v(env, name):
                    price = stripe.Price.retrieve(_v(env, name))
                    good = bool(price.get('active')) and bool(price.get('livemode')) == _v(env, 'STRIPE_SECRET_KEY').startswith(('sk_live_', 'rk_live_'))
                    out.append(_item('Abonnements (Stripe)', OK if good else FAIL, f'{name} : '
                                     + ('actif' if good else 'inactif ou d\'un autre mode (test / live)')))
        except Exception as exc:
            out.append(_item('Abonnements (Stripe)', FAIL, 'Connexion Stripe impossible', str(exc)[:200]))
    try:
        from profitos.runtime import auth_cx
        conn = auth_cx()
        try:
            out.append(last_backup(conn))
        finally:
            conn.close()
    except Exception as exc:
        out.append(_item('Sauvegardes', WARN, 'Base d\'authentification inaccessible', type(exc).__name__))
    return out


def report(items):
    lines, section = [], None
    for it in items:
        if it['section'] != section:
            section = it['section']
            lines.append(f'\n{section}')
        lines.append(f"  {SYMBOL[it['level']]} {it['label']}" + (f" — {it['detail']}" if it['detail'] else ''))
    fails = sum(1 for it in items if it['level'] == FAIL)
    warns = sum(1 for it in items if it['level'] == WARN)
    lines.append(f'\n{fails} point(s) bloquant(s), {warns} avertissement(s).')
    return '\n'.join(lines)


def main(argv=None, env=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--network', action='store_true', help='tester aussi les connexions réelles (lecture seule)')
    args = ap.parse_args(argv)
    env = os.environ if env is None else env
    items = configuration_checks(env)
    if args.network:
        items += network_checks(env)
    items.sort(key=lambda x: x['section'])  # tri stable : regroupe par service
    print(report(items))
    return 1 if any(it['level'] == FAIL for it in items) else 0


if __name__ == '__main__':
    sys.exit(main())
