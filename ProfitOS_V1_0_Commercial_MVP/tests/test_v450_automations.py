"""v450 — Production réglementaire, lot 3 : automatisations (écritures récurrentes,
rappels d'échéances fiscales, suivi automatique des télédéclarations, contrôles avant
liasse)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import profitos.runtime as rt
from profitos import jobs, create_app, automation as A
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('SMTP_HOST', raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def add_org(org_id, plan='BUSINESS', status='ACTIVE_PAID'):
    ac = rt.auth_cx()
    ac.execute('INSERT INTO organizations(id,name,plan,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
               (org_id, f'Org {org_id}', plan, status, rt.now(), rt.now()))
    ac.execute('INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)',
               (org_id, f'owner{org_id}@exemple.fr', 'x', rt.now()))
    ac.execute("INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,'OWNER',?)", (org_id, org_id, rt.now()))
    ac.commit(); ac.close()
    rt.migrate_tenant(org_id, force=True)


RENT = [{'account_code': '613200', 'debit': 1000}, {'account_code': '445660', 'debit': 200},
        {'account_code': '401000', 'credit': 1200}]


def test_recurring_entries_catch_up_and_idempotent(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    tid = A.create_recurring_entry(c, None, 'Loyer du local', 'OD', RENT, 'monthly', '2026-01-31', '2026-05-31')
    created, errors = A.generate_due_entries(c, None, date(2026, 4, 15))
    assert not errors and [x['date'] for x in created] == ['2026-01-31', '2026-02-28', '2026-03-31']
    again, _ = A.generate_due_entries(c, None, date(2026, 4, 15))
    assert again == []
    created, _ = A.generate_due_entries(c, None, date(2026, 12, 31))
    assert [x['date'] for x in created] == ['2026-04-30', '2026-05-31']  # fin au 31/05
    row = c.execute('SELECT active FROM recurring_entries WHERE id=?', (tid,)).fetchone()
    total = c.execute("SELECT SUM(debit) d FROM accounting_entry_lines WHERE account_code='613200'").fetchone()['d']
    c.close()
    assert row['active'] == 0 and total == 5000


def test_recurring_entry_validation(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    with pytest.raises(A.AutomationError, match='équilibrée'):
        A.create_recurring_entry(c, None, 'Faux', 'OD', [{'account_code': '613200', 'debit': 100},
                                                         {'account_code': '401000', 'credit': 90}], 'monthly', '2026-01-01')
    with pytest.raises(A.AutomationError, match='Compte inconnu'):
        A.create_recurring_entry(c, None, 'Faux', 'OD', [{'account_code': '999999', 'debit': 100},
                                                         {'account_code': '401000', 'credit': 100}], 'monthly', '2026-01-01')
    with pytest.raises(A.AutomationError, match='Fréquence'):
        A.create_recurring_entry(c, None, 'Faux', 'OD', RENT, 'weekly', '2026-01-01')
    c.close()


def test_recurring_entries_job(app, monkeypatch):
    add_org(1)
    add_org(2, plan='STARTER')  # comptabilité non incluse
    c = rt.tenant_cx_direct(1)
    A.create_recurring_entry(c, None, 'Abonnement logiciel', 'OD',
                             [{'account_code': '651000', 'debit': 50}, {'account_code': '512000', 'credit': 50}],
                             'monthly', (date.today() - timedelta(days=1)).isoformat())
    c.close()
    r = jobs.run_job(app, 'recurring_entries')
    assert r['ok'] == [1] and 2 in r['skipped']
    c = rt.tenant_cx_direct(1)
    assert c.execute("SELECT COUNT(*) n FROM accounting_entries WHERE source_type LIKE 'recurring_entry:%%'").fetchone()['n'] == 1
    c.close()


def test_fiscal_deadline_reminders(app, monkeypatch):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    create_entry(c, 'VE', '2026-10-05', 'Vente', [{'account_code': '411000', 'debit': 1200, 'auxiliary_name': 'C'},
                                                 {'account_code': '706000', 'credit': 1000},
                                                 {'account_code': '445710', 'credit': 200, 'vat_rate': 20.0, 'vat_base': 1000}])
    items = A.upcoming_deadlines(c, None, date(2026, 11, 8), horizon_days=10)
    c.close()
    tva = [x for x in items if x['form'] == '3310-CA3']
    assert tva and tva[0]['date'] == date(2026, 11, 15) and tva[0]['amount'] == 200 and not tva[0]['deposited']
    import profitos.accounting as acc
    monkeypatch.setattr(acc, 'paris_today', lambda: date(2026, 11, 8))
    sent = []
    monkeypatch.setattr(jobs, '_notify_owner', lambda org, subject, title, intro, *a, **k: sent.append(intro) or {'sent': True})
    r = jobs.run_job(app, 'fiscal_deadlines')
    assert r['ok'] == [1] and sent and '15/11/2026' in sent[0] and '3310-CA3' in sent[0]
    # Ni rappel à 5 jours, ni pour une déclaration déjà déposée.
    sent.clear()
    monkeypatch.setattr(acc, 'paris_today', lambda: date(2026, 11, 10))
    jobs.run_job(app, 'fiscal_deadlines', force=True)
    assert not sent


def test_teledeclaration_status_job_notifies_rejection(app, monkeypatch):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    c.execute("""INSERT INTO teledeclarations(entity_id,decl_type,form,channel,period_start,period_end,payload_json,payload_hash,
                 status,mode,provider,remote_id,history_json,submitted_at,updated_at)
                 VALUES(NULL,'TVA_CA3','3310-CA3','EDI-TVA','2026-09-01','2026-09-30','{}','h','deposited','production','partenaire','EDI-1','[]',?,?)""",
              (rt.now(), rt.now()))
    c.commit(); c.close()
    from profitos import teledeclaration as T

    class Fake:
        name, production = 'partenaire', True

        def fetch_status(self, remote_id, status, payload):
            return {'status': 'rejected', 'rejection_reasons': ['SIREN inconnu']}
    monkeypatch.setattr(T, 'get_connector', lambda: Fake())
    sent = []
    monkeypatch.setattr(jobs, '_notify_owner', lambda org, subject, *a, **k: sent.append(subject) or {'sent': True})
    r = jobs.run_job(app, 'teledeclaration_status')
    assert r['ok'] == [1] and sent and 'rejetée' in sent[0]
    c = rt.tenant_cx_direct(1)
    row = c.execute('SELECT status,rejection_json FROM teledeclarations').fetchone()
    c.close()
    assert row['status'] == 'rejected' and json.loads(row['rejection_json']) == ['SIREN inconnu']


def test_precontrols(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    create_entry(c, 'VE', '2025-05-01', 'Vente', [{'account_code': '411000', 'debit': 12000, 'auxiliary_name': 'C'},
                                                 {'account_code': '706000', 'credit': 10000},
                                                 {'account_code': '445710', 'credit': 2000, 'vat_rate': 20.0, 'vat_base': 10000}])
    out = A.precontrols(c, date(2025, 1, 1), date(2025, 12, 31), None, today=date(2026, 3, 1))
    c.close()
    labels = ' | '.join(x['label'] for x in out)
    assert "impôt sur les sociétés" in labels and 'TVA' in labels and 'Capital social' in labels and 'clôturé' in labels


def test_pages_render(app):
    from tests.test_v421_invoice_compliance import _post
    import uuid
    client = app.test_client()
    _post(client, '/signup', {'accept_terms': '1', 'full_name': 'Alice', 'email': f'{uuid.uuid4().hex[:8]}@exemple.fr',
                              'password': 'Brume-Cactus-Velo-48', 'company_name': 'Auto SAS'})
    with client.session_transaction() as s:
        org = s['org_id']
    ac = rt.auth_cx(); ac.execute("UPDATE organizations SET plan='BUSINESS',status='ACTIVE_PAID' WHERE id=?", (org,)); ac.commit(); ac.close()
    assert 'Prochaines échéances' in client.get('/comptabilite/echeances').get_data(as_text=True)
    assert 'Nouvelle écriture récurrente' in client.get('/comptabilite/ecritures-recurrentes').get_data(as_text=True)
    r = _post(client, '/comptabilite/ecritures-recurrentes', {
        'action': 'create', 'label': 'Loyer', 'journal_code': 'OD', 'frequency': 'monthly', 'start_date': '2026-01-31',
        'account_1': '613200', 'debit_1': '1 000,00', 'account_2': '445660', 'debit_2': '200', 'account_3': '401000', 'credit_3': '1 200'})
    assert r.status_code == 302
    c = rt.tenant_cx_direct(org)
    assert c.execute('SELECT COUNT(*) n FROM recurring_entries').fetchone()['n'] == 1
    c.close()
    _post(client, '/comptabilite/ecritures-recurrentes', {'action': 'run'})
    c = rt.tenant_cx_direct(org)
    assert c.execute("SELECT COUNT(*) n FROM accounting_entries WHERE source_type LIKE 'recurring_entry:%%'").fetchone()['n'] >= 1
    c.close()
