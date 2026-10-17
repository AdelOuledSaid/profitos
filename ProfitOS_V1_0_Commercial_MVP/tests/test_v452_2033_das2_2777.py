"""v452 — Production réglementaire, lot 5 : annexes 2033-C à 2033-G et 2065 du régime
simplifié, DAS2 (honoraires versés), 2777 (prélèvements sur dividendes)."""
import re
import uuid
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, corporate_tax as CT, teledeclaration as T, automation as A
from profitos import tiers_declarations as TD
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry
from profitos.liasse_2033_annexes import annexes_2033, annexes_2033_rows

IDENTITY = {'name': 'Exemple SARL', 'siren': '732829320', 'siret': '73282932000074'}
CSRF = 'c' * 64


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    monkeypatch.delenv('SMTP_HOST', raising=False)
    monkeypatch.delenv('EDI_PARTNER_ENABLED', raising=False)
    monkeypatch.setenv('PROFITOS_ENV', 'development')
    a = create_app(DevelopmentConfig)
    a.config.update(TESTING=True)
    return a


def tenant(app):
    ac = rt.auth_cx()
    ac.execute("INSERT INTO organizations(id,name,plan,status,created_at,updated_at) VALUES(1,'Org','BUSINESS','ACTIVE_PAID',?,?)",
               (rt.now(), rt.now()))
    ac.commit(); ac.close()
    rt.migrate_tenant(1, force=True)
    return rt.tenant_cx_direct(1)


def signup(app):
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    r = cl.post('/signup', data={'csrf_token': CSRF, 'accept_terms': '1', 'full_name': 'Alice Martin',
                                 'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                                 'company_name': 'Exemple SARL'})
    assert r.status_code in (302, 303)
    with cl.session_transaction() as s:
        org_id, uid = s['org_id'], s['user_id']
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (org_id,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit(); ac.close()
    return cl, org_id


def post(cl, url, data):
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl.post(url, data=dict(data, csrf_token=CSRF))


def book(c):
    create_entry(c, 'BQ', '2025-01-05', 'Capital', [{'account_code': '512000', 'debit': 20000},
                                                     {'account_code': '101300', 'credit': 20000}])
    create_entry(c, 'VE', '2025-03-01', 'Vente', [{'account_code': '411000', 'debit': 120000, 'auxiliary_name': 'C'},
                                                  {'account_code': '706000', 'credit': 100000},
                                                  {'account_code': '445710', 'credit': 20000, 'vat_rate': 20.0, 'vat_base': 100000}])
    create_entry(c, 'AC', '2025-02-01', 'Matériel', [{'account_code': '218200', 'debit': 15000},
                                                     {'account_code': '445620', 'debit': 3000},
                                                     {'account_code': '404000', 'credit': 18000, 'auxiliary_name': 'Garage'}])
    create_entry(c, 'AC', '2025-04-02', 'Avocat', [{'account_code': '622600', 'debit': 2000},
                                                   {'account_code': '445660', 'debit': 400},
                                                   {'account_code': '401000', 'credit': 2400, 'auxiliary_name': 'Cabinet Dupont'}])
    create_entry(c, 'AC', '2025-09-02', 'Avocat', [{'account_code': '622700', 'debit': 500},
                                                   {'account_code': '445660', 'debit': 100},
                                                   {'account_code': '401000', 'credit': 600, 'auxiliary_name': 'Cabinet Dupont'}])
    create_entry(c, 'AC', '2025-05-02', 'Conseil', [{'account_code': '622600', 'debit': 900},
                                                    {'account_code': '445660', 'debit': 180},
                                                    {'account_code': '401000', 'credit': 1080, 'auxiliary_name': 'Petit Conseil'}])
    create_entry(c, 'OD', '2025-12-31', 'Dotation', [{'account_code': '681000', 'debit': 2750},
                                                     {'account_code': '281820', 'credit': 2750}])


def test_2033_annexes_from_ledger(app):
    c = tenant(app)
    book(c)
    ax = annexes_2033(c, date(2025, 1, 1), date(2025, 12, 31), None,
                      profile={'shareholders': [{'kind': 'PP', 'name': 'Martin', 'pct': 100, 'shares': 1000}],
                               'subsidiaries': [{'name': 'Filiale', 'pct': 30}]},
                      settings={'deficits_anterieurs': 10000})
    c.close()
    transport = next(r for r in ax['immo'] if r['label'] == 'Matériel de transport')
    assert (transport['debut'], transport['augmentations'], transport['fin']) == (0, 15000, 15000)
    assert next(r for r in ax['amort'] if r['label'] == 'Matériel de transport')['dotations'] == 2750
    assert ax['immo_tot']['fin'] == 15000 and ax['amort_tot']['fin'] == 2750
    rf = ax['resultat_fiscal']
    resultat = 100000 - 2000 - 500 - 900 - 2750
    assert rf['resultat_comptable'] == resultat and rf['deficits_imputes'] == 10000 and rf['benefice'] == resultat - 10000
    d = {x['code']: x['amount'] for x in ax['deficits']}
    assert d == {'982': 10000, '983': 10000, '984': 0, '860': 0, '870': 0}
    assert ax['capital']['nb_pp'] == 1 and ax['filiales']['count'] == 1 and not ax['checks']
    rows = annexes_2033_rows(ax)
    assert {r['Tableau'] for r in rows} >= {'2033-B', '2033-C', '2033-D', '2033-E', '2033-F', '2033-G'}
    assert any(r['Case'] == '370' and r['Montant'] == resultat - 10000 for r in rows)


def test_2033_teledeclaration_carries_annexes_and_2065(app):
    c = tenant(app)
    book(c)
    built = T.build_declaration(c, 'LIASSE', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY, regime='simplifie')
    c.close()
    labels = ' '.join(b['label'] for b in built['payload']['boxes'])
    assert '2033-C' in labels and '2033-D' in labels and '2065' in labels
    assert built['payload']['annex_lists']['2065']['regime'] == "Régime simplifié d'imposition"
    assert any('Signataire' in w for w in built['warnings'])


def test_das2_threshold_ttc_and_overrides(app):
    c = tenant(app)
    book(c)
    d = TD.das2(c, 2025)
    rows = {r['name']: r for r in d['rows']}
    # 2 400 + 600 TTC (honoraires + frais d'actes) : au-dessus du seuil ; 1 080 € : en dessous.
    assert rows['Cabinet Dupont']['amount'] == 3000 and rows['Cabinet Dupont']['include']
    assert rows['Cabinet Dupont']['categories'] == {'HON': 3000}
    assert rows['Petit Conseil']['amount'] == 1080 and not rows['Petit Conseil']['include']
    assert d['issues'] and d['deadline'] == date(2026, 5, 5)
    built = T.build_declaration(c, 'DAS2', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    assert any('SIRET' in e for e in built['errors'])
    TD.save_das2(c, None, 2025, {'cabinet dupont': {'include': True, 'siret': '73282932000074', 'address': '1 rue X 75001 Paris',
                                                    'category': 'HON', 'amount': 2900}}, rt.now())
    built = T.build_declaration(c, 'DAS2', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    assert not built['errors']
    assert [(b['code'], b['amount']) for b in built['payload']['boxes']] == [('HON', 2900)]
    assert built['payload']['annex_lists']['DAS2'][0]['siret'] == '73282932000074'
    bad = T.build_declaration(c, 'DAS2', date(2025, 1, 1), date(2025, 6, 30), identity=IDENTITY)
    c.close()
    assert any('année civile' in e for e in bad['errors'])


def test_das2_supplier_card_prefill(app):
    c = tenant(app)
    book(c)
    c.execute("INSERT INTO suppliers(name,address,siret,created_at) VALUES('Cabinet Dupont','2 place du Palais','73282932000074',?)", (rt.now(),))
    c.commit()
    d = TD.das2(c, 2025)
    c.close()
    assert d['included'][0]['siret'] == '73282932000074' and not d['issues']


def test_2777_rates_split_and_ledger_check(app):
    c = tenant(app)
    create_entry(c, 'OD', '2026-06-10', 'Dividendes', [{'account_code': '110000', 'debit': 10000},
                                                       {'account_code': '457000', 'credit': 10000}])
    create_entry(c, 'BQ', '2026-06-30', 'Paiement', [{'account_code': '457000', 'debit': 10000},
                                                     {'account_code': '512000', 'credit': 6860},
                                                     {'account_code': '442000', 'credit': 3140}])
    r = TD.rcm_2777(c, date(2026, 6, 1), None, {})
    assert (r['paid'], r['pfnl'], r['ps'], r['total'], r['deadline']) == (10000, 1280, 1860, 3140, date(2026, 7, 15))
    assert r['ps_rate'] == 18.6 and r['withheld'] == 3140 and len(r['warnings']) == 1  # capital non renseigné
    profile = {'shareholders': [{'kind': 'PP', 'name': 'A', 'pct': 60}, {'kind': 'PM', 'name': 'Holding', 'pct': 40}]}
    TD.save_store(c, None, 'rcm-2026-06', {'dispensed': 2000}, rt.now())
    r = TD.rcm_2777(c, date(2026, 6, 1), None, profile)
    assert r['gross_pp'] == 6000 and r['pfnl'] == 512 and r['ps'] == 1116
    assert any('compte 442' in w for w in r['warnings'])
    assert TD.social_levies_rate(date(2025, 12, 1)) == 17.2
    assert TD.dividend_months(c) == [date(2026, 6, 1)]
    c.close()


def test_deadlines_include_das2_and_2777(app):
    c = tenant(app)
    book(c)
    TD.save_das2(c, None, 2025, {'cabinet dupont': {'include': True, 'siret': '73282932000074', 'address': 'x'}}, rt.now())
    items = A.upcoming_deadlines(c, None, date(2026, 4, 20), horizon_days=30)
    assert any(x['type'] == 'DAS2' and x['date'] == date(2026, 5, 5) for x in items)
    create_entry(c, 'BQ', '2026-04-10', 'Paiement dividendes', [{'account_code': '457000', 'debit': 1000},
                                                                {'account_code': '512000', 'credit': 1000}])
    items = A.upcoming_deadlines(c, None, date(2026, 4, 20), horizon_days=30)
    c.close()
    rcm = [x for x in items if x['form'] == '2777']
    assert rcm and rcm[0]['date'] == date(2026, 5, 15) and rcm[0]['amount'] == 314


def test_pages_render_and_save(app):
    cl, oid = signup(app)
    c = rt.tenant_cx_direct(oid)
    book(c)
    create_entry(c, 'BQ', '2026-06-30', 'Paiement', [{'account_code': '457000', 'debit': 5000},
                                                     {'account_code': '512000', 'credit': 3430},
                                                     {'account_code': '442000', 'credit': 1570}])
    c.close()
    page = cl.get('/comptabilite/liasse-2033?exercice=precedent').get_data(as_text=True)
    assert '2033-C' in page and '2033-D' in page and '2033-F' in page and '2065' in page and '>370<' in page
    csv = cl.get('/comptabilite/liasse-2033?exercice=precedent&format=csv').get_data(as_text=True)
    assert '2033-D;982;' in csv
    page = cl.get('/comptabilite/das2?annee=2025').get_data(as_text=True)
    assert 'Cabinet Dupont' in page and 'Petit Conseil' in page
    r = post(cl, '/comptabilite/das2', {'annee': '2025', 'key_1': 'cabinet dupont', 'include_1': '1', 'category_1': 'HON',
                                        'amount_1': '3 000,00', 'siret_1': '732 829 320 00074', 'address_1': '1 rue X'})
    assert r.status_code == 302
    c = rt.tenant_cx_direct(oid)
    d = TD.das2(c, 2025)
    c.close()
    assert d['included'][0]['siret'] == '73282932000074' and not d['issues']
    assert post(cl, '/comptabilite/das2', {'annee': '2025', 'key_1': 'x', 'amount_1': 'abc'}).status_code == 302
    page = cl.get('/comptabilite/dividendes-2777?mois=2026-06').get_data(as_text=True)
    assert re.search(r'1\D{1,8}570', page) and '18,6' in page
    post(cl, '/comptabilite/dividendes-2777', {'mois': '2026-06', 'part_pp': '150'})
    c = rt.tenant_cx_direct(oid)
    assert TD.load_store(c, None, 'rcm-2026-06') == {}
    c.close()
    assert cl.get('/comptabilite/dividendes-2777?mois=n-importe').status_code == 200
    assert cl.get('/comptabilite/das2?annee=1900').status_code == 200
