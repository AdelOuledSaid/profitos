"""v438 — Comptabilité complète, lot 2 : CA3 case par case et liquidation de la TVA,
liasse 2033-A / 2033-B pré-remplie, amortissement dégressif."""
from datetime import date

import pytest

from support_v4xx import app, flashes, post, signup, tenant  # noqa: F401
from profitos.accounting import (AccountingError, create_entry, degressive_coefficient,
                                 degressive_schedule, compute_depreciation_for_year)
from profitos.vat_return import ca3_form, generate_liquidation_entry
from profitos.liasse import liasse_2033


def _sale(c, day, ht, vat, rate=20.0):
    create_entry(c, 'VE', day, 'Vente', [
        {'account_code': '411000', 'debit': ht + vat, 'auxiliary_name': 'Client'},
        {'account_code': '706000', 'credit': ht},
        {'account_code': '445710', 'credit': vat, 'vat_rate': rate, 'vat_base': ht}])


def _purchase(c, day, ht, vat, vat_account='445660', charge='606400'):
    create_entry(c, 'AC', day, 'Achat', [
        {'account_code': charge, 'debit': ht},
        {'account_code': vat_account, 'debit': vat},
        {'account_code': '401000', 'credit': ht + vat, 'auxiliary_name': 'Fournisseur'}])


# ------------------------------------------------------------------ CA3
def test_ca3_boxes_and_liquidation_due(app):
    _, _, org, _ = signup(app, 'Tva SAS')
    c = tenant(org)
    _sale(c, '2026-03-05', 1000, 200)
    _sale(c, '2026-03-20', 500, 50, rate=10.0)
    _purchase(c, '2026-03-10', 300, 60)
    _purchase(c, '2026-03-12', 1000, 200, vat_account='445620', charge='218300')
    f = ca3_form(c, '2026-03-01', '2026-03-31')
    assert f['l01'] == 1500 and f['l16'] == 250
    assert {r['line']: r['vat'] for r in f['rates']} == {'08': 200, '9B': 50}
    assert (f['l19'], f['l20'], f['l22'], f['l23']) == (200, 60, 0, 260)
    assert f['l25'] == 10 and f['l28'] == 0  # crédit de 10 €
    eid = generate_liquidation_entry(c, '2026-03-01', '2026-03-31')
    lines = {l['account_code']: (l['debit'], l['credit']) for l in
             c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (eid,)).fetchall()}
    assert lines == {'445710': (250.0, 0.0), '445620': (0.0, 200.0), '445660': (0.0, 60.0), '445670': (10.0, 0.0)}
    with pytest.raises(AccountingError, match='déjà été liquidée'):
        generate_liquidation_entry(c, '2026-03-01', '2026-03-31')
    # La déclaration reste lisible après liquidation, et le crédit est reporté en avril.
    assert ca3_form(c, '2026-03-01', '2026-03-31')['l16'] == 250
    _sale(c, '2026-04-02', 2000, 400)
    f4 = ca3_form(c, '2026-04-01', '2026-04-30')
    assert f4['l22'] == 10 and f4['l28'] == 390
    eid4 = generate_liquidation_entry(c, '2026-04-01', '2026-04-30')
    l4 = {l['account_code']: (l['debit'], l['credit']) for l in
          c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (eid4,)).fetchall()}
    assert l4 == {'445710': (400.0, 0.0), '445670': (0.0, 10.0), '445510': (0.0, 390.0)}
    c.close()


def test_ca3_page_and_liquidation_route(app):
    client, _, org, _ = signup(app, 'Tva2 SAS')
    c = tenant(org)
    _sale(c, '2026-02-10', 100, 20)
    c.close()
    html = client.get('/comptabilite/tva/ca3?date_from=2026-02-01&date_to=2026-02-28').get_data(as_text=True)
    assert 'Déclaration CA3' in html and 'TVA nette due' in html and 'Générer' in html
    r = post(client, '/comptabilite/tva/ca3/liquider', {'date_from': '2026-02-01', 'date_to': '2026-02-28'})
    assert r.status_code == 302 and '/comptabilite/ecritures/' in r.headers['Location']
    html = client.get('/comptabilite/tva/ca3?date_from=2026-02-01&date_to=2026-02-28').get_data(as_text=True)
    assert 'déjà liquidée' in html
    summary = client.get('/comptabilite/tva?date_from=2026-02-01&date_to=2026-02-28').get_data(as_text=True)
    assert 'Déclaration CA3 case par case' in summary


def test_nothing_to_liquidate(app):
    _, _, org, _ = signup(app, 'Tva3 SAS')
    c = tenant(org)
    with pytest.raises(AccountingError, match='Aucune TVA'):
        generate_liquidation_entry(c, '2026-01-01', '2026-01-31')
    c.close()


# ------------------------------------------------------------------ liasse 2033
def test_liasse_2033_balanced_and_mapped(app):
    _, _, org, _ = signup(app, 'Liasse SAS')
    c = tenant(org)
    create_entry(c, 'BQ', '2026-01-05', 'Capital', [{'account_code': '512000', 'debit': 10000},
                                                      {'account_code': '101300', 'credit': 10000}])
    create_entry(c, 'OD', '2026-01-06', 'Emprunt', [{'account_code': '512000', 'debit': 5000},
                                                     {'account_code': '164000', 'credit': 5000}])
    _sale(c, '2026-02-01', 8000, 1600)
    _purchase(c, '2026-02-03', 2000, 400)
    _purchase(c, '2026-02-04', 3000, 600, vat_account='445620', charge='218300')
    create_entry(c, 'OD', '2026-12-31', 'Dotation', [{'account_code': '681100', 'debit': 500},
                                                      {'account_code': '281830', 'credit': 500}])
    create_entry(c, 'OD', '2026-12-31', 'Salaire', [{'account_code': '641000', 'debit': 1000},
                                                     {'account_code': '421000', 'credit': 1000}])
    l = liasse_2033(c, date(2026, 1, 1), date(2026, 12, 31))
    c.close()
    a = {x['box_brut']: x for x in l['actif']}
    p = {x['box']: x['amount'] for x in l['passif']}
    cr = {x['box']: x['amount'] for x in l['produits'] + l['charges']}
    assert a['028']['brut'] == 3000 and a['028']['amort'] == 500 and a['028']['net'] == 2500
    assert a['068']['brut'] == 9600 and a['084']['brut'] == 15000
    assert a['072']['brut'] == 1000  # TVA déductible (44566 + 44562)
    assert p['120'] == 10000 and p['156'] == 5000 and p['166'] == 2400 + 3600
    assert p['172'] == 1600 + 1000  # TVA collectée + salaire à payer
    assert cr['218'] == 8000 and cr['242'] == 2000 and cr['250'] == 1000 and cr['254'] == 500
    assert l['resultat'] == 8000 - 2000 - 1000 - 500 == p['136']
    assert l['balanced'] and l['total_actif'] == l['total_passif']


def test_liasse_page_renders_and_exports(app):
    client, _, _, _ = signup(app, 'Liasse2 SAS')
    assert client.get('/comptabilite/liasse-2033').status_code == 200
    html = client.get('/comptabilite/liasse-2033?exercice=courant').get_data(as_text=True)
    assert '2033-B' in html and 'situation provisoire' in html
    x = client.get('/comptabilite/liasse-2033?format=xlsx')
    assert x.status_code == 200 and x.data[:2] == b'PK'


# ------------------------------------------------------------------ dégressif
def test_degressive_coefficients_and_textbook_plan():
    assert [degressive_coefficient(n) for n in (2, 3, 4, 5, 6, 7, 10)] == [None, 1.25, 1.25, 1.75, 1.75, 2.25, 2.25]
    plan = degressive_schedule(10000, 5, '2026-07-01')  # taux 35 %, 6 mois la 1re année
    assert plan == {2026: 1750.0, 2027: 2887.5, 2028: 1876.88, 2029: 1742.81, 2030: 1742.81}
    assert round(sum(plan.values()), 2) == 10000
    with pytest.raises(ValueError):
        degressive_schedule(1000, 2, '2026-01-01')


def test_degressive_asset_creation_and_depreciation(app):
    client, _, org, _ = signup(app, 'Degressif SAS')
    r = post(client, '/comptabilite/immobilisations', {'label': 'Machine', 'purchase_date': '2026-07-01',
             'purchase_amount': '10000', 'useful_life_years': '5', 'method': 'degressive',
             'asset_account': '218300', 'depreciation_account': '281830', 'expense_account': '681100'})
    assert r.status_code == 302
    c = tenant(org)
    asset = c.execute("SELECT * FROM fixed_assets WHERE label='Machine'").fetchone()
    assert asset['method'] == 'degressive'
    assert compute_depreciation_for_year(c, asset, 2026) == 1750.0
    c.close()
    html = client.get(r.headers['Location']).get_data(as_text=True)
    assert 'Plan d' in html and '2 887,50' in html and 'coefficient 1,75' in html
    post(client, '/comptabilite/immobilisations', {'label': 'Trop court', 'purchase_date': '2026-07-01',
         'purchase_amount': '1000', 'useful_life_years': '2', 'method': 'degressive',
         'asset_account': '218300', 'depreciation_account': '281830', 'expense_account': '681100'})
    c = tenant(org)
    assert c.execute("SELECT 1 FROM fixed_assets WHERE label='Trop court'").fetchone() is None
    c.close()
