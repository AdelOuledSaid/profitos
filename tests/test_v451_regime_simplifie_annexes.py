"""v451 — Production réglementaire, lot 4 : TVA annuelle CA12 et acomptes 3514, tableaux
2054 à 2056 codés, 2059-A à 2059-G, page de garde 2065, profil fiscal et paiement en
ligne (EDI-Paiement avec mandat), cessions d'immobilisations."""
import json
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, corporate_tax as CT, teledeclaration as T, automation as A
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry, dispose_fixed_asset
from profitos.vat_return import ca12_form
from profitos.liasse_annexes import annexes, annexes_rows, form_2065
from profitos.liasse_normal import liasse_normale

IDENTITY = {'name': 'Exemple SARL', 'siren': '732829320', 'siret': '73282932000074', 'address': '1 rue Test',
            'postal_code': '75001', 'city': 'Paris'}
IBAN = 'FR7630006000011234567890189'


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


def add_org(org_id, plan='BUSINESS', role='OWNER'):
    ac = rt.auth_cx()
    ac.execute("INSERT INTO organizations(id,name,plan,status,created_at,updated_at) VALUES(?,?,?,'ACTIVE_PAID',?,?)",
               (org_id, f'Org {org_id}', plan, rt.now(), rt.now()))
    ac.execute('INSERT INTO users(id,email,password_hash,created_at,email_verified) VALUES(?,?,?,?,1)',
               (org_id, f'owner{org_id}@exemple.fr', 'x', rt.now()))
    ac.execute('INSERT INTO memberships(user_id,organization_id,role,created_at) VALUES(?,?,?,?)', (org_id, org_id, role, rt.now()))
    ac.commit(); ac.close()
    rt.migrate_tenant(org_id, force=True)


def book_2025(c):
    create_entry(c, 'BQ', '2025-01-05', 'Capital', [{'account_code': '512000', 'debit': 20000},
                                                     {'account_code': '101300', 'credit': 20000}])
    create_entry(c, 'VE', '2025-03-01', 'Vente', [{'account_code': '411000', 'debit': 120000, 'auxiliary_name': 'C'},
                                                  {'account_code': '706000', 'credit': 100000},
                                                  {'account_code': '445710', 'credit': 20000, 'vat_rate': 20.0, 'vat_base': 100000}])
    create_entry(c, 'AC', '2025-03-02', 'Achat', [{'account_code': '606400', 'debit': 30000},
                                                  {'account_code': '445660', 'debit': 6000},
                                                  {'account_code': '401000', 'credit': 36000, 'auxiliary_name': 'F'}])
    create_entry(c, 'AC', '2025-02-01', 'Matériel', [{'account_code': '218300', 'debit': 3000},
                                                     {'account_code': '445620', 'debit': 600},
                                                     {'account_code': '404000', 'credit': 3600, 'auxiliary_name': 'F'}])


CSRF = 'c' * 64


def signup(app, role='OWNER'):
    """Compte réel (inscription), formule payante, mode expert ; renvoie (client, org_id)."""
    import uuid
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
    ac.execute('UPDATE memberships SET role=? WHERE organization_id=?', (role, org_id))
    ac.commit(); ac.close()
    return cl, org_id


def post(cl, url, data):
    with cl.session_transaction() as s:
        s['csrf_token'] = CSRF
    return cl.post(url, data=dict(data, csrf_token=CSRF))


def test_ca12_form_lines_and_instalments(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    book_2025(c)
    f = ca12_form(c, '2025-01-01', '2025-12-31')
    c.close()
    assert [(x['line'], x['base_code'], x['base'], x['vat']) for x in f['lines']] == [('5A', '0207', 100000, 20000)]
    assert (f['l16'], f['l20'], f['l23'], f['l26'], f['l28'], f['l33'], f['l57']) == (20000, 6000, 600, 6600, 13400, 13400, 14000)
    assert f['instalments'] == [{'date': date(2026, 7, 15), 'label': 'Acompte de juillet (55 %)', 'amount': 7700},
                                {'date': date(2026, 12, 15), 'label': 'Acompte de décembre (40 %)', 'amount': 5600}]
    assert f['deadline'] == date(2026, 5, 5)


def test_ca12_acomptes_override_and_small_base(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    create_entry(c, 'VE', '2025-03-01', 'Vente', [{'account_code': '411000', 'debit': 1200, 'auxiliary_name': 'C'},
                                                  {'account_code': '706000', 'credit': 1000},
                                                  {'account_code': '445710', 'credit': 200, 'vat_rate': 20.0, 'vat_base': 1000}])
    f = ca12_form(c, '2025-01-01', '2025-12-31', acomptes=150)
    c.close()
    assert f['l30'] == 150 and f['l33'] == 50 and f['instalments'] == []  # base < 1 000 € : dispense


def test_teledeclaration_ca12_and_3514_with_mandate(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    book_2025(c)
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    assert not built['errors'] and built['payload']['form'] == '3517-S CA12'
    assert built['payload']['payment'] == {'amount': 13400, 'method': 'hors_edi'}
    assert any('mandat' in w for w in built['warnings'])
    CT.save_fiscal_profile(c, None, {'mandate': {'iban': IBAN, 'bic': 'AGRIFRPP', 'holder': 'Exemple SARL', 'rum': 'RUM-1',
                                                 'signed_at': '2026-01-10', 'bank_confirmed': True}}, rt.now())
    built = T.build_declaration(c, 'TVA_CA12', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    pay = built['payload']['payment']
    assert pay['method'] == 'prelevement_edi' and pay['iban'] == IBAN and pay['rum'] == 'RUM-1'
    # Deux acomptes sur le même exercice de référence : distingués par l'échéance.
    july = T.build_declaration(c, 'TVA_3514', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY,
                               due_date='2026-07-15')
    dec = T.build_declaration(c, 'TVA_3514', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY,
                              due_date='2026-12-15')
    assert july['payload']['payment']['amount'] == 7700 and dec['payload']['payment']['amount'] == 5600
    T.submit_declaration(c, july)
    T.submit_declaration(c, dec)
    with pytest.raises(T.TeledeclarationError):
        T.submit_declaration(c, dec)
    rows = c.execute("SELECT due_date FROM teledeclarations WHERE decl_type='TVA_3514' ORDER BY id").fetchall()
    c.close()
    assert [r['due_date'] for r in rows] == ['2026-07-15', '2026-12-15']


def test_is_instalments_distinct_due_dates(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    book_2025(c)
    for due in ('2026-03-15', '2026-06-15'):
        b = T.build_declaration(c, 'IS_2571', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY, due_date=due)
        assert not b['errors']
        T.submit_declaration(c, b)
    # Dépôts simulés : visibles pour éviter les doublons de simulation, jamais comptés comme réels.
    assert T.active_declaration(c, None, 'IS_2571', '2025-01-01', '2025-12-31', '2026-06-15', include_simulation=True)
    assert not T.active_declaration(c, None, 'IS_2571', '2025-01-01', '2025-12-31', '2026-06-15')
    assert not T.active_declaration(c, None, 'IS_2571', '2025-01-01', '2025-12-31', '2026-09-15', include_simulation=True)
    bad = T.build_declaration(c, 'IS_2571', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY, due_date='2025-12-15')
    c.close()
    assert any('échéance' in e for e in bad['errors'])


def test_disposal_entry_and_2059a(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    c.execute("""INSERT INTO fixed_assets(label,asset_account,depreciation_account,expense_account,purchase_date,
                 purchase_amount,useful_life_years,created_at) VALUES('Véhicule','218200','281820','681000','2023-01-01',10000,5,?)""",
              (rt.now(),))
    aid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for y, amt in (('2023', 2000), ('2024', 2000)):
        c.execute("INSERT INTO fixed_asset_depreciation_runs(asset_id,period_label,amount,created_at) VALUES(?,?,?,?)", (aid, y, amt, rt.now()))
    c.commit()
    asset = c.execute('SELECT * FROM fixed_assets WHERE id=?', (aid,)).fetchone()
    res = dispose_fixed_asset(c, asset, '2025-06-30', 7000, 20)
    assert res['complementary'] == pytest.approx(2000 * 181 / 365, abs=0.01) and res['missing_years'] == []
    assert res['vnc'] == pytest.approx(10000 - 4000 - res['complementary'], abs=0.01)
    lines = {l['account_code']: (l['debit'], l['credit']) for l in
             c.execute('SELECT * FROM accounting_entry_lines WHERE entry_id=?', (res['entry_id'],)).fetchall()}
    assert lines['218200'] == (0, 10000) and lines['462000'] == (8400, 0) and lines['775000'] == (0, 7000)
    assert lines['445710'] == (0, 1400) and lines['675000'][0] == pytest.approx(res['vnc'], abs=0.01)
    with pytest.raises(Exception):
        dispose_fixed_asset(c, c.execute('SELECT * FROM fixed_assets WHERE id=?', (aid,)).fetchone(), '2025-07-01', 1)
    ax = annexes(c, date(2025, 1, 1), date(2025, 12, 31), None, profile={}, settings={})
    c.close()
    assert len(ax['pv_2059a']) == 1
    pv = ax['pv_2059a'][0]
    assert pv['prix'] == 7000 and pv['court_terme'] == pytest.approx(res['gain'], abs=0.01) and pv['long_terme'] == 0
    cession = [r for r in ax['immo_2054'] if r['cessions']]
    assert cession and cession[0]['codes_b'][1]


def test_disposal_skips_months_already_posted(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    c.execute("""INSERT INTO fixed_assets(label,asset_account,depreciation_account,expense_account,purchase_date,
                 purchase_amount,useful_life_years,created_at) VALUES('Ordinateur','218300','281830','681000','2024-01-01',3650,1,?)""",
              (rt.now(),))
    aid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute("INSERT INTO fixed_asset_depreciation_runs(asset_id,period_label,amount,created_at) VALUES(?,?,?,?)", (aid, '2024-T1', 900, rt.now()))
    c.commit()
    res = dispose_fixed_asset(c, c.execute('SELECT * FROM fixed_assets WHERE id=?', (aid,)).fetchone(), '2024-06-30', 0)
    c.execute("""INSERT INTO fixed_assets(label,asset_account,depreciation_account,expense_account,purchase_date,
                 purchase_amount,useful_life_years,created_at) VALUES('Oublié','218300','281830','681000','2022-03-01',1000,5,?)""", (rt.now(),))
    other = c.execute("SELECT * FROM fixed_assets WHERE label='Oublié'").fetchone()
    assert dispose_fixed_asset(c, other, '2024-05-31', 0)['missing_years'] == [2022, 2023]
    c.close()
    assert res['missing_years'] == []
    # Prorata au 30/06 (182 jours sur 366) moins le trimestre déjà passé.
    assert res['complementary'] == pytest.approx(3650 * 182 / 366 - 900, abs=0.5)


def test_coded_tables_and_2065_export(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    book_2025(c)
    profile = CT.get_fiscal_profile(c, None)
    profile.update({'signataire': 'A. Gérant', 'activite': 'Conseil',
                    'shareholders': [{'kind': 'PP', 'name': 'Martin', 'pct': 60, 'shares': 600},
                                     {'kind': 'PM', 'name': 'Holding', 'siren': '732829320', 'pct': 40, 'shares': 400}],
                    'subsidiaries': [{'name': 'Filiale', 'pct': 51}]})
    l = liasse_normale(c, date(2025, 1, 1), date(2025, 12, 31), None, settings={})
    ax = annexes(c, date(2025, 1, 1), date(2025, 12, 31), None, profile=profile, settings={'mvlt_anterieures': 500})
    is_res = CT.compute_is(l['fiscal']['XN'], date(2025, 1, 1), date(2025, 12, 31), l['chiffre_affaires'], True)
    f = form_2065(IDENTITY, profile, l, is_res, date(2025, 1, 1), date(2025, 12, 31))
    c.close()
    rows = annexes_rows(ax, f)
    by_case = {(r['Formulaire'], r['Case']): r['Montant'] for r in rows if r['Case']}
    assert by_case[('2054', 'LD')] == 3000 and by_case[('2054', 'ØJ')] == 3000 and by_case[('2054', 'ØL')] == 3000
    assert ('2055', 'ØR') in by_case and ('2056', 'UE') in by_case
    assert ax['capital_2059f']['nb_pp'] == 1 and ax['capital_2059f']['nb_pm'] == 1 and ax['capital_2059f']['parts_pp'] == 600
    assert ax['filiales_2059g']['count'] == 1 and not ax['checks']
    assert ax['mvlt_2059c'] == {'anterieures': 500, 'imputees': 0, 'solde': 500}
    assert f['benefice'] == 70000 and f['benefice_taux_15'] == 42500 and f['signataire'] == 'A. Gérant'
    assert {r['Formulaire'] for r in rows} >= {'2054', '2055', '2056', '2059-A', '2059-F', '2059-G', '2065'}


def test_liasse_teledeclaration_carries_annexes(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    book_2025(c)
    built = T.build_declaration(c, 'LIASSE', date(2025, 1, 1), date(2025, 12, 31), identity=IDENTITY)
    c.close()
    forms = {b.get('form') for b in built['payload']['boxes']}
    assert {'2050', '2054', '2065'} <= forms
    assert built['payload']['annex_lists']['2065']['denomination'] == 'Exemple SARL'
    assert any('Signataire' in w for w in built['warnings'])


def test_fiscal_profile_page_and_mandate_masking(app):
    cl, oid = signup(app)
    assert cl.get('/comptabilite/profil-fiscal').status_code == 200
    r = post(cl, '/comptabilite/profil-fiscal', {
        'tax_regime': 'simplifie', 'vat_filing': 'ca12', 'signataire': 'A. Gérant',
        'sh_kind_1': 'PP', 'sh_name_1': 'Martin', 'sh_pct_1': '60', 'sh_shares_1': '600',
        'sub_name_1': 'Filiale', 'sub_siren_1': '732 829 320', 'sub_pct_1': '51',
        'mandate_iban': 'FR76 3000 6000 0112 3456 7890 189', 'mandate_bic': 'AGRIFRPP', 'mandate_rum': 'RUM-1',
        'mandate_signed_at': '2026-01-10', 'mandate_bank_confirmed': '1'})
    assert r.status_code == 302
    page = cl.get('/comptabilite/profil-fiscal').get_data(as_text=True)
    assert 'FR76 •••• •••• 0189' in page and '30006000011234567890189' not in page and 'Mandat complet' in page
    # Champ IBAN laissé vide : le compte enregistré est conservé.
    post(cl, '/comptabilite/profil-fiscal', {'tax_regime': 'simplifie', 'vat_filing': 'ca12',
                                                 'mandate_iban': '', 'mandate_rum': 'RUM-1', 'mandate_signed_at': '2026-01-10',
                                                 'mandate_bank_confirmed': '1'})
    c = rt.tenant_cx_direct(oid)
    p = CT.get_fiscal_profile(c, None)
    c.close()
    assert p['mandate']['iban'] == IBAN and CT.mandate_ready(p) and p['vat_filing'] == 'ca12'
    # Saisie invalide : rien n'est enregistré.
    post(cl, '/comptabilite/profil-fiscal', {'mandate_iban': 'FR76 0000', 'sh_name_1': 'X', 'sh_pct_1': '150'})
    c = rt.tenant_cx_direct(oid)
    p2 = CT.get_fiscal_profile(c, None)
    c.close()
    assert p2['mandate']['iban'] == IBAN and p2['vat_filing'] == 'ca12'


def test_mandate_reserved_to_owner_and_admin(app):
    cl, oid = signup(app, role='COMPTABLE')
    page = cl.get('/comptabilite/profil-fiscal').get_data(as_text=True)
    assert 'name="mandate_iban"' not in page
    post(cl, '/comptabilite/profil-fiscal', {'tax_regime': 'normal', 'vat_filing': 'ca12',
                                                 'mandate_iban': IBAN, 'mandate_rum': 'X', 'mandate_signed_at': '2026-01-01',
                                                 'mandate_bank_confirmed': '1'})
    c = rt.tenant_cx_direct(oid)
    p = CT.get_fiscal_profile(c, None)
    c.close()
    assert p['vat_filing'] == 'ca12' and not p['mandate']['iban']


def test_ca12_page_and_liasse_page_render(app):
    cl, oid = signup(app)
    c = rt.tenant_cx_direct(oid)
    book_2025(c)
    c.close()
    page = cl.get('/comptabilite/tva/ca12?exercice=precedent').get_data(as_text=True)
    import re
    assert '3517-S' in page and re.search(r'13\D{1,8}400', page) and 'TVA_CA12' in page
    page = cl.get('/comptabilite/liasse-2050?exercice=precedent').get_data(as_text=True)
    assert '2059-A' in page and '2059-F' in page and '2065' in page and 'ØL' in page
    csv = cl.get('/comptabilite/liasse-2050?exercice=precedent&format=csv').get_data(as_text=True)
    assert '2054;LD;' in csv and '2065;' in csv


def test_deadlines_follow_vat_regime(app):
    add_org(1)
    c = rt.tenant_cx_direct(1)
    book_2025(c)
    ca3 = A.upcoming_deadlines(c, None, date(2026, 4, 20), horizon_days=90)
    assert any(x['type'] == 'TVA_CA3' for x in ca3) and not any(x['type'] == 'TVA_CA12' for x in ca3)
    CT.save_fiscal_profile(c, None, {'vat_filing': 'ca12'}, rt.now())
    ca12 = A.upcoming_deadlines(c, None, date(2026, 4, 20), horizon_days=90)
    assert not any(x['type'] == 'TVA_CA3' for x in ca12)
    assert [x['date'] for x in ca12 if x['type'] == 'TVA_CA12'] == [date(2026, 5, 5)]
    july = [x for x in ca12 if x['type'] == 'TVA_3514']
    assert july and july[0]['date'] == date(2026, 7, 15) and july[0]['amount'] == 7700
    CT.save_fiscal_profile(c, None, {'vat_filing': 'franchise'}, rt.now())
    fr = A.upcoming_deadlines(c, None, date(2026, 4, 20), horizon_days=90)
    c.close()
    assert not any(str(x.get('type') or '').startswith('TVA') for x in fr)
