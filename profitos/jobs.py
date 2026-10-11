"""Tâches planifiées ProfitOS (cron Render).

Un seul cron Render horaire exécute ``python scripts/run_job.py --due`` : le
registre ci-dessous décide, à l'heure de Paris (changement d'heure compris),
quelles tâches sont dues. Chaque tâche peut aussi être lancée seule :
``python scripts/run_job.py weekly_reports [--force] [--org 12]``.

Garanties :
  - isolation par organisation : une organisation en échec n'arrête pas les
    autres (exception journalisée, événement OPS JOB_ORG_FAILED) ;
  - idempotence : table ``job_runs`` (base AUTH) par (tâche, organisation,
    période) — une période déjà réussie n'est jamais rejouée, une période en
    échec est retentée au plus JOB_MAX_ATTEMPTS fois ;
  - les tâches qui écrivent des documents (``needs_disk``) doivent tourner
    dans le service web, seul à monter le disque persistant : le cron les
    délègue via ``POST /api/v1/internal/jobs/<nom>`` (jeton
    JOBS_TRIGGER_TOKEN), le web les exécute dans un thread d'arrière-plan.
"""
import hmac
import importlib
import importlib.util
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PARIS = ZoneInfo('Europe/Paris')
JOB_MAX_ATTEMPTS = 3
RUNNING_STALE_AFTER = timedelta(hours=2)
log = logging.getLogger('profitos.jobs')

JOBS = {}


@dataclass
class Job:
    name: str
    func: object
    cadence: str              # 'hourly' | 'daily' | 'weekly'
    hour: int = 0             # heure de Paris à partir de laquelle la tâche est due
    weekday: int = None       # 0 = lundi (tâches hebdomadaires)
    scope: str = 'org'        # 'org' (une exécution par organisation) | 'global'
    needs_disk: bool = False  # écrit sur le disque persistant -> exécutée dans le web
    description: str = ''
    meta: dict = field(default_factory=dict)


def job(name, cadence, hour=0, weekday=None, scope='org', needs_disk=False, description=''):
    def deco(fn):
        JOBS[name] = Job(name, fn, cadence, hour, weekday, scope, needs_disk, description)
        return fn
    return deco


# ---------------------------------------------------------------------------
# Planification
# ---------------------------------------------------------------------------

def paris_now(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(PARIS)


def period_key(j, now=None):
    p = paris_now(now)
    if j.cadence == 'hourly':
        return p.strftime('%Y-%m-%dT%H')
    if j.cadence == 'weekly':
        y, w, _ = p.isocalendar()
        return f'{y}-W{w:02d}'
    return p.date().isoformat()


def is_due(j, now=None):
    p = paris_now(now)
    if j.cadence == 'hourly':
        return True
    if j.cadence == 'weekly':
        wd = j.weekday or 0
        return p.weekday() > wd or (p.weekday() == wd and p.hour >= j.hour)
    return p.hour >= j.hour


# ---------------------------------------------------------------------------
# Tables d'exploitation (base AUTH)
# ---------------------------------------------------------------------------

_ops_tables_ready = False


def ensure_ops_tables():
    """Crée job_runs et users.weekly_report_enabled (idempotent)."""
    global _ops_tables_ready
    from profitos.runtime import auth_cx
    from profitos.db import USE_POSTGRES
    c = auth_cx()
    try:
        id_column = 'BIGSERIAL PRIMARY KEY' if USE_POSTGRES else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        c.execute(f'''CREATE TABLE IF NOT EXISTS job_runs(
            id {id_column},
            job_name TEXT NOT NULL,
            organization_id INTEGER NOT NULL DEFAULT 0,
            period_key TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            started_at TEXT,
            finished_at TEXT,
            detail TEXT,
            UNIQUE(job_name, organization_id, period_key))''')
        if USE_POSTGRES:
            cols = [r['column_name'] for r in c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users'"
            ).fetchall()]
        else:
            cols = [r['name'] for r in c.execute('PRAGMA table_info(users)').fetchall()]
        if cols and 'weekly_report_enabled' not in cols:
            c.execute('ALTER TABLE users ADD COLUMN weekly_report_enabled INTEGER DEFAULT 1')
        c.commit()
        _ops_tables_ready = True
    finally:
        c.close()


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat()


def claim(job_name, org_id, period, force=False):
    """Réserve (tâche, organisation, période). False si déjà faite / en cours."""
    from profitos.runtime import auth_cx, parse_dt
    c = auth_cx()
    try:
        row = c.execute('SELECT id,status,attempts,started_at FROM job_runs WHERE job_name=? AND organization_id=? AND period_key=?',
                        (job_name, int(org_id), period)).fetchone()
        if row:
            if not force:
                if row['status'] in ('success', 'skipped'):
                    return False
                started = parse_dt(row['started_at'])
                if row['status'] == 'running' and started and datetime.now(timezone.utc) - started < RUNNING_STALE_AFTER:
                    return False
                if row['status'] == 'error' and int(row['attempts'] or 0) >= JOB_MAX_ATTEMPTS:
                    return False
            c.execute("UPDATE job_runs SET status='running',attempts=COALESCE(attempts,0)+1,started_at=?,finished_at=NULL,detail=NULL WHERE id=?",
                      (_iso(), row['id']))
            c.commit()
            return True
        cur = c.execute("INSERT OR IGNORE INTO job_runs(job_name,organization_id,period_key,status,attempts,started_at) VALUES(?,?,?,'running',1,?)",
                        (job_name, int(org_id), period, _iso()))
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def finish(job_name, org_id, period, status, detail=None):
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        c.execute('UPDATE job_runs SET status=?,finished_at=?,detail=? WHERE job_name=? AND organization_id=? AND period_key=?',
                  (status, _iso(), (str(detail)[:500] if detail is not None else None), job_name, int(org_id), period))
        c.commit()
    finally:
        c.close()


def last_success(job_name, org_id=0):
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        row = c.execute("SELECT MAX(finished_at) AS t FROM job_runs WHERE job_name=? AND organization_id=? AND status='success'",
                        (job_name, int(org_id))).fetchone()
        return row['t'] if row else None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------

class Skip(Exception):
    """Organisation non concernée (pas d'abonnement, rien à faire)."""


def _organizations(org_ids=None):
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        rows = c.execute('SELECT * FROM organizations ORDER BY id').fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        if org_ids and int(r['id']) not in org_ids:
            continue
        if str(r['status'] or '').upper().startswith('DELET'):
            continue
        out.append(r)
    return out


def _ops(event, outcome, detail):
    from profitos.runtime import log_ops_event
    log_ops_event(event, outcome, detail=detail)
    if outcome in ('ERROR', 'WARNING'):
        log.warning('OPS %s %s %s', event, outcome, detail)


def run_job(app, name, force=False, org_ids=None, now=None):
    """Exécute une tâche. Retourne un rapport {ok, failed, skipped, already}."""
    from flask import g, session
    j = JOBS[name]
    ensure_ops_tables()
    period = period_key(j, now)
    report = {'job': name, 'period': period, 'ok': [], 'failed': {}, 'skipped': [], 'already': []}
    ctx = {'now': now, 'period': period, 'cache': {}}
    started = time.monotonic()
    if j.scope == 'global':
        with app.test_request_context(f'/_jobs/{name}'):
            g.request_id = f'job-{name}'
            if not claim(name, 0, period, force):
                report['already'].append(0)
                return report
            try:
                detail = j.func(None, ctx)
                finish(name, 0, period, 'success', detail)
                report['ok'].append(0)
            except Skip as exc:
                finish(name, 0, period, 'skipped', str(exc))
                report['skipped'].append(0)
            except Exception as exc:
                log.exception('Tâche %s en échec', name)
                finish(name, 0, period, 'error', f'{type(exc).__name__}: {exc}')
                report['failed'][0] = type(exc).__name__
                _ops('JOB_FAILED', 'ERROR', f'job={name} error={type(exc).__name__}')
        return report

    for org in _organizations(org_ids):
        org_id = int(org['id'])
        with app.test_request_context(f'/_jobs/{name}'):
            g.request_id = f'job-{name}-{org_id}'
            session['org_id'] = org_id
            if not claim(name, org_id, period, force):
                report['already'].append(org_id)
                continue
            try:
                from profitos.runtime import migrate_tenant
                migrate_tenant(org_id)  # vérification bon marché de la version du schéma
                detail = j.func(org, ctx)
                finish(name, org_id, period, 'success', detail)
                report['ok'].append(org_id)
            except Skip as exc:
                finish(name, org_id, period, 'skipped', str(exc))
                report['skipped'].append(org_id)
            except Exception as exc:
                log.exception('Tâche %s en échec pour org_%s', name, org_id)
                finish(name, org_id, period, 'error', f'{type(exc).__name__}: {str(exc)[:300]}')
                report['failed'][org_id] = type(exc).__name__
                _ops('JOB_ORG_FAILED', 'ERROR', f'job={name} org={org_id} error={type(exc).__name__}')
    with app.test_request_context(f'/_jobs/{name}'):
        _ops('JOB_DONE', 'WARNING' if report['failed'] else 'INFO',
             f"job={name} period={period} ok={len(report['ok'])} failed={len(report['failed'])} "
             f"skipped={len(report['skipped'])} already={len(report['already'])} {time.monotonic()-started:.1f}s")
    return report


def due_jobs(now=None):
    """Tâches dues maintenant dont la période n'est pas encore terminée."""
    ensure_ops_tables()
    from profitos.runtime import auth_cx
    out = []
    c = auth_cx()
    try:
        for j in JOBS.values():
            if not is_due(j, now):
                continue
            row = c.execute("SELECT status FROM job_runs WHERE job_name=? AND organization_id=0 AND period_key=?",
                            (f'{j.name}:period', period_key(j, now))).fetchone()
            if row and row['status'] == 'success':
                continue
            out.append(j)
    finally:
        c.close()
    return out


def mark_period_done(name, now=None):
    """Marqueur global : la période d'une tâche est terminée (toutes les
    organisations réussies, ignorées ou à court de tentatives)."""
    j = JOBS[name]
    period = period_key(j, now)
    key = f'{name}:period'
    claim(key, 0, period, force=True)
    finish(key, 0, period, 'success')


def period_complete(report):
    """Vrai si aucune organisation ne doit être retentée sur cette période."""
    if not report['failed']:
        return True
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        for org_id in report['failed']:
            row = c.execute('SELECT attempts FROM job_runs WHERE job_name=? AND organization_id=? AND period_key=?',
                            (report['job'], int(org_id), report['period'])).fetchone()
            if row and int(row['attempts'] or 0) < JOB_MAX_ATTEMPTS:
                return False
        return True
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Helpers métier
# ---------------------------------------------------------------------------

def _is_paid(org):
    """Abonnement donnant accès aux fonctions de la formule (payé, impayé encore
    en délai de grâce, ou essai en cours = formule Pro), comme côté web."""
    return has_access(org)


def has_access(org):
    """Même règle que runtime.org_has_access (organisation payeuse Multi-sociétés,
    délai de grâce PAST_DUE, essai en cours), sans dépendre des clés Stripe : le
    cron n'en a pas besoin."""
    from profitos.runtime import subscription_state
    return subscription_state(org) in ('ACTIVE', 'TRIAL', 'PAST_DUE_GRACE')


def _require_access(org):
    if not has_access(org):
        raise Skip('organisation sans accès actif')


def _require_paid(org, feature=None):
    if not _is_paid(org):
        raise Skip('abonnement payant requis')
    if feature:
        from profitos.plan_limits import feature_enabled
        from profitos.runtime import effective_plan
        if not feature_enabled(effective_plan(org), feature):
            raise Skip(f'fonctionnalité {feature} non incluse')


def _owner_email(org_id):
    from profitos.runtime import auth_cx
    c = auth_cx()
    try:
        row = c.execute("SELECT users.email FROM memberships JOIN users ON users.id=memberships.user_id "
                        "WHERE memberships.organization_id=? AND memberships.role='OWNER' ORDER BY memberships.id LIMIT 1",
                        (int(org_id),)).fetchone()
        return row['email'] if row else None
    finally:
        c.close()


def _app_url(path=''):
    return os.environ.get('APP_BASE_URL', 'http://127.0.0.1:5050').rstrip('/') + path


def _notify_owner(org, subject, title, intro, cta_label, cta_path, footer=''):
    from flask import render_template
    from profitos.runtime import send_email
    to = _owner_email(org['id'])
    if not to:
        return {'sent': False, 'reason': 'no_owner_email'}
    html = render_template('email_transactional.html', title=title, intro=intro,
                           cta_label=cta_label, cta_url=_app_url(cta_path), footer=footer)
    return send_email(to, subject, html)


def _entity_ids(c):
    ids = [None]
    try:
        ids += [r['id'] for r in c.execute('SELECT id FROM entities ORDER BY id').fetchall()]
    except Exception:
        pass
    return ids


# ---------------------------------------------------------------------------
# Tâches
# ---------------------------------------------------------------------------

@job('weekly_reports', 'weekly', hour=7, weekday=0,
     description='Rapport hebdomadaire RECOVER/SAVE/GROW aux propriétaires abonnés (lundi 7 h).')
def weekly_reports(org, ctx):
    _require_access(org)
    from profitos.runtime import compute_weekly_digest, send_weekly_email
    result = send_weekly_email(org, compute_weekly_digest(org['id']))
    if result.get('reason') in ('no_owner_email', 'unsubscribed'):
        raise Skip(result['reason'])
    if not result.get('sent') and not result.get('dry_run'):
        raise RuntimeError("échec d'envoi du rapport hebdomadaire")
    return 'simulé' if result.get('dry_run') else 'envoyé'


@job('recurring_invoices', 'daily', hour=6,
     description='Génère les factures récurrentes échues en BROUILLON et prévient le propriétaire (jamais émises).')
def recurring_invoices(org, ctx):
    _require_paid(org)
    from profitos.runtime import tenant_cx_direct, auth_cx, now
    from profitos.routes.invoicing import _generate_due_recurring_invoices
    c = tenant_cx_direct(org['id'])
    created = []
    try:
        for eid in _entity_ids(c):
            try:
                rows = _generate_due_recurring_invoices(c, eid)
                c.commit()
            except Exception:
                c.rollback()
                raise
            created.extend(rows)
    finally:
        c.close()
    if not created:
        return '0 brouillon'
    ac = auth_cx()
    try:
        for invoice_id, number, token in created:
            ac.execute('INSERT OR IGNORE INTO outgoing_invoice_tokens(token,organization_id,invoice_local_id,created_at) VALUES(?,?,?,?)',
                       (token, org['id'], invoice_id, now()))
        ac.commit()
    finally:
        ac.close()
    n = len(created)
    numbers = ', '.join(x[1] for x in created[:10])
    _notify_owner(
        org, f"{n} facture(s) récurrente(s) prête(s) à vérifier — {org['name']}",
        'Factures récurrentes générées en brouillon',
        f"ProfitOS a généré {n} facture(s) récurrente(s) en brouillon pour {org['name']} ({numbers}). "
        "Elles n'ont pas été envoyées à vos clients : vérifiez-les puis émettez-les depuis ProfitOS.",
        'Vérifier les brouillons', '/facturation/recurrentes',
        "Vous recevez cet email car des modèles de facturation récurrente sont actifs sur votre compte.")
    return f'{n} brouillon(s)'


@job('einvoice_inbound_sync', 'hourly', needs_disk=True,
     description="Réception des factures électroniques fournisseurs WeInvoice (toutes les heures).")
def einvoice_inbound_sync(org, ctx):
    _require_paid(org)
    from profitos.runtime import tenant_cx_direct
    from profitos.routes.invoicing import sync_weinvoice_inbound, WeInvoiceInboundNotConfigured
    c = tenant_cx_direct(org['id'])
    try:
        try:
            rows = c.execute("SELECT entity_key FROM weinvoice_entity_settings WHERE COALESCE(weinvoice_company_id,'')!=''").fetchall()
        except Exception:
            rows = []
        if not rows:
            raise Skip('WeInvoice non configuré')
        totals = {'created': 0, 'failed': 0}
        errors = []
        for r in rows:
            eid = int(r['entity_key'] or 0) or None
            try:
                res = sync_weinvoice_inbound(c, eid, org['id'])
                totals['created'] += res['created']; totals['failed'] += len(res['failed'])
            except WeInvoiceInboundNotConfigured:
                continue
            except Exception as exc:
                errors.append(f'entité {eid or 0}: {type(exc).__name__}')
        if errors:
            raise RuntimeError('; '.join(errors))
        return f"{totals['created']} importée(s), {totals['failed']} échec(s)"
    finally:
        c.close()


@job('bank_sync', 'daily', hour=5,
     description='Synchronisation quotidienne des comptes bancaires Powens.')
def bank_sync(org, ctx):
    _require_paid(org, 'banking')
    from profitos.runtime import tenant_cx_direct
    from profitos.routes.bank_sync import _sync_powens, _mark_powens_sync_error, _configured
    if not _configured():
        raise Skip('Powens non configuré')
    c = tenant_cx_direct(org['id'])
    try:
        rows = c.execute("SELECT * FROM bank_connections WHERE provider='powens' AND COALESCE(provider_user_id,'')!='' ORDER BY id").fetchall()
        if not rows:
            raise Skip('aucune banque connectée')
        done, errors = 0, []
        for row in rows:
            try:
                _sync_powens(c, row)
                done += 1
            except Exception as exc:
                try:
                    _mark_powens_sync_error(c, row)
                except Exception:
                    c.rollback()
                errors.append(type(exc).__name__)
        if errors:
            raise RuntimeError(f'{len(errors)} connexion(s) en échec : {", ".join(errors)}')
        return f'{done} connexion(s)'
    finally:
        c.close()


@job('boamp_refresh', 'daily', hour=6,
     description='Actualisation quotidienne des appels d\'offres BOAMP (GROW).')
def boamp_refresh(org, ctx):
    _require_paid(org, 'advanced_features')
    from profitos.runtime import profile, sync_grow, fetch_boamp_records
    if not profile():
        raise Skip('profil entreprise absent')
    if 'boamp' not in ctx['cache']:
        ctx['cache']['boamp'] = fetch_boamp_records()  # une seule requête BOAMP par exécution
    return f"{sync_grow(rows=ctx['cache']['boamp'])} opportunité(s)"


def refresh_dso_snapshot(c, entity_id, today=None):
    """Recalcule l'ancienneté des impayés et enregistre l'instantané DSO du jour."""
    from profitos.runtime import invoice_score, parse_date, now
    today = today or date.today()
    rows = c.execute("SELECT id,amount,paid_amount,due_date,retention_release_date,kind,status FROM invoices "
                     "WHERE LOWER(COALESCE(status,''))!='paid' AND entity_id IS ?", (entity_id,)).fetchall()
    overdue = []
    for r in rows:
        ref = parse_date(r['retention_release_date']) if r['kind'] == 'RETENTION' and r['retention_release_date'] else parse_date(r['due_date'])
        days = max(0, (today - ref).days) if ref and ref <= today else 0
        score = invoice_score(r['amount'] or 0, days, r['paid_amount'] or 0) if days > 0 else 0
        c.execute('UPDATE invoices SET days_overdue=?,score=? WHERE id=?', (days, score, r['id']))
        if days > 0:
            overdue.append((days, max((r['amount'] or 0) - (r['paid_amount'] or 0), 0)))
    avg = (sum(d for d, _ in overdue) / len(overdue)) if overdue else 0
    total = sum(a for _, a in overdue)
    entity_key = str(entity_id) if entity_id else '__ROOT__'
    today_iso = today.isoformat()
    c.execute('''INSERT INTO dso_entity_snapshots(snapshot_date,entity_key,entity_id,avg_days_overdue,total_outstanding,invoice_count,created_at)
                 VALUES(?,?,?,?,?,?,?)
                 ON CONFLICT(snapshot_date,entity_key) DO UPDATE SET avg_days_overdue=excluded.avg_days_overdue,
                   total_outstanding=excluded.total_outstanding,invoice_count=excluded.invoice_count,created_at=excluded.created_at''',
              (today_iso, entity_key, entity_id, avg, total, len(overdue), now()))
    if not entity_id:
        c.execute('''INSERT INTO dso_snapshots(snapshot_date,avg_days_overdue,total_outstanding,invoice_count,created_at)
                     VALUES(?,?,?,?,?)
                     ON CONFLICT(snapshot_date) DO UPDATE SET avg_days_overdue=excluded.avg_days_overdue,
                       total_outstanding=excluded.total_outstanding,invoice_count=excluded.invoice_count,created_at=excluded.created_at''',
                  (today_iso, avg, total, len(overdue), now()))
    return avg, total, len(overdue)


@job('dso_snapshots', 'daily', hour=3,
     description="Recalcul quotidien de l'ancienneté des impayés et historique DSO.")
def dso_snapshots(org, ctx):
    _require_access(org)
    from profitos.runtime import tenant_cx_direct, sync_sector_dso
    c = tenant_cx_direct(org['id'])
    try:
        root = None
        for eid in _entity_ids(c):
            res = refresh_dso_snapshot(c, eid)
            if eid is None:
                root = res
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    try:
        if root:
            sync_sector_dso(org['id'], root[0])
    except Exception:
        log.warning('benchmark DSO sectoriel non mis à jour org=%s', org['id'])
    return f'DSO {root[0]:.1f} j' if root else 'ok'


@job('trial_reminders', 'daily', hour=9,
     description="Emails de fin d'essai (J-3, J0) et d'échec de paiement.")
def trial_reminders(org, ctx):
    from profitos.runtime import parse_dt
    notices = []
    if str(org['plan'] or '').upper() == 'TRIAL' and org['trial_ends_at']:
        end = parse_dt(org['trial_ends_at'])
        if end:
            end_day = end.astimezone(PARIS).date()
            left = (end_day - paris_now(ctx.get('now')).date()).days
            if left == 3:
                notices.append(('trial_j3', end_day.isoformat()))
            elif left == 0:
                notices.append(('trial_j0', end_day.isoformat()))
    if str(org['status'] or '').upper() == 'PAST_DUE':
        notices.append(('past_due', str(org['updated_at'] or '')[:10]))
    if not notices:
        raise Skip('aucune notification')
    sent = []
    for kind, ref in notices:
        key = f'{kind}:{ref}'
        if not claim('trial_reminders:notice', org['id'], key):
            continue
        end_fr = '/'.join(reversed(ref.split('-'))) if kind != 'past_due' else ''
        if kind == 'trial_j3':
            r = _notify_owner(org, "Votre essai ProfitOS se termine dans 3 jours", "Votre essai se termine bientôt",
                              f"La période d'essai de {org['name']} se termine le {end_fr}. Pour conserver l'accès à vos données "
                              "et à vos automatisations (relances, rapports, synchronisations), choisissez une formule dès maintenant.",
                              'Choisir une formule', '/billing')
        elif kind == 'trial_j0':
            r = _notify_owner(org, "Votre essai ProfitOS se termine aujourd'hui", "Dernier jour d'essai",
                              f"La période d'essai de {org['name']} se termine aujourd'hui. Sans abonnement, l'accès aux "
                              "fonctionnalités sera suspendu ; vos données restent conservées.",
                              'Choisir une formule', '/billing')
        else:
            r = _notify_owner(org, "Échec du paiement de votre abonnement ProfitOS", "Paiement refusé",
                              f"Le dernier paiement de l'abonnement de {org['name']} a échoué. Mettez à jour votre moyen de "
                              "paiement pour éviter l'interruption du service.",
                              'Mettre à jour le paiement', '/billing')
        ok = bool(r.get('sent') or r.get('dry_run') or r.get('reason') == 'no_owner_email')
        finish('trial_reminders:notice', org['id'], key, 'success' if ok else 'error')
        if not ok:
            raise RuntimeError(f'notification {kind} non envoyée')
        sent.append(kind)
    return ', '.join(sent) or 'déjà notifié'


@job('weekly_accountant_export', 'weekly', hour=8, weekday=0,
     description="Envoi hebdomadaire du lien d'export au comptable (si activé dans Paramètres).")
def weekly_accountant_export(org, ctx):
    _require_access(org)
    from profitos.runtime import tenant_cx_direct, send_accountant_export
    c = tenant_cx_direct(org['id'])
    try:
        s = c.execute('SELECT weekly_export_enabled,accountant_email FROM app_settings WHERE id=1').fetchone()
    finally:
        c.close()
    if not s or not s['weekly_export_enabled'] or not s['accountant_email']:
        raise Skip('export hebdomadaire non activé')
    r = send_accountant_export(org, s['accountant_email'])
    if not r.get('sent') and not r.get('dry_run'):
        raise RuntimeError("échec d'envoi de l'export comptable")
    return 'envoyé'


@job('cloud_folder_sync', 'daily', hour=1, needs_disk=True,
     description='Import nocturne des factures déposées dans le dossier cloud connecté.')
def cloud_folder_sync(org, ctx):
    _require_access(org)
    from profitos.runtime import tenant_cx_direct
    from profitos.routes.cloud_storage import sync_cloud_folder
    c = tenant_cx_direct(org['id'])
    try:
        conns = c.execute("SELECT * FROM cloud_storage_connections WHERE COALESCE(status,'active')='active' AND COALESCE(folder_path,'')!=''").fetchall()
        if not conns:
            raise Skip('aucun dossier cloud')
        created, errors = 0, []
        for conn in conns:
            try:
                new, failed, _ = sync_cloud_folder(c, conn, conn['provider'], None, org['id'], max_files=500)
                created += len(new)
            except ValueError as exc:
                c.execute('UPDATE cloud_storage_connections SET last_sync_error=? WHERE id=?', (str(exc)[:300], conn['id']))
                c.commit()
                errors.append(conn['provider'])
        if errors:
            raise RuntimeError(f'listing refusé : {", ".join(errors)}')
        return f'{created} facture(s)'
    finally:
        c.close()


RETENTION_CANDIDATES = (
    ('profitos.retention', ('purge_deleted_organizations', 'purge_expired_logs', 'purge_log_retention')),
    ('profitos.runtime', ('purge_deleted_organizations', 'purge_expired_logs', 'purge_log_retention', 'purge_old_security_events')),
    ('profitos.routes.account', ('purge_deleted_organizations',)),
)


@job('retention_purge', 'weekly', hour=4, weekday=6, scope='global', needs_disk=True,
     description='Purges RGPD (organisations supprimées, journaux expirés).')
def retention_purge(org, ctx):
    done, seen = [], set()
    for module_name, names in RETENTION_CANDIDATES:
        try:
            mod = importlib.import_module(module_name)
        except Exception:
            continue
        for fname in names:
            fn = getattr(mod, fname, None)
            if fn is None or fname in seen:
                continue
            seen.add(fname)
            result = fn()
            done.append(f'{fname}={result}')
    if not done:
        log.warning('retention_purge : aucune fonction de purge disponible (purge_deleted_organizations absente)')
        _ops('RETENTION_PURGE_UNAVAILABLE', 'WARNING', 'aucune fonction de purge disponible')
        raise Skip('aucune fonction de purge disponible')
    return '; '.join(done)[:400]


@job('backup', 'daily', hour=2, scope='global', needs_disk=True,
     description='Sauvegarde chiffrée PostgreSQL + documents, envoi S3 ou rotation locale.')
def backup(org, ctx):
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location('profitos_backup_script', root / 'scripts' / 'backup.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.run_backup()
    return ', '.join(result.get('files', []))[:400]


# ---------------------------------------------------------------------------
# Déclenchement depuis le cron (service web, seul à monter le disque)
# ---------------------------------------------------------------------------

_web_running = {}
_web_lock = threading.Lock()


def _run_in_background(app, name, force):
    def target():
        try:
            report = run_job(app, name, force=force)
            if period_complete(report):
                mark_period_done(name)
        except Exception:
            log.exception('Tâche %s (déclenchée par le cron) en échec', name)
        finally:
            with _web_lock:
                _web_running.pop(name, None)
    t = threading.Thread(target=target, name=f'job-{name}', daemon=True)
    with _web_lock:
        if name in _web_running:
            return False
        _web_running[name] = t
    t.start()
    return True


def trigger_token():
    return (os.environ.get('JOBS_TRIGGER_TOKEN') or '').strip()


def register(app):
    """Routes : déclenchement interne des tâches, désinscription du rapport
    hebdomadaire, préférence utilisateur."""
    from flask import request, jsonify, abort, render_template, redirect, url_for, flash, session
    from profitos.runtime import limiter, login_required, auth_cx, now, weekly_unsubscribe_user_id

    try:
        ensure_ops_tables()
    except Exception:
        app.logger.exception('Tables job_runs / préférences non initialisées')

    def weekly_report_enabled():
        """Préférence de l'utilisateur connecté (cochée par défaut)."""
        uid = session.get('user_id')
        if not uid:
            return False
        try:
            c = auth_cx()
            try:
                row = c.execute('SELECT weekly_report_enabled FROM users WHERE id=?', (int(uid),)).fetchone()
            finally:
                c.close()
            return bool(row is None or row['weekly_report_enabled'] is None or int(row['weekly_report_enabled']) == 1)
        except Exception:
            return True

    app.jinja_env.globals['weekly_report_enabled'] = weekly_report_enabled

    @app.post('/api/v1/internal/jobs/<name>')
    @limiter.exempt
    def internal_job_trigger(name):
        token = trigger_token()
        sent = (request.headers.get('Authorization') or '')
        if not token or not sent.startswith('Bearer ') or not hmac.compare_digest(sent[7:].strip(), token):
            abort(404)
        if name not in JOBS:
            abort(404)
        started = _run_in_background(app, name, force=request.args.get('force') == '1')
        return jsonify(job=name, status='started' if started else 'already_running'), (202 if started else 409)

    def _set_weekly_pref(user_id, enabled):
        c = auth_cx()
        try:
            c.execute('UPDATE users SET weekly_report_enabled=?,updated_at=? WHERE id=?', (1 if enabled else 0, now(), int(user_id)))
            c.commit()
        finally:
            c.close()

    @app.route('/email/desinscription/<token>', methods=['GET', 'POST'])
    def weekly_report_unsubscribe(token):
        user_id = weekly_unsubscribe_user_id(token)
        if not user_id:
            abort(404)
        done = False
        if request.method == 'POST':
            _set_weekly_pref(user_id, False)
            done = True
        return render_template('email_unsubscribe.html', token=token, done=done)

    @app.post('/api/v1/email/unsubscribe/<token>')
    @limiter.exempt
    def weekly_report_unsubscribe_one_click(token):
        # RFC 8058 : POST « List-Unsubscribe=One-Click » envoyé par le client mail.
        user_id = weekly_unsubscribe_user_id(token)
        if not user_id:
            abort(404)
        _set_weekly_pref(user_id, False)
        return ('', 204)

    @app.post('/parametres/rapport-hebdomadaire')
    @login_required
    def weekly_report_preference():
        enabled = request.form.get('weekly_report_enabled') == 'on'
        _set_weekly_pref(session['user_id'], enabled)
        flash('Rapport hebdomadaire activé.' if enabled else 'Rapport hebdomadaire désactivé.')
        return redirect(url_for('settings'))
