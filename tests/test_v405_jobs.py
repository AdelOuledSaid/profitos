"""Tâches planifiées : planification, isolation, idempotence, tâches métier."""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import profitos.runtime as rt
from profitos import jobs, create_app
from profitos.config import DevelopmentConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import run_job  # noqa: E402


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('SMTP_HOST', raising=False)
    monkeypatch.delenv('JOBS_TRIGGER_TOKEN', raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def add_org(org_id, plan='PRO', status='ACTIVE_PAID', trial_ends_at=None, email=None):
    ac = rt.auth_cx()
    ac.execute('INSERT INTO organizations(id,name,plan,status,trial_ends_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
               (org_id, f'Org {org_id}', plan, status, trial_ends_at, rt.now(), '2026-10-01T10:00:00+00:00'))
    ac.execute('INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)',
               (org_id, email or f'owner{org_id}@exemple.fr', 'x', rt.now()))
    ac.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'OWNER',?)", (org_id, org_id, rt.now()))
    ac.commit(); ac.close()
    rt.migrate_tenant(org_id, force=True)


# --- Planification -------------------------------------------------------------

def test_registry_contains_all_promised_jobs():
    for name in ('weekly_reports', 'recurring_invoices', 'einvoice_inbound_sync', 'bank_sync', 'boamp_refresh',
                 'dso_snapshots', 'trial_reminders', 'weekly_accountant_export', 'retention_purge', 'backup',
                 'cloud_folder_sync'):
        assert name in jobs.JOBS
    for name in ('einvoice_inbound_sync', 'cloud_folder_sync', 'backup', 'retention_purge'):
        assert jobs.JOBS[name].needs_disk


def test_weekly_due_uses_paris_time_with_dst():
    j = jobs.JOBS['weekly_reports']
    # lundi 5 octobre 2026 (heure d'été, UTC+2)
    assert not jobs.is_due(j, datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc))   # 06:30 Paris
    assert jobs.is_due(j, datetime(2026, 10, 5, 5, 30, tzinfo=timezone.utc))       # 07:30 Paris
    # lundi 7 décembre 2026 (heure d'hiver, UTC+1)
    assert not jobs.is_due(j, datetime(2026, 12, 7, 5, 30, tzinfo=timezone.utc))   # 06:30 Paris
    assert jobs.is_due(j, datetime(2026, 12, 7, 6, 5, tzinfo=timezone.utc))        # 07:05 Paris
    assert jobs.is_due(j, datetime(2026, 12, 9, 1, 0, tzinfo=timezone.utc))        # rattrapage mercredi
    assert jobs.period_key(j, datetime(2026, 12, 9, 1, 0, tzinfo=timezone.utc)) == '2026-W50'
    assert jobs.period_key(jobs.JOBS['einvoice_inbound_sync'], datetime(2026, 12, 9, 1, 0, tzinfo=timezone.utc)) == '2026-12-09T02'


# --- Isolation & idempotence ------------------------------------------------------

def test_org_failure_is_isolated_and_retried_then_abandoned(app, monkeypatch):
    for oid in (1, 2, 3):
        add_org(oid)
    calls = []

    def fn(org, ctx):
        calls.append(org['id'])
        if org['id'] == 2:
            raise RuntimeError('panne org 2')
        return 'ok'
    monkeypatch.setitem(jobs.JOBS, 'demo', jobs.Job('demo', fn, 'daily'))
    r1 = jobs.run_job(app, 'demo')
    assert sorted(r1['ok']) == [1, 3] and list(r1['failed']) == [2]
    assert not jobs.period_complete(r1)
    r2 = jobs.run_job(app, 'demo')
    assert sorted(r2['already']) == [1, 3] and list(r2['failed']) == [2]
    jobs.run_job(app, 'demo')
    r4 = jobs.run_job(app, 'demo')  # 3 tentatives épuisées : plus rejouée
    assert 2 in r4['already']
    assert calls.count(1) == 1 and calls.count(2) == 3
    assert jobs.period_complete(r1) and jobs.period_complete(r2)
    r5 = jobs.run_job(app, 'demo', force=True, org_ids={1})
    assert r5['ok'] == [1]


def test_dispatcher_marks_period_done(app, monkeypatch):
    add_org(1)
    monkeypatch.setattr(jobs, 'JOBS', {'demo': jobs.Job('demo', lambda org, ctx: 'ok', 'hourly')})
    assert [j.name for j in jobs.due_jobs()] == ['demo']
    assert run_job.main(['--due'], app=app) == 0
    assert jobs.due_jobs() == []


def test_run_job_exit_code_on_failure(app, monkeypatch):
    add_org(1)

    def boom(org, ctx):
        raise ValueError('x')
    monkeypatch.setitem(jobs.JOBS, 'boom', jobs.Job('boom', boom, 'daily'))
    assert run_job.main(['boom'], app=app) == 1


def test_disk_job_is_delegated_in_production_and_fails_without_token(app, monkeypatch, capsys):
    monkeypatch.setenv('PROFITOS_ENV', 'production')
    assert run_job.main(['backup'], app=app) == 1
    assert 'JOBS_TRIGGER_TOKEN' in capsys.readouterr().out


def test_disk_job_delegation_posts_bearer_token(app, monkeypatch):
    monkeypatch.setenv('JOBS_TRIGGER_TOKEN', 'secret-token')
    monkeypatch.setenv('APP_BASE_URL', 'https://app.profitos.fr')
    seen = {}

    class Resp:
        status_code = 202
    import requests
    monkeypatch.setattr(requests, 'post', lambda url, headers=None, timeout=None: seen.update(url=url, headers=headers, timeout=timeout) or Resp())
    assert run_job.main(['einvoice_inbound_sync'], app=app) == 0
    assert seen['url'] == 'https://app.profitos.fr/api/v1/internal/jobs/einvoice_inbound_sync'
    assert seen['headers']['Authorization'] == 'Bearer secret-token' and seen['timeout'] <= 30


def test_internal_trigger_requires_token(app, monkeypatch):
    client = app.test_client()
    assert client.post('/api/v1/internal/jobs/backup').status_code == 404
    monkeypatch.setenv('JOBS_TRIGGER_TOKEN', 'tok')
    started = []
    monkeypatch.setattr(jobs, '_run_in_background', lambda a, name, force: started.append(name) or True)
    assert client.post('/api/v1/internal/jobs/backup', headers={'Authorization': 'Bearer nope'}).status_code == 404
    r = client.post('/api/v1/internal/jobs/backup', headers={'Authorization': 'Bearer tok'})
    assert r.status_code == 202 and started == ['backup']
    assert client.post('/api/v1/internal/jobs/inconnu', headers={'Authorization': 'Bearer tok'}).status_code == 404


# --- Tâches métier ---------------------------------------------------------------

def test_weekly_reports_new_means_first_reported(app, monkeypatch):
    add_org(1)
    real_send = rt.send_email
    # rendu réel du template, envoi considéré réussi (un envoi simulé ne marque rien)
    monkeypatch.setattr(rt, 'send_email', lambda *a, **k: dict(real_send(*a, **k), sent=True))
    c = rt.tenant_cx_direct(1)
    c.execute("INSERT INTO invoices(invoice_number,customer,amount,paid_amount,status,days_overdue,score,created_at) VALUES('F1','Client A',1000,0,'unpaid',20,50,?)", (rt.now(),))
    c.commit(); c.close()
    with app.test_request_context('/'):
        assert rt.compute_weekly_digest(1)['recover_count'] == 1
    r = jobs.run_job(app, 'weekly_reports')
    assert r['ok'] == [1]
    # Ré-import : la même facture est recréée (created_at réinitialisé) -> pas « nouvelle »
    c = rt.tenant_cx_direct(1)
    c.execute('DELETE FROM invoices')
    c.execute("INSERT INTO invoices(invoice_number,customer,amount,paid_amount,status,days_overdue,score,created_at) VALUES('F1','Client A',1000,0,'unpaid',27,55,?)", (rt.now(),))
    c.execute("INSERT INTO invoices(invoice_number,customer,amount,paid_amount,status,days_overdue,score,created_at) VALUES('F2','Client B',500,0,'unpaid',3,20,?)", (rt.now(),))
    c.commit(); c.close()
    with app.test_request_context('/'):
        d = rt.compute_weekly_digest(1)
    assert d['recover_count'] == 1 and d['recover_top'][0]['invoice_number'] == 'F2'


def test_weekly_report_respects_unsubscribe(app):
    add_org(1)
    client = app.test_client()
    with app.test_request_context('/'):
        token = rt.weekly_unsubscribe_token(1)
        assert rt.weekly_unsubscribe_user_id('forged') is None
    assert client.get(f'/email/desinscription/{token}').status_code == 200
    assert client.get('/email/desinscription/forged').status_code == 404
    assert client.post(f'/api/v1/email/unsubscribe/{token}').status_code == 204
    r = jobs.run_job(app, 'weekly_reports')
    assert r['skipped'] == [1]


def test_weekly_email_has_list_unsubscribe_header_and_french_subject(app, monkeypatch):
    add_org(1)
    sent = []
    monkeypatch.setattr(rt, 'send_email', lambda to, subject, html, dry_run=None, headers=None, **k: sent.append((to, subject, html, headers)) or {'sent': True})
    with app.test_request_context('/'):
        org = {'id': 1, 'name': 'Org 1'}
        rt.send_weekly_email(org, rt.compute_weekly_digest(1))
    to, subject, html, headers = sent[0]
    assert subject == 'Votre rapport financier de la semaine — Org 1'
    assert headers['List-Unsubscribe'].startswith('<') and '/api/v1/email/unsubscribe/' in headers['List-Unsubscribe']
    assert headers['List-Unsubscribe-Post'] == 'List-Unsubscribe=One-Click'
    assert '/email/desinscription/' in html and 'Settings' not in html


def test_recurring_invoices_job_creates_drafts_and_notifies(app, monkeypatch):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    items = json.dumps([{'description': 'Maintenance', 'quantity': 1, 'unit_price': 100, 'vat_rate': 20, 'line_total': 100}])
    c.execute("""INSERT INTO recurring_invoice_templates(client_name,client_email,line_items,frequency,next_run_date,created_at,updated_at)
                 VALUES('Client R','r@exemple.fr',?,'monthly',?,?,?)""", (items, date.today().isoformat(), rt.now(), rt.now()))
    c.commit(); c.close()
    notified = []
    monkeypatch.setattr(jobs, '_notify_owner', lambda org, subject, *a, **k: notified.append(subject) or {'sent': True})
    r = jobs.run_job(app, 'recurring_invoices')
    assert r['ok'] == [1]
    c = rt.tenant_cx_direct(1)
    rows = c.execute('SELECT status FROM outgoing_invoices').fetchall()
    c.close()
    assert [x['status'] for x in rows] == ['draft']  # jamais émise automatiquement
    assert notified and 'récurrente' in notified[0]
    ac = rt.auth_cx()
    assert ac.execute('SELECT COUNT(*) n FROM outgoing_invoice_tokens WHERE organization_id=1').fetchone()['n'] == 1
    ac.close()


def test_recurring_invoices_skipped_without_active_subscription(app):
    # L'essai en cours donne les fonctions Pro (comme côté web) ; un essai expiré
    # ou un abonnement résilié ne génère plus rien.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    add_org(1, plan='TRIAL', status='ACTIVE', trial_ends_at=past)
    add_org(2, plan='PRO', status='CANCELED')
    assert sorted(jobs.run_job(app, 'recurring_invoices')['skipped']) == [1, 2]


def test_jobs_access_matches_web_rules():
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    base = {'id': 1, 'parent_org_id': None, 'updated_at': None}
    assert jobs.has_access(dict(base, plan='TRIAL', status='ACTIVE', trial_ends_at=future, past_due_since=None))
    assert jobs.has_access(dict(base, plan='PRO', status='PAST_DUE', trial_ends_at=None, past_due_since=recent))
    assert not jobs.has_access(dict(base, plan='PRO', status='PAST_DUE', trial_ends_at=None, past_due_since=old))
    assert not jobs.has_access(dict(base, plan='PRO', status='CANCELED', trial_ends_at=None, past_due_since=None))


def test_trial_reminders_j3_sent_once(app, monkeypatch):
    end = (datetime.now(jobs.PARIS) + timedelta(days=3)).replace(hour=12).astimezone(timezone.utc).isoformat()
    add_org(1, plan='TRIAL', status='ACTIVE', trial_ends_at=end)
    add_org(2, plan='PRO', status='PAST_DUE')
    subjects = []
    monkeypatch.setattr(jobs, '_notify_owner', lambda org, subject, *a, **k: subjects.append((org['id'], subject)) or {'sent': True})
    jobs.run_job(app, 'trial_reminders')
    jobs.run_job(app, 'trial_reminders', force=True)  # même période forcée : pas de doublon
    assert subjects == [(1, 'Votre essai ProfitOS se termine dans 3 jours'),
                        (2, 'Échec du paiement de votre abonnement ProfitOS')]


def test_dso_snapshot_refreshes_days_overdue(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    due = (date.today() - timedelta(days=10)).isoformat()
    c.execute("INSERT INTO invoices(invoice_number,customer,amount,paid_amount,due_date,status,days_overdue,score,created_at,kind) VALUES('F1','A',1000,0,?,'unpaid',1,1,?,'STANDARD')", (due, rt.now()))
    c.commit(); c.close()
    assert jobs.run_job(app, 'dso_snapshots')['ok'] == [1]
    c = rt.tenant_cx_direct(1)
    assert c.execute('SELECT days_overdue FROM invoices').fetchone()['days_overdue'] == 10
    snap = c.execute('SELECT * FROM dso_snapshots').fetchone()
    c.close()
    assert snap['avg_days_overdue'] == 10 and snap['invoice_count'] == 1


def test_weekly_accountant_export_only_when_enabled(app, monkeypatch):
    add_org(1); add_org(2)
    c = rt.tenant_cx_direct(1)
    c.execute("INSERT INTO app_settings(id,weekly_export_enabled,accountant_email) VALUES(1,1,'compta@cabinet.fr')")
    c.commit(); c.close()
    sent = []
    monkeypatch.setattr(rt, 'send_accountant_export', lambda org, email, dry_run=None: sent.append(email) or {'sent': True})
    r = jobs.run_job(app, 'weekly_accountant_export')
    assert r['ok'] == [1] and r['skipped'] == [2] and sent == ['compta@cabinet.fr']


def test_retention_purge_skips_cleanly_when_functions_absent(app, monkeypatch):
    monkeypatch.setattr(jobs, 'RETENTION_CANDIDATES', (('profitos.nexistepas', ('purge_deleted_organizations',)),))
    assert jobs.run_job(app, 'retention_purge')['skipped'] == [0]


def test_einvoice_job_skips_orgs_without_weinvoice(app):
    add_org(1)
    assert jobs.run_job(app, 'einvoice_inbound_sync')['skipped'] == [1]


def test_sync_grow_upsert_preserves_created_at_and_status(app, monkeypatch):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    c.execute("INSERT INTO company(id,name,activities) VALUES(1,'X','plomberie')")
    c.commit(); c.close()
    monkeypatch.setattr(rt, 'score_market', lambda rec, p: (80, ['69'], ['ok'], []))
    rows = [{'objet': 'Travaux plomberie', 'nomacheteur': 'Ville', 'url_avis': 'https://boamp.fr/avis/1'}]
    with app.test_request_context('/'):
        from flask import session
        session['org_id'] = 1
        assert rt.sync_grow(rows=rows) == 1
        c = rt.tenant_cx_direct(1)
        first = c.execute("SELECT id,created_at FROM opportunities WHERE type='GROW'").fetchone()
        c.execute("UPDATE opportunities SET created_at='2020-01-01T00:00:00+00:00',status='IN_PROGRESS' WHERE id=?", (first['id'],))
        c.commit(); c.close()
        assert rt.sync_grow(rows=rows) == 1
        c = rt.tenant_cx_direct(1)
        again = c.execute("SELECT id,created_at,status FROM opportunities WHERE type='GROW'").fetchall()
        c.close()
    assert len(again) == 1 and again[0]['id'] == first['id']
    assert again[0]['created_at'].startswith('2020') and again[0]['status'] == 'IN_PROGRESS'


def test_ui_wording_matches_automation():
    rec = (ROOT / 'templates' / 'recurring_invoices.html').read_text(encoding='utf-8')
    assert 'Chaque matin, ProfitOS génère automatiquement en brouillon' in rec
    settings = (ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
    assert "weekly_report_preference" in settings and 'chaque lundi' in settings
    weekly = (ROOT / 'templates' / 'email_weekly.html').read_text(encoding='utf-8')
    assert 'ajuster la fréquence' not in weekly and 'unsubscribe_url' in weekly
