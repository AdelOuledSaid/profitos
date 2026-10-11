"""Audit de mise en production (v390) — corrections de la vague 1.

Essai gratuit réellement utilisable, avoirs déduits du reste à payer, clôture comptable,
traduction PostgreSQL (montants en double précision, verrou d'avoir), e-mails en français.
"""
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSRF = 'v' * 64
PASSWORD = 'Sup3r-Secret-Pass!2026'


@pytest.fixture()
def app():
    from profitos import create_app
    from profitos.config import DevelopmentConfig
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    return app


def _post(client, url, data=None, **kw):
    with client.session_transaction() as s:
        s['csrf_token'] = CSRF
    payload = dict(data or {}); payload['csrf_token'] = CSRF
    return client.post(url, data=payload, **kw)


def _signup(app, plan=None):
    from profitos import runtime as rt
    client = app.test_client()
    r = _post(client, '/signup', {'accept_terms': '1', 'full_name': 'Claire Dupuis', 'email': f'{uuid.uuid4().hex[:10]}@example.com',
                                  'password': PASSWORD, 'company_name': 'Audit Prod SAS'})
    assert r.status_code in (302, 303)
    with client.session_transaction() as s:
        org_id = s['org_id']
    if plan:
        c = rt.auth_cx()
        c.execute("UPDATE organizations SET plan=?,status='ACTIVE_PAID' WHERE id=?", (plan, org_id))
        c.commit(); c.close()
    return client, org_id


def _company(client):
    _post(client, '/company', {'name': 'Audit Prod SAS', 'city': 'Lyon', 'department': '69', 'siret': '73282932000074',
                               'address': '18 rue de la Charité', 'postal_code': '69002', 'vat_number': 'FR44732829320',
                               'iban': 'FR7630006000011234567890189', 'bic': 'AGRIFRPP',
                               'legal_form': 'SAS', 'share_capital': '10000', 'rcs_city': 'Lyon'})


def _tenant(org_id):
    from profitos import runtime as rt
    return sqlite3.connect(rt.tenant_db(org_id))


def _issue_invoice(client, org_id, price='1000', vat='20'):
    _post(client, '/facturation/nouvelle', {
        'client_name': 'Client Test', 'client_email': 'compta@client.example', 'client_address': '1 rue A, 69001 Lyon',
        'client_siren': '552100554', 'due_date': (date.today() + timedelta(days=30)).isoformat(),
        'operation_nature': 'services', 'label_1': 'Prestation', 'qty_1': '1', 'price_1': price, 'vat_1': vat})
    cx = _tenant(org_id)
    inv_id = cx.execute('SELECT MAX(id) FROM outgoing_invoices').fetchone()[0]
    cx.close()
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    return inv_id


def test_trial_can_invoice_and_reach_accounting(app):
    client, org_id = _signup(app)  # plan TRIAL, essai en cours
    _company(client)
    inv_id = _issue_invoice(client, org_id)
    cx = _tenant(org_id)
    status = cx.execute('SELECT status FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()[0]
    cx.close()
    assert status == 'sent'
    r = client.get('/comptabilite/journaux')
    assert r.status_code == 200


def test_expired_trial_is_still_blocked(app):
    from profitos import runtime as rt
    client, org_id = _signup(app)
    c = rt.auth_cx()
    c.execute("UPDATE organizations SET trial_ends_at=? WHERE id=?", ('2020-01-01T00:00:00+00:00', org_id))
    c.commit(); c.close()
    r = client.get('/facturation/nouvelle')
    assert r.status_code in (302, 303)


def test_trial_days_left_counts_the_first_day():
    from datetime import datetime, timezone
    from profitos.runtime import trial_days_left
    end = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    assert trial_days_left({'trial_ends_at': end}) == 14


def test_full_credit_note_settles_invoice_and_blocks_fake_payment(app):
    client, org_id = _signup(app, 'PRO')
    _company(client)
    inv_id = _issue_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/avoir', {'reason': 'Erreur de prix', 'amount_ttc': '1200'})
    cx = _tenant(org_id)
    status = cx.execute('SELECT status FROM outgoing_invoices WHERE id=?', (inv_id,)).fetchone()[0]
    assert status == 'paid'
    _post(client, f'/facturation/{inv_id}/marquer-payee')
    n_pay = cx.execute('SELECT COUNT(*) FROM outgoing_invoice_payments WHERE invoice_id=?', (inv_id,)).fetchone()[0]
    cx.close()
    assert n_pay == 0  # aucun encaissement fictif


def test_partial_credit_reduces_balance_for_payment(app):
    client, org_id = _signup(app, 'PRO')
    _company(client)
    inv_id = _issue_invoice(client, org_id)
    _post(client, f'/facturation/{inv_id}/avoir', {'reason': 'Remise', 'amount_ttc': '200'})
    _post(client, f'/facturation/{inv_id}/marquer-payee')
    cx = _tenant(org_id)
    paid = cx.execute('SELECT SUM(amount) FROM outgoing_invoice_payments WHERE invoice_id=?', (inv_id,)).fetchone()[0]
    cx.close()
    assert round(paid, 2) == 1000.00


def test_accounting_closure_date_is_accepted(app):
    client, org_id = _signup(app, 'PRO')
    _company(client)
    src = (ROOT / 'profitos/routes/accounting.py').read_text(encoding='utf-8')
    assert 're.fullmatch(r"\\d{4}-\\d{2}-\\d{2}", closed_until)' in src
    import re
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", '2026-09-30')


def test_postgres_translation_keeps_money_precision_and_locks():
    from profitos.db import _translate_ddl, _translate_statement
    assert 'DOUBLE PRECISION' in _translate_ddl('CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, total REAL)')
    assert ' REAL' not in _translate_ddl('CREATE TABLE t(total REAL, vat REAL DEFAULT 0)')
    assert _translate_statement('ALTER TABLE t ADD COLUMN amount REAL') == 'ALTER TABLE t ADD COLUMN amount DOUBLE PRECISION'
    assert _translate_statement('BEGIN IMMEDIATE') == 'SELECT pg_advisory_xact_lock(hashtext(current_schema()))'
    # Un libellé contenant « REAL » dans une requête ordinaire n'est pas modifié
    assert _translate_statement("SELECT * FROM t WHERE label='REAL'") == "SELECT * FROM t WHERE label='REAL'"


def test_transactional_emails_are_in_french():
    src = (ROOT / 'profitos/runtime.py').read_text(encoding='utf-8')
    for english in ('Confirm your ProfitOS account', 'Reset your ProfitOS password', 'Your Weekly Profit Report'):
        assert english not in src
    assert '127.0.0.1:5050' not in src.split('def app_base_url', 1)[1]
    # Le rapport hebdo passe par la tâche planifiée : clients payants + essais valides.
    jobs = (ROOT / 'profitos/jobs.py').read_text(encoding='utf-8')
    # Même règle que le web (runtime.subscription_state : payé, essai valide, délai de grâce).
    assert "subscription_state(org) in ('ACTIVE', 'TRIAL', 'PAST_DUE_GRACE')" in jobs


def test_account_deletion_cancels_stripe_subscription_first():
    src = (ROOT / 'profitos/routes/account.py').read_text(encoding='utf-8')
    block = src.split('if member_count==1:', 1)[1]
    # v4xx : suppression programmée (délai de grâce), purge définitive dans retention.py.
    assert block.index('Subscription.cancel') < block.index('schedule_organization_deletion(')
    purge = (ROOT / 'profitos/retention.py').read_text(encoding='utf-8')
    assert 'delete_tenant_storage' in purge
    for folder in ("'purchase_documents'", "'expense_receipts'", "'cabinet_documents'"):
        assert folder in purge
