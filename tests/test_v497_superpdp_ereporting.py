"""v497 — E-reporting par SUPER PDP : ventes aux particuliers et encaissements de prestations,
une déclaration par opération, jamais renvoyée une fois acceptée. Réseau simulé."""
from datetime import date, timedelta

import pytest

from profitos import einvoice_superpdp_ereporting as ESE
from test_v421_invoice_compliance import FULL_PROFILE, _flashes, _post, _signup, app  # noqa: F401
from test_v495_superpdp_client import Resp
from test_v495_superpdp_integration import _db, _no_boamp, connect, pdp  # noqa: F401


def b2c_invoice(client, org_id, category='TPS1', lines=((1000, 20),)):
    form = {'client_name': 'Mme Petitjean', 'client_email': 'p@example.com', 'client_address': '3 rue B\n69002 Lyon',
            'due_date': (date.today() + timedelta(days=30)).isoformat(), 'operation_nature': 'services',
            'ereporting_scope': 'b2c', 'ereporting_category': category}
    for i, (price, vat) in enumerate(lines, 1):
        form.update({f'label_{i}': f'Prestation {i}', f'qty_{i}': '1', f'price_{i}': str(price), f'vat_{i}': str(vat)})
    _post(client, '/facturation/nouvelle', form)
    c = _db(org_id)
    inv_id = c.execute('SELECT MAX(id) AS m FROM outgoing_invoices').fetchone()['m']
    c.close()
    _post(client, f'/facturation/{inv_id}/marquer-emise')
    _flashes(client)
    return inv_id


def records(org_id):
    c = _db(org_id)
    rows = c.execute('SELECT * FROM ereporting_records ORDER BY id').fetchall()
    c.close()
    return rows


@pytest.fixture
def linked(app, pdp):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    connect(client)
    _flashes(client)
    pdp.routes[('PATCH', '/v1.beta/companies')] = [Resp(200, {'vat_regime': 'monthly'})]
    n = {'t': 100, 'p': 500}

    def tx(req):
        n['t'] += 1
        return Resp(201, {'data': [{**req['json']['data'][0], 'id': n['t']}]})

    def pay(req):
        n['p'] += 1
        return Resp(201, {'data': [{**req['json']['data'][0], 'id': n['p']}]})
    pdp.routes[('POST', '/v1.beta/b2c_transactions')] = tx
    pdp.routes[('POST', '/v1.beta/b2c_payments')] = pay
    return client, org_id


def posts(pdp, path):
    return [c[2]['json'] for c in pdp.calls if c[:2] == ('POST', path)]


def declare(client):
    _post(client, '/facturation/e-reporting/declarer')
    return ' '.join(_flashes(client))


def test_vente_ventilee_par_taux_declaree_une_seule_fois(linked, pdp):
    client, org_id = linked
    inv = b2c_invoice(client, org_id, lines=((1000, 20), (200, 10)))
    assert 'Ventes aux particuliers (e-reporting)' in client.get('/facturation').get_data(as_text=True)
    assert '1 opération(s) déclarée(s)' in declare(client)
    sent = posts(pdp, '/v1.beta/b2c_transactions')[0]['data'][0]
    assert sent['role_code'] == 'SE' and sent['category_code'] == 'TPS1' and sent['currency'] == 'EUR'
    assert sent['date'] == date.today().isoformat()
    assert (sent['tax_exclusive_amount'], sent['tax_total']) == ('1200.00', '220.00')
    assert sent['tax_subtotals'] == [{'tax_percent': '10.00', 'taxable_amount': '200.00', 'tax_total': '20.00'},
                                     {'tax_percent': '20.00', 'taxable_amount': '1000.00', 'tax_total': '200.00'}]
    # le régime de TVA est indiqué à la plateforme (calendrier de transmission)
    patch = [c[2]['json'] for c in pdp.calls if c[:2] == ('PATCH', '/v1.beta/companies')][0]
    assert patch == {'vat_regime': 'monthly', 'has_vat_on_debits': False}
    r = records(org_id)[0]
    assert (r['status'], r['provider_reference'], r['source_id'], r['amount_ttc']) == ('submitted', '101', inv, 1420.0)
    assert '0 opération(s) déclarée(s)' in declare(client)          # second passage : rien n'est renvoyé
    assert len(posts(pdp, '/v1.beta/b2c_transactions')) == 1


def test_encaissement_d_une_prestation_declare(linked, pdp):
    client, org_id = linked
    inv = b2c_invoice(client, org_id)
    _post(client, f'/facturation/{inv}/reglement', {'amount': '600', 'payment_date': date.today().isoformat()})
    assert '2 opération(s) déclarée(s)' in declare(client)
    sent = posts(pdp, '/v1.beta/b2c_payments')[0]['data'][0]
    assert sent == {'date': date.today().isoformat(),
                    'subtotals': [{'category_code': 'TPS1', 'tax_percent': '20.00', 'amount': '600.00', 'currency_code': 'EUR'}]}
    assert [r['status'] for r in records(org_id) if r['record_type'] == 'payment'] == ['submitted']


def test_encaissement_de_biens_non_declare(linked, pdp):
    client, org_id = linked
    inv = b2c_invoice(client, org_id, category='TLB1')
    _post(client, f'/facturation/{inv}/reglement', {'amount': '1200', 'payment_date': date.today().isoformat()})
    declare(client)
    assert len(posts(pdp, '/v1.beta/b2c_transactions')) == 1 and not posts(pdp, '/v1.beta/b2c_payments')
    pay = [r for r in records(org_id) if r['record_type'] == 'payment'][0]
    assert pay['status'] == 'manual' and 'hors e-reporting de paiement' in pay['last_error']


def test_refus_de_la_plateforme_puis_nouvel_essai(linked, pdp):
    client, org_id = linked
    b2c_invoice(client, org_id)
    ok = pdp.routes[('POST', '/v1.beta/b2c_transactions')]
    pdp.routes[('POST', '/v1.beta/b2c_transactions')] = [Resp(422, {'message': 'category_code invalide'})]
    msg = declare(client)
    assert '0 opération(s) déclarée(s)' in msg and '1 refusée(s)' in msg
    assert records(org_id)[0]['status'] == 'error'
    assert 'category_code invalide' in client.get('/facturation').get_data(as_text=True)
    pdp.routes[('POST', '/v1.beta/b2c_transactions')] = ok
    assert '1 opération(s) déclarée(s)' in declare(client)
    assert records(org_id)[0]['status'] == 'submitted'


def test_reponse_perdue_jamais_renvoyee_sans_decision(linked, pdp):
    client, org_id = linked
    b2c_invoice(client, org_id)
    ok = pdp.routes[('POST', '/v1.beta/b2c_transactions')]
    pdp.routes[('POST', '/v1.beta/b2c_transactions')] = [Resp(502, {'message': 'bad gateway'})]
    assert '1 sans réponse' in declare(client)
    rec = records(org_id)[0]
    assert rec['status'] == 'unknown'
    pdp.routes[('POST', '/v1.beta/b2c_transactions')] = ok
    declare(client)                                              # pas de renvoi automatique
    assert records(org_id)[0]['status'] == 'unknown' and not [p for p in posts(pdp, '/v1.beta/b2c_transactions') if False]
    n = len(pdp.calls)
    page = client.get('/facturation').get_data(as_text=True)
    assert 'Déjà déclarée' in page and 'Renvoyer' in page
    _post(client, '/facturation/e-reporting/declarer', {'action': 'renvoyer', 'record_id': str(rec['id'])})
    assert records(org_id)[0]['status'] == 'submitted' and len(pdp.calls) > n


def test_regime_de_tva_transmis(linked, pdp):
    client, org_id = linked
    c = _db(org_id)
    assert ESE.vat_regime(c, None) == ('monthly', False)
    c.execute("UPDATE company SET vat_regime='franchise_293B' WHERE id=1")
    assert ESE.vat_regime(c, None) == ('vat_exemption', False)
    c.close()


def test_sans_raccordement_message_clair(app, pdp):
    client, org_id = _signup(app)
    _post(client, '/company', FULL_PROFILE)
    b2c_invoice(client, org_id)
    assert 'non reliée' in declare(client)
