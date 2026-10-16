"""v453 — Production réglementaire, lot 6 : CFE (échéances, écriture, déclarations 1447),
DECLOYER (loyers des locaux professionnels avec la liasse), IFU 2561 (dividendes de
l'année)."""
import uuid
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, corporate_tax as CT, teledeclaration as T, automation as A
from profitos import tiers_declarations as TD, local_taxes as LT
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry, AccountingError

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


PROFILE = {'shareholders': [{'kind': 'PP', 'name': 'Martin', 'first_name': 'Paul', 'pct': 70, 'address': '1 rue A',
                             'birth_date': '1980-01-01'},
                            {'kind': 'PM', 'name': 'Holding', 'pct': 30, 'address': '2 rue B'}]}


def test_cfe_schedule_rules():
    s = LT.cfe_schedule(2026, {'montant': 4200, 'montant_n1': 3600})
    assert [(i['date'], i['amount']) for i in s['schedule']] == [(date(2026, 6, 15), 1800), (date(2026, 12, 15), 2400)]
    s = LT.cfe_schedule(2026, {'montant': 1000, 'montant_n1': 2999})
    assert [(i['date'], i['amount']) for i in s['schedule']] == [(date(2026, 12, 15), 1000)]
    s = LT.cfe_schedule(2026, {'montant': 5000, 'montant_n1': 4000, 'mensualise': True})
    assert [i['date'] for i in s['schedule']] == [date(2026, 12, 15)] and any('mensualisé' in n for n in s['notes'])
    s = LT.cfe_schedule(2026, {}, creation_date='2026-02-01')
    assert s['exempt'] and [i['form'] for i in s['schedule']] == ['1447-C']
    s = LT.cfe_schedule(2026, {'montant': 300}, creation_date='2025-05-01')
    assert any('moitié' in n for n in s['notes'])
    s = LT.cfe_schedule(2026, {'montant': 300, 'changements': True})
    assert any(i['form'] == '1447-M' and i['date'] == date(2027, 5, 4) for i in s['schedule'])


def test_cfe_entry_once(app):
    c = tenant(app)
    entry = LT.generate_cfe_entry(c, 2026, 1234.5, '2026-12-15')
    lines = {l['account_code']: (l['debit'], l['credit'])
             for l in c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (entry,)).fetchall()}
    assert lines == {'635110': (1234.5, 0), '447000': (0, 1234.5)}
    with pytest.raises(AccountingError):
        LT.generate_cfe_entry(c, 2026, 10, '2026-12-15')
    with pytest.raises(AccountingError):
        LT.generate_cfe_entry(c, 2027, 0, '2027-12-15')
    c.close()


def test_decloyer_in_liasse_both_regimes(app):
    c = tenant(app)
    create_entry(c, 'BQ', '2025-01-05', 'Capital', [{'account_code': '512000', 'debit': 20000},
                                                     {'account_code': '101300', 'credit': 20000}])
    create_entry(c, 'AC', '2025-03-01', 'Loyer', [{'account_code': '613200', 'debit': 12000},
                                                  {'account_code': '401000', 'credit': 12000, 'auxiliary_name': 'SCI'}])
    built = T.build_declaration(c, 'LIASSE', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    assert any('DECLOYER' in w for w in built['warnings'])
    CT.save_fiscal_profile(c, None, {'premises': [{'invariant': '0751234567A', 'address': '10 rue X', 'rent': 12000}]}, rt.now())
    for regime in ('normal', 'simplifie'):
        built = T.build_declaration(c, 'LIASSE', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY, regime=regime)
        dec = [b for b in built['payload']['boxes'] if b['label'].startswith('DECLOYER')]
        assert [(b['code'], b['amount']) for b in dec] == [('0751234567A', 12000)]
        assert built['payload']['annex_lists']['DECLOYER'][0]['rent'] == 12000
        assert not any('DECLOYER' in w for w in built['warnings'])
    d = LT.decloyer({'premises': [{'address': '10 rue X', 'rent': 2000}]}, 12000)
    c.close()
    assert len(d['warnings']) == 2  # identifiant manquant et loyer très différent


def test_ifu_allocation_and_boxes(app):
    c = tenant(app)
    create_entry(c, 'BQ', '2025-06-30', 'Dividendes', [{'account_code': '457000', 'debit': 10000},
                                                       {'account_code': '512000', 'credit': 6860},
                                                       {'account_code': '442000', 'credit': 3140}])
    create_entry(c, 'BQ', '2025-09-30', 'Dividende nominatif', [{'account_code': '457000', 'debit': 1000, 'auxiliary_name': 'Paul Martin'},
                                                                {'account_code': '512000', 'credit': 1000}])
    ifu = TD.ifu_2561(c, 2025, None, PROFILE)
    rows = {r['name']: r for r in ifu['rows']}
    paul, holding = rows['Paul Martin'], rows['Holding']
    assert paul['gross'] == 8000 and holding['gross'] == 3000
    assert (paul['box_2DC'], paul['box_2CK'], paul['box_2BH']) == (8000, 1024, 8000)
    assert paul['ps'] == 1376  # 17,2 % en 2025
    assert (holding['box_2DC'], holding['box_2CK']) == (0, 0) and not ifu['issues']
    assert ifu['deadline'] == date(2026, 2, 15)
    TD.save_store(c, None, 'ifu-2025', {'dispensed': ['paul martin']}, rt.now())
    assert {r['name']: r for r in TD.ifu_2561(c, 2025, None, PROFILE)['rows']}['Paul Martin']['box_2CK'] == 0
    bare = TD.ifu_2561(c, 2025, None, {})
    c.close()
    assert bare['unallocated'] == 11000 and bare['issues']


def test_deadlines_cfe_and_ifu(app):
    c = tenant(app)
    LT.save_cfe(c, None, 2026, {'montant': 4200, 'montant_n1': 3600}, rt.now())
    create_entry(c, 'BQ', '2025-06-30', 'Dividendes', [{'account_code': '457000', 'debit': 1000},
                                                       {'account_code': '512000', 'credit': 1000}])
    CT.save_fiscal_profile(c, None, PROFILE, rt.now())
    items = A.upcoming_deadlines(c, None, date(2026, 2, 1), horizon_days=150)
    c.close()
    forms = {(x['form'], x['date']) for x in items}
    assert ('IFU 2561', date(2026, 2, 15)) in forms and ('CFE', date(2026, 6, 15)) in forms


def test_pages(app):
    cl, oid = signup(app)
    r = post(cl, '/comptabilite/profil-fiscal', {'tax_regime': 'normal', 'vat_filing': 'ca3_mensuelle', 'date_creation': '2024-03-01',
                                                 'loc_invariant_1': '0751234567A', 'loc_address_1': '10 rue X', 'loc_rent_1': '12 000'})
    assert r.status_code == 302
    c = rt.tenant_cx_direct(oid)
    p = CT.get_fiscal_profile(c, None)
    c.close()
    assert p['premises'] == [{'invariant': '0751234567A', 'address': '10 rue X', 'rent': 12000.0}] and p['date_creation'] == '2024-03-01'
    post(cl, '/comptabilite/profil-fiscal', {'date_creation': 'pas une date', 'loc_address_1': 'x', 'loc_rent_1': '-5'})
    c = rt.tenant_cx_direct(oid)
    assert CT.get_fiscal_profile(c, None)['date_creation'] == '2024-03-01'
    c.close()
    assert '0751234567A' in cl.get('/comptabilite/liasse-2050?exercice=precedent').get_data(as_text=True)
    assert '0751234567A' in cl.get('/comptabilite/liasse-2033?exercice=precedent').get_data(as_text=True)
    assert cl.get('/comptabilite/cfe?annee=2024').status_code == 200
    post(cl, '/comptabilite/cfe', {'annee': '2026', 'action': 'save', 'montant': '4 200', 'montant_n1': '3 600'})
    page = cl.get('/comptabilite/cfe?annee=2026').get_data(as_text=True)
    assert '15/06/2026' in page and '15/12/2026' in page
    r = post(cl, '/comptabilite/cfe', {'annee': '2026', 'action': 'entry', 'entry_date': '2026-12-15'})
    assert '/comptabilite/ecritures/' in r.headers['Location']
    post(cl, '/comptabilite/cfe', {'annee': '2026', 'action': 'entry', 'entry_date': '2026-12-15'})
    c = rt.tenant_cx_direct(oid)
    assert c.execute("SELECT COUNT(*) n FROM accounting_entries WHERE source_type='cfe'").fetchone()['n'] == 1
    c.close()
    assert cl.get('/comptabilite/ifu?annee=2025').status_code == 200
    assert cl.get('/comptabilite/ifu?annee=abc').status_code == 200
    assert cl.get('/comptabilite/cfe?annee=1800').status_code == 200
