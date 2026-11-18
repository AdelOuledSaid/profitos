"""v513 — Une vente à un particulier ne se transmet pas à la plateforme agréée.

Rien ne regardait le périmètre e-reporting : une facture B2C proposait « Transmettre la
facture électronique » comme n'importe quelle autre. Or un particulier n'est inscrit à aucun
annuaire, et l'obligation qui pèse sur cette opération est de déclarer des montants
(e-reporting), pas d'acheminer un document. Transmettre aurait produit un refus de la
plateforme, et surtout laissé croire l'obligation satisfaite alors que la déclaration
restait à faire.
"""
import uuid

import pytest

import profitos.runtime as rt
from profitos import create_app, storage
from profitos.config import DevelopmentConfig

CSRF = 'c' * 64


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.setattr(storage, 'UPLOAD_ROOT', tmp_path / 'uploads')
    app = create_app(DevelopmentConfig)
    app.config.update(TESTING=True)
    try:
        rt.limiter.enabled = False
    except Exception:
        pass
    cl = app.test_client()

    def csrf():
        with cl.session_transaction() as s:
            s['csrf_token'] = CSRF

    csrf()
    cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'first_name': 'A', 'last_name': 'M',
                             'email': f'{uuid.uuid4().hex[:10]}@example.com',
                             'password': 'Brume-Cactus-Velo-48', 'company_name': 'Tricatel'})
    with cl.session_transaction() as s:
        org, uid = s['org_id'], s['user_id']
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='BUSINESS',status='ACTIVE_PAID' WHERE id=?", (org,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit()
    ac.close()
    csrf()
    cl.post('/company', data={'csrf_token': CSRF, 'name': 'Tricatel', 'legal_form': 'SAS',
                              'siret': '73282932000074', 'address': '1 rue Test', 'postal_code': '75001',
                              'city': 'Paris', 'share_capital': '1000', 'rcs_city': 'Paris',
                              'payment_terms': '30 jours', 'late_penalty_rate': '10'})
    # le bac à sable n'est activé qu'ici : en pré-lancement, l'inscription est fermée
    monkeypatch.setenv('SUPERPDP_COMPANY_NUMBER', '000000001')
    monkeypatch.setenv('PROFITOS_PRELAUNCH', '1')
    import profitos.db as dbmod
    c = dbmod.connect_tenant(org, rt.tenant_db(org))
    yield cl, csrf, c
    c.close()


def make_invoice(cl, csrf, c, scope):
    csrf()
    cl.post('/facturation/nouvelle', data={
        'csrf_token': CSRF, 'client_name': 'Marie Dupont', 'client_address': '12 rue de la Paix\n75002 Paris',
        'client_siren': '' if scope else '000000002', 'due_date': '2027-01-01',
        'operation_nature': 'services', 'vat_treatment': 'standard', 'ereporting_scope': scope,
        'ereporting_category': 'TPS1' if scope else '',
        'label_1': 'Prestation', 'qty_1': '1', 'price_1': '100', 'vat_1': '20'})
    inv_id = c.execute('SELECT id FROM outgoing_invoices ORDER BY id DESC LIMIT 1').fetchone()['id']
    c.execute("UPDATE outgoing_invoices SET status='sent', invoice_number='FA-2026-001' WHERE id=?", (inv_id,))
    c.commit()
    return inv_id


def test_la_transmission_d_une_vente_b2c_est_refusee(ctx):
    cl, csrf, c = ctx
    inv_id = make_invoice(cl, csrf, c, 'b2c')
    csrf()
    resp = cl.post(f'/facturation/{inv_id}/weinvoice', data={'csrf_token': CSRF}, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert 'ne se transmet pas par la plateforme agréée' in body
    assert c.execute('SELECT weinvoice_invoice_id FROM outgoing_invoices WHERE id=?',
                     (inv_id,)).fetchone()['weinvoice_invoice_id'] is None


def test_l_ecran_ne_propose_pas_de_transmettre_une_vente_b2c(ctx):
    cl, _csrf, c = ctx
    inv_id = make_invoice(cl, _csrf, c, 'b2c')
    body = cl.get(f'/facturation/{inv_id}').get_data(as_text=True)
    assert 'Transmettre la facture électronique' not in body
    assert 'e-reporting' in body


def test_une_vente_entre_entreprises_reste_transmissible(ctx):
    """Le contrôle ne doit pas déborder sur les factures qui, elles, se transmettent."""
    cl, _csrf, c = ctx
    inv_id = make_invoice(cl, _csrf, c, '')
    body = cl.get(f'/facturation/{inv_id}').get_data(as_text=True)
    assert 'Transmettre la facture électronique' in body
