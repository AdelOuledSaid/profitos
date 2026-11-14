"""v503 — L'adresse d'acheminement se corrige après émission.

Une facture émise n'est plus modifiable : son contenu est figé, numéro et écriture compris.
Mais l'identifiant d'annuaire n'est pas une mention de la facture, c'est l'adresse à laquelle
on la remet. Le figer avec le reste obligeait, dès qu'un dépôt échouait pour adresse inconnue
(« receiver address <0225:000000002> does not accept this document »), à annuler la facture et
à la refaire, alors que rien dans la facture n'était en cause.
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
    cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'first_name': 'Alice',
                             'last_name': 'Martin', 'email': f'{uuid.uuid4().hex[:10]}@example.com',
                             'password': 'Brume-Cactus-Velo-48', 'company_name': 'Atelier Martin'})
    with cl.session_transaction() as s:
        org = s['org_id']
        uid = s['user_id']
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='BUSINESS',status='ACTIVE_PAID' WHERE id=?", (org,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit()
    ac.close()
    csrf()
    cl.post('/company', data={'csrf_token': CSRF, 'name': 'Atelier Martin', 'legal_form': 'SAS',
                              'siret': '73282932000074', 'vat_number': 'FR44732829320',
                              'address': '1 rue Test', 'postal_code': '75001', 'city': 'Paris',
                              'share_capital': '1000', 'rcs_city': 'Paris',
                              'payment_terms': '30 jours', 'late_penalty_rate': '10'})
    csrf()
    cl.post('/facturation/nouvelle', data={
        'csrf_token': CSRF, 'client_name': 'Burger Queen',
        'client_address': '809 avenue du Languedoc\n12100 Millau', 'client_siren': '732829320',
        'due_date': '2027-01-01', 'operation_nature': 'services', 'vat_treatment': 'standard',
        'label_1': 'Prestation', 'qty_1': '1', 'price_1': '1000', 'vat_1': '20'})
    import profitos.db as dbmod
    c = dbmod.connect_tenant(org, rt.tenant_db(org))
    inv_id = c.execute('SELECT id FROM outgoing_invoices ORDER BY id DESC LIMIT 1').fetchone()['id']
    c.execute("UPDATE outgoing_invoices SET status='sent' WHERE id=?", (inv_id,))   # facture émise
    c.commit()
    yield cl, csrf, c, inv_id
    c.close()


def address_of(c, inv_id):
    return c.execute('SELECT client_electronic_address FROM outgoing_invoices WHERE id=?',
                     (inv_id,)).fetchone()['client_electronic_address']


def test_l_adresse_se_corrige_sur_une_facture_emise(ctx):
    cl, csrf, c, inv_id = ctx
    csrf()
    resp = cl.post(f'/facturation/{inv_id}/annuaire',
                   data={'csrf_token': CSRF, 'client_electronic_address': '0225:315143296_121168'})
    assert resp.status_code == 302
    assert address_of(c, inv_id) == '0225:315143296_121168'


def test_l_adresse_peut_etre_effacee(ctx):
    cl, csrf, c, inv_id = ctx
    csrf()
    cl.post(f'/facturation/{inv_id}/annuaire',
            data={'csrf_token': CSRF, 'client_electronic_address': '0225:315143296_121168'})
    csrf()
    cl.post(f'/facturation/{inv_id}/annuaire', data={'csrf_token': CSRF, 'client_electronic_address': ''})
    assert address_of(c, inv_id) is None      # le SIREN du client reprend son rôle


def test_une_facture_deja_deposee_garde_son_adresse(ctx):
    """Le document est parti : changer l'adresse n'aurait plus de sens."""
    cl, csrf, c, inv_id = ctx
    c.execute("UPDATE outgoing_invoices SET weinvoice_invoice_id='42', client_electronic_address='0225:aaa' "
              'WHERE id=?', (inv_id,))
    c.commit()
    csrf()
    cl.post(f'/facturation/{inv_id}/annuaire',
            data={'csrf_token': CSRF, 'client_electronic_address': '0225:bbb'})
    assert address_of(c, inv_id) == '0225:aaa'


def test_une_facture_annulee_n_est_pas_readressable(ctx):
    cl, csrf, c, inv_id = ctx
    c.execute("UPDATE outgoing_invoices SET status='cancelled' WHERE id=?", (inv_id,))
    c.commit()
    csrf()
    cl.post(f'/facturation/{inv_id}/annuaire',
            data={'csrf_token': CSRF, 'client_electronic_address': '0225:bbb'})
    assert address_of(c, inv_id) is None


def test_la_valeur_saisie_est_assainie(ctx):
    """Seuls les caractères d'un identifiant d'annuaire sont conservés."""
    cl, csrf, c, inv_id = ctx
    csrf()
    cl.post(f'/facturation/{inv_id}/annuaire',
            data={'csrf_token': CSRF, 'client_electronic_address': ' 0225:315<script>_121168 '})
    assert address_of(c, inv_id) == '0225:315script_121168'
