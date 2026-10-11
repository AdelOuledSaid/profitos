"""Conservation des données personnelles (RGPD) — fonctions à appeler par le
planificateur (tâche quotidienne). Aucune n'a besoin d'une requête HTTP ni
d'une session : elles peuvent être lancées depuis un script ou un cron.

Fonctions publiques (toutes idempotentes, renvoient des compteurs) :

- ``purge_deleted_organizations(now=None)`` : efface définitivement les
  organisations dont la suppression a été demandée il y a plus de 14 jours
  (``organizations.purge_after`` dépassé) — fichiers, schéma/base tenant,
  métadonnées AUTH, jetons tiers révoqués, comptes utilisateurs sans autre
  organisation.
- ``purge_expired_logs(months=None)`` : activity_log, security_events,
  api_audit_log (AUTH) et webhook_deliveries (chaque tenant) de plus de
  12 mois (``RETENTION_LOG_MONTHS``).
- ``purge_old_demo_requests(years=None)`` : demandes de démo de plus de 3 ans
  (``RETENTION_DEMO_YEARS``).
- ``anonymize_orphan_users(days=None)`` : comptes sans plus aucune
  organisation depuis 30 jours (``RETENTION_ORPHAN_USER_DAYS``) — anonymisés.
- ``purge_stale_security_rows(days=90)`` : sessions révoquées/expirées,
  invitations expirées, compteurs d'e-mails sortants.
- ``run_retention_tasks()`` : enchaîne toutes les tâches ci-dessus.
"""
import os
import secrets
import shutil
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

GRACE_PERIOD_DAYS = 14

# Tables AUTH/partagées rattachées à une organisation, nettoyées à la purge définitive.
ORG_AUTH_TABLES = (
    ('buyer_signals', 'organization_id'),
    ('sector_dso_signals', 'organization_id'),
    ('public_invoice_tokens', 'organization_id'),
    ('export_tokens', 'organization_id'),
    ('accounting_fec_tokens', 'organization_id'),
    ('supplier_inbox_tokens', 'organization_id'),
    ('supplier_inbox_entity_tokens', 'organization_id'),
    ('outgoing_invoice_tokens', 'organization_id'),
    ('outgoing_quote_tokens', 'organization_id'),
    ('api_audit_log', 'organization_id'),
    ('api_idempotency', 'organization_id'),
    ('api_keys', 'organization_id'),
    ('cabinet_time_entries', 'organization_id'),
    ('cabinet_documents', 'organization_id'),
    ('partner_directory', 'organization_id'),
    ('integration_interest', 'organization_id'),
    ('activity_log', 'organization_id'),
    ('security_events', 'organization_id'),
    ('plan_usage', 'organization_id'),
    ('team_invitations', 'organization_id'),
    ('outbound_email_log', 'organization_id'),
    ('swan_account_bindings', 'organization_id'),
    ('swan_onboarding_bindings', 'organization_id'),
    ('swan_webhook_inbox', 'organization_id'),
)


def _now():
    return datetime.now(timezone.utc)


def _log(msg, *args):
    try:
        from flask import current_app
        current_app.logger.info(msg, *args)
    except Exception:
        print('[ProfitOS] ' + (msg % args if args else msg))


def _table_exists(c, table):
    try:
        c.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchall()
        return True
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Révocation des accès tiers (best effort, jamais bloquante)
# ---------------------------------------------------------------------------

def revoke_third_party_tokens(org_id):
    """Révoque chez les fournisseurs les accès accordés par l'organisation :
    Dropbox / Google Drive (révocation OAuth), Powens (suppression de
    l'utilisateur Powens et de ses connexions bancaires), GoCardless
    (annulation des mandats). Chaque appel est indépendant et ne lève jamais.
    Renvoie un dict {fournisseur: nombre de révocations réussies}."""
    from profitos.runtime import tenant_cx_direct
    result = {'cloud_storage': 0, 'powens': 0, 'gocardless': 0}
    try:
        c = tenant_cx_direct(org_id)
    except Exception:
        return result
    try:
        if _table_exists(c, 'cloud_storage_connections'):
            from profitos.cloud_storage import revoke_tokens
            for row in c.execute('SELECT * FROM cloud_storage_connections').fetchall():
                try:
                    if revoke_tokens(row, org_id=org_id):
                        result['cloud_storage'] += 1
                except Exception:
                    pass
        if _table_exists(c, 'bank_connections'):
            rows = c.execute("SELECT provider_user_id FROM bank_connections WHERE provider='powens' "
                             "AND provider_user_id IS NOT NULL").fetchall()
            if rows:
                try:
                    import requests
                    from profitos.routes import bank_sync
                    if bank_sync._configured():
                        for row in rows:
                            try:
                                token = bank_sync._renew_token(row['provider_user_id'])
                                resp = requests.delete(bank_sync._api_url('/users/me'),
                                                       headers=bank_sync._headers(token), timeout=15)
                                if resp.status_code in (200, 204):
                                    result['powens'] += 1
                            except Exception:
                                pass
                except Exception:
                    pass
        if _table_exists(c, 'gocardless_mandates'):
            rows = c.execute("SELECT gc_mandate_id FROM gocardless_mandates WHERE gc_mandate_id IS NOT NULL "
                             "AND COALESCE(status,'') NOT IN ('cancelled','failed','expired')").fetchall()
            if rows:
                try:
                    from profitos import gocardless_baas
                    if gocardless_baas.is_configured():
                        for row in rows:
                            try:
                                gocardless_baas._request('POST', f"/mandates/{row['gc_mandate_id']}/actions/cancel")
                                result['gocardless'] += 1
                            except Exception:
                                pass
                except Exception:
                    pass
    finally:
        try:
            c.close()
        except Exception:
            pass
    return result


# ---------------------------------------------------------------------------
# Suppression d'organisation (délai de grâce de 14 jours)
# ---------------------------------------------------------------------------

def schedule_organization_deletion(org_id, user_id):
    """Marque l'organisation comme supprimée ; la purge définitive a lieu après
    GRACE_PERIOD_DAYS jours (purge_deleted_organizations). Pendant ce délai,
    l'organisation est inaccessible mais un propriétaire peut annuler."""
    from profitos.runtime import auth_cx
    now = _now()
    c = auth_cx()
    try:
        c.execute('UPDATE organizations SET deleted_at=?,purge_after=?,deletion_requested_by=?,updated_at=? WHERE id=?',
                  (now.isoformat(), (now + timedelta(days=GRACE_PERIOD_DAYS)).isoformat(), user_id,
                   now.isoformat(), org_id))
        # Les clés API et liens publics cessent immédiatement de fonctionner.
        c.execute('UPDATE api_keys SET revoked_at=? WHERE organization_id=? AND revoked_at IS NULL',
                  (now.isoformat(), org_id))
        c.commit()
    finally:
        c.close()
    return now + timedelta(days=GRACE_PERIOD_DAYS)


def cancel_organization_deletion(org_id):
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        c.execute('UPDATE organizations SET deleted_at=NULL,purge_after=NULL,deletion_requested_by=NULL,updated_at=? '
                  'WHERE id=?', (_now().isoformat(), org_id))
        c.commit()
    finally:
        c.close()


def hard_delete_organization(org_id):
    """Effacement définitif d'une organisation. Ordre : jetons tiers révoqués,
    fichiers, stockage tenant (schéma PostgreSQL / fichier SQLite), puis
    métadonnées AUTH dans une transaction unique. Lève une exception si une
    étape obligatoire échoue (l'organisation reste alors marquée pour une
    nouvelle tentative au prochain passage)."""
    from profitos import db as dbmod
    from profitos.runtime import UP, auth_cx, tenant_db
    oid = str(int(org_id))
    revoke_third_party_tokens(int(org_id))
    for upload_dir in (UP / f"org_{oid}", UP / 'purchase_documents' / oid,
                       UP / 'expense_receipts' / oid, UP / 'cabinet_documents' / oid):
        if upload_dir.exists():
            shutil.rmtree(upload_dir)
    dbmod.delete_tenant_storage(int(org_id), tenant_db(int(org_id)))
    ac = auth_cx()
    # Existence des tables vérifiée AVANT toute écriture : sous PostgreSQL, une requête
    # en échec annule la transaction en cours.
    present = [(t, col) for t, col in ORG_AUTH_TABLES if _table_exists(ac, t)]
    try:
        members = [r['user_id'] for r in ac.execute('SELECT user_id FROM memberships WHERE organization_id=?',
                                                     (int(org_id),)).fetchall()]
        for table, column in present:
            ac.execute(f'DELETE FROM {table} WHERE {column}=?', (int(org_id),))
        ac.execute('DELETE FROM referrals WHERE referrer_org_id=? OR referred_org_id=?', (int(org_id), int(org_id)))
        ac.execute('DELETE FROM memberships WHERE organization_id=?', (int(org_id),))
        ac.execute('DELETE FROM organizations WHERE id=?', (int(org_id),))
        # Comptes sans autre organisation : supprimés (la personne a demandé la suppression).
        for uid in members:
            if not ac.execute('SELECT 1 FROM memberships WHERE user_id=? LIMIT 1', (uid,)).fetchone():
                _delete_user_rows(ac, uid)
        ac.commit()
    except Exception:
        ac.rollback()
        raise
    finally:
        ac.close()


def _delete_user_rows(ac, uid):
    for table in ('push_device_tokens', 'user_sessions', 'user_devices'):
        ac.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))
    ac.execute('DELETE FROM users WHERE id=?', (uid,))


def purge_deleted_organizations(now=None):
    """À appeler quotidiennement : purge définitive des organisations dont le
    délai de grâce (14 jours) est écoulé. Renvoie le nombre d'organisations purgées."""
    from profitos.runtime import auth_cx
    now = now or _now()
    c = auth_cx()
    try:
        rows = c.execute('SELECT id FROM organizations WHERE deleted_at IS NOT NULL AND purge_after IS NOT NULL '
                         'AND purge_after<=?', (now.isoformat(),)).fetchall()
    finally:
        c.close()
    purged = 0
    for row in rows:
        try:
            hard_delete_organization(row['id'])
            purged += 1
            _log('GDPR organization purge completed org_id=%s', row['id'])
        except Exception as exc:  # nouvelle tentative au prochain passage
            _log('GDPR organization purge failed org_id=%s: %s', row['id'], type(exc).__name__)
    return purged


# ---------------------------------------------------------------------------
# Journaux, demandes de démo, comptes orphelins
# ---------------------------------------------------------------------------

def _months(value, env, default):
    if value is not None:
        return int(value)
    try:
        return int(os.environ.get(env, default))
    except ValueError:
        return int(default)


def purge_expired_logs(months=None, now=None):
    """Supprime les journaux plus anciens que ``months`` mois (12 par défaut,
    RETENTION_LOG_MONTHS) : activity_log, security_events, api_audit_log et,
    dans chaque organisation, webhook_deliveries."""
    from profitos.runtime import auth_cx, list_organization_ids, tenant_cx_direct
    months = _months(months, 'RETENTION_LOG_MONTHS', 12)
    cutoff = ((now or _now()) - timedelta(days=round(months * 30.44))).isoformat()
    counts = {}
    c = auth_cx()
    try:
        for table in ('activity_log', 'security_events', 'api_audit_log'):
            counts[table] = c.execute(f'DELETE FROM {table} WHERE created_at<?', (cutoff,)).rowcount or 0
        c.commit()
    finally:
        c.close()
    total = 0
    for org_id in list_organization_ids():
        try:
            tc = tenant_cx_direct(org_id)
            try:
                if _table_exists(tc, 'webhook_deliveries'):
                    total += tc.execute('DELETE FROM webhook_deliveries WHERE attempted_at<?',
                                        (cutoff[:19],)).rowcount or 0
                    tc.commit()
            finally:
                tc.close()
        except Exception:
            continue
    counts['webhook_deliveries'] = total
    return counts


def purge_old_demo_requests(years=None, now=None):
    from profitos.runtime import auth_cx
    years = _months(years, 'RETENTION_DEMO_YEARS', 3)
    cutoff = ((now or _now()) - timedelta(days=round(years * 365.25))).isoformat()
    c = auth_cx()
    try:
        n = c.execute('DELETE FROM demo_requests WHERE created_at<?', (cutoff,)).rowcount or 0
        c.commit()
        return n
    finally:
        c.close()


def anonymize_user(ac, uid):
    """Anonymise un compte (adresse, nom, mot de passe, secrets) sans casser les
    références historiques (journaux, auteurs d'écritures)."""
    ac.execute(
        "UPDATE users SET email=?,full_name=NULL,password_hash=?,is_active=0,email_verified=0,"
        "verification_token=NULL,reset_token=NULL,reset_token_expires=NULL,totp_secret=NULL,totp_enabled=0,"
        "totp_recovery_codes=NULL,pending_email=NULL,pending_email_token=NULL,anonymized_at=?,"
        "auth_version=COALESCE(auth_version,0)+1,updated_at=? WHERE id=?",
        (f'anonyme-{uid}-{secrets.token_hex(4)}@anonymise.invalid', generate_password_hash(secrets.token_hex(32)),
         _now().isoformat(), _now().isoformat(), uid))
    for table in ('push_device_tokens', 'user_sessions', 'user_devices'):
        ac.execute(f'DELETE FROM {table} WHERE user_id=?', (uid,))


def anonymize_orphan_users(days=None, now=None):
    """Anonymise les comptes qui n'appartiennent plus à aucune organisation
    depuis ``days`` jours (30 par défaut, RETENTION_ORPHAN_USER_DAYS) — par
    exemple un membre retiré d'une équipe qui n'a pas d'autre organisation."""
    from profitos.runtime import auth_cx
    days = _months(days, 'RETENTION_ORPHAN_USER_DAYS', 30)
    cutoff = ((now or _now()) - timedelta(days=days)).isoformat()
    c = auth_cx()
    try:
        rows = c.execute(
            'SELECT id FROM users WHERE anonymized_at IS NULL AND membership_lost_at IS NOT NULL '
            'AND membership_lost_at<? AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.user_id=users.id)',
            (cutoff,)).fetchall()
        for row in rows:
            anonymize_user(c, row['id'])
        c.commit()
        return len(rows)
    finally:
        c.close()


def purge_stale_security_rows(days=90, now=None):
    from profitos.runtime import auth_cx
    cutoff = ((now or _now()) - timedelta(days=days)).isoformat()
    c = auth_cx()
    try:
        out = {
            'user_sessions': c.execute('DELETE FROM user_sessions WHERE COALESCE(last_seen_at,created_at)<?',
                                       (cutoff,)).rowcount or 0,
            'team_invitations': c.execute('DELETE FROM team_invitations WHERE accepted_at IS NULL AND expires_at<?',
                                          (cutoff,)).rowcount or 0,
            'outbound_email_log': c.execute('DELETE FROM outbound_email_log WHERE created_at<?',
                                            (cutoff,)).rowcount or 0,
        }
        c.commit()
        return out
    finally:
        c.close()


def run_retention_tasks():
    """Point d'entrée unique pour le planificateur (quotidien)."""
    return {
        'organizations_purged': purge_deleted_organizations(),
        'logs': purge_expired_logs(),
        'demo_requests': purge_old_demo_requests(),
        'users_anonymized': anonymize_orphan_users(),
        'security_rows': purge_stale_security_rows(),
    }
