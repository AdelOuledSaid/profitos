"""v458 — Dossiers comptables de référence. Chaque dossier est passé en écritures puis les
résultats de ProfitOS sont comparés à des montants calculés à la main, indépendamment du
code (détail des calculs : docs/validation-fiscale.md). Tolérance : 1 € (arrondis à
l'euro des formulaires)."""
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, corporate_tax as CT, tiers_declarations as TD
from profitos.config import DevelopmentConfig
from profitos.accounting import create_entry, dispose_fixed_asset
from profitos.liasse import liasse_2033
from profitos.liasse_normal import liasse_normale
from profitos.liasse_2033_annexes import annexes_2033
from profitos.vat_return import ca12_form, ca3_form

Y0, Y1 = date(2025, 1, 1), date(2025, 12, 31)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, 'AUTH_DB', tmp_path / 'auth.db')
    monkeypatch.setattr(rt, 'TENANTS', tmp_path)
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


def E(c, journal, day, label, *lines):
    create_entry(c, journal, day, label, [l if isinstance(l, dict) else
                                          dict(zip(('account_code', 'debit', 'credit', 'auxiliary_name'), l)) for l in lines])


def VAT(amount, rate, base):
    return {'account_code': '445710', 'credit': amount, 'vat_rate': rate, 'vat_base': base}


def near(a, b, tol=1.0):
    return abs(float(a) - float(b)) <= tol


# ----------------------------------------------------------------------------------------
# Dossier A — SARL de services, IS au régime simplifié, TVA CA12, exercice 2025
# ----------------------------------------------------------------------------------------
def dossier_a(c):
    E(c, 'BQ', '2025-01-02', 'Capital', ('512000', 10000, 0, None), ('101300', 0, 10000, None))
    E(c, 'VE', '2025-06-30', 'Prestations', ('411000', 144000, 0, 'Clients'), ('706000', 0, 120000, None), VAT(24000, 20.0, 120000))
    E(c, 'BQ', '2025-07-31', 'Encaissements', ('512000', 132000, 0, None), ('411000', 0, 132000, 'Clients'))
    E(c, 'AC', '2025-03-01', 'Fournitures', ('606400', 30000, 0, None), ('445660', 6000, 0, None), ('401000', 0, 36000, 'Fourn'))
    E(c, 'BQ', '2025-03-31', 'Règlement', ('401000', 36000, 0, 'Fourn'), ('512000', 0, 36000, None))
    E(c, 'AC', '2025-12-31', 'Loyers', ('613200', 12000, 0, None), ('445660', 2400, 0, None), ('401000', 0, 14400, 'SCI'))
    E(c, 'BQ', '2025-12-31', 'Loyers payés', ('401000', 14400, 0, 'SCI'), ('512000', 0, 14400, None))
    E(c, 'AC', '2025-04-02', 'Avocat', ('622600', 2000, 0, None), ('445660', 400, 0, None), ('401000', 0, 2400, 'Cabinet Dupont'))
    E(c, 'AC', '2025-09-02', 'Frais d\'actes', ('622700', 500, 0, None), ('445660', 100, 0, None), ('401000', 0, 600, 'Cabinet Dupont'))
    E(c, 'BQ', '2025-09-30', 'Règlement avocat', ('401000', 3000, 0, 'Cabinet Dupont'), ('512000', 0, 3000, None))
    E(c, 'AC', '2025-05-02', 'Conseil', ('622600', 900, 0, None), ('445660', 180, 0, None), ('401000', 0, 1080, 'Petit Conseil'))
    E(c, 'BQ', '2025-05-31', 'Règlement conseil', ('401000', 1080, 0, 'Petit Conseil'), ('512000', 0, 1080, None))
    E(c, 'OD', '2025-12-31', 'Salaires', ('641000', 24000, 0, None), ('421000', 0, 24000, None))
    E(c, 'BQ', '2025-12-31', 'Salaires payés', ('421000', 24000, 0, None), ('512000', 0, 24000, None))
    E(c, 'OD', '2025-12-31', 'Charges sociales', ('645000', 10000, 0, None), ('431000', 0, 10000, None))
    E(c, 'BQ', '2025-12-31', 'Urssaf', ('431000', 10000, 0, None), ('512000', 0, 10000, None))
    E(c, 'AC', '2025-04-01', 'Véhicule utilitaire', ('218200', 15000, 0, None), ('445620', 3000, 0, None), ('404000', 0, 18000, 'Garage'))
    E(c, 'BQ', '2025-04-15', 'Règlement véhicule', ('404000', 18000, 0, 'Garage'), ('512000', 0, 18000, None))
    E(c, 'OD', '2025-12-31', 'Dotation', ('681100', 2260.27, 0, None), ('281820', 0, 2260.27, None))
    E(c, 'BQ', '2025-08-01', 'Amende', ('671200', 300, 0, None), ('512000', 0, 300, None))


def test_dossier_a_regime_simplifie(app):
    c = tenant(app)
    dossier_a(c)
    l = liasse_2033(c, Y0, Y1)
    # TVA non liquidée à la clôture : créance de TVA déductible (12 080) et dette de TVA
    # collectée (24 000) présentées sans compensation.
    assert l['balanced'] and near(l['total_actif'], 72039.73) and near(l['total_passif'], 72039.73)
    assert near(l['resultat'], 38039.73) and near(l['capitaux_propres'], 48039.73)
    actif = {a['box_brut']: a for a in l['actif']}
    passif = {p['box']: p for p in l['passif']}
    assert near(actif['028']['brut'], 15000) and near(actif['028']['amort'], 2260.27)
    assert near(actif['068']['brut'], 12000) and near(actif['084']['brut'], 35220) and near(actif['072']['brut'], 12080)
    assert near(passif['172']['amount'], 24000) and near(passif['120']['amount'], 10000)
    settings = {'capital_libere': True, 'detention_pp': True}
    ax = annexes_2033(c, Y0, Y1, None, profile={}, settings=settings)
    rf = ax['resultat_fiscal']
    assert near(rf['benefice'], 38339.73)                       # + amende 300 réintégrée
    transport = next(r for r in ax['immo'] if r['label'] == 'Matériel de transport')
    assert near(transport['fin'], 15000)
    assert near(next(r for r in ax['amort'] if r['label'] == 'Matériel de transport')['dotations'], 2260.27)
    is_res = CT.compute_is(rf['benefice'], Y0, Y1, 120000, CT.pme_eligible(120000, Y0, Y1, True, True))
    assert is_res['is_total'] == 5751                            # 15 % × 38 340 €
    f = ca12_form(c, '2025-01-01', '2025-12-31')
    assert (f['l16'], f['l20'], f['l23'], f['l28'], f['l57']) == (24000, 9080, 3000, 11920, 14920)
    assert [a['amount'] for a in f['instalments']] == [8206, 5968]
    d2 = TD.das2(c, 2025)
    rows = {r['name']: r for r in d2['rows']}
    assert rows['Cabinet Dupont']['amount'] == 3000 and rows['Cabinet Dupont']['include']
    assert rows['Petit Conseil']['amount'] == 1080 and not rows['Petit Conseil']['include']
    cvae = CT.compute_cvae(120000, ax['valeur_ajoutee'], Y0, Y1)
    assert cvae['cvae'] == 0 and not cvae['declaration_obligatoire']
    c.close()


# ----------------------------------------------------------------------------------------
# Dossier B — SAS, régime réel normal, TVA CA3, PME au taux réduit, déficits antérieurs
# ----------------------------------------------------------------------------------------
def dossier_b(c):
    E(c, 'BQ', '2025-01-02', 'Capital', ('512000', 50000, 0, None), ('101300', 0, 50000, None))
    E(c, 'VE', '2025-06-30', 'Ventes 20 %', ('411000', 840000, 0, 'Clients'), ('707000', 0, 700000, None), VAT(140000, 20.0, 700000))
    E(c, 'VE', '2025-06-30', 'Ventes 5,5 %', ('411000', 211000, 0, 'Clients'), ('707000', 0, 200000, None), VAT(11000, 5.5, 200000))
    E(c, 'BQ', '2025-11-30', 'Encaissements', ('512000', 1000000, 0, None), ('411000', 0, 1000000, 'Clients'))
    E(c, 'AC', '2025-03-01', 'Marchandises', ('607000', 300000, 0, None), ('445660', 60000, 0, None), ('401000', 0, 360000, 'Grossiste'))
    E(c, 'BQ', '2025-04-30', 'Règlement', ('401000', 360000, 0, 'Grossiste'), ('512000', 0, 360000, None))
    E(c, 'OD', '2025-12-31', 'Stock final', ('370000', 20000, 0, None), ('603700', 0, 20000, None))
    E(c, 'AC', '2025-06-01', 'Services', ('622600', 100000, 0, None), ('445660', 20000, 0, None), ('401000', 0, 120000, 'Prestataires'))
    E(c, 'BQ', '2025-06-30', 'Règlement services', ('401000', 120000, 0, 'Prestataires'), ('512000', 0, 120000, None))
    E(c, 'OD', '2025-12-31', 'Salaires', ('641000', 200000, 0, None), ('421000', 0, 200000, None))
    E(c, 'BQ', '2025-12-31', 'Salaires payés', ('421000', 200000, 0, None), ('512000', 0, 200000, None))
    E(c, 'OD', '2025-12-31', 'Charges sociales', ('645000', 90000, 0, None), ('431000', 0, 90000, None))
    E(c, 'BQ', '2025-12-31', 'Urssaf', ('431000', 90000, 0, None), ('512000', 0, 90000, None))
    E(c, 'BQ', '2025-12-15', 'CFE', ('635110', 2000, 0, None), ('512000', 0, 2000, None))
    E(c, 'BQ', '2025-01-02', 'Emprunt', ('512000', 100000, 0, None), ('164000', 0, 100000, None))
    E(c, 'BQ', '2025-12-31', 'Échéances', ('164000', 18000, 0, None), ('661600', 3000, 0, None), ('512000', 0, 21000, None))
    E(c, 'AC', '2025-01-02', 'Machine', ('215400', 60000, 0, None), ('445620', 12000, 0, None), ('404000', 0, 72000, 'Fabricant'))
    E(c, 'BQ', '2025-01-31', 'Règlement machine', ('404000', 72000, 0, 'Fabricant'), ('512000', 0, 72000, None))
    E(c, 'OD', '2025-12-31', 'Dotation', ('681100', 6000, 0, None), ('281540', 0, 6000, None))
    E(c, 'OD', '2025-12-31', 'Provision litige', ('681500', 5000, 0, None), ('151100', 0, 5000, None))


def test_dossier_b_regime_normal(app):
    c = tenant(app)
    dossier_b(c)
    settings = {'deficits_anterieurs': 30000, 'capital_libere': True, 'detention_pp': True}
    l = liasse_normale(c, Y0, Y1, None, settings=settings)
    # TVA non liquidée : créance de TVA déductible (92 000) et dette de TVA collectée
    # (151 000) sans compensation → total 410 000 + 92 000.
    assert near(l['totals_a']['CO_net'], 502000) and near(l['totals_p']['EE'], 502000) and l['balanced']
    assert near(l['cr']['HN'], 214000)
    assert near(l['fiscal']['XL'], 30000) and near(l['fiscal']['XN'], 184000)
    assert near(l['chiffre_affaires'], 900000)
    is_res = CT.compute_is(l['fiscal']['XN'], Y0, Y1, l['chiffre_affaires'],
                           CT.pme_eligible(l['chiffre_affaires'], Y0, Y1, True, True))
    assert is_res['is_total'] == 41750 and is_res['contribution_sociale'] == 0     # 6 375 + 35 375
    # Valeur ajoutée (2059-E) : la hausse du stock diminue les consommations.
    assert near(l['va']['OX'], 900000) and near(l['va']['OG'], 520000)
    cvae = CT.compute_cvae(l['va']['OX'], l['va']['OG'], Y0, Y1)
    # Barème 2025 (0,19 %) : 0,063 % × 400 000 / 2 500 000 = 0,0101 % → 0,01 % × 520 000 = 52 €,
    # inférieur au dégrèvement de 125 € → 0.
    assert near(cvae['cotisation_brute'], 52, 0.01) and cvae['degrevement'] == 125 and cvae['cvae'] == 0
    vat = ca3_form(c, '2025-01-01', '2025-12-31')
    assert near(vat['l16'], 151000) and near(vat['l32'], 59000)
    c.close()


# ----------------------------------------------------------------------------------------
# Dossier C — exercice déficitaire avec cession d'immobilisation, régime simplifié
# ----------------------------------------------------------------------------------------
def test_dossier_c_deficit_et_cession(app):
    c = tenant(app)
    E(c, 'AN', '2025-01-01', 'À-nouveaux', ('215400', 10000, 0, None), ('281540', 0, 4000, None),
      ('512000', 14000, 0, None), ('101300', 0, 20000, None))
    c.execute("""INSERT INTO fixed_assets(label,asset_account,depreciation_account,expense_account,purchase_date,
                 purchase_amount,useful_life_years,created_at) VALUES('Machine','215400','281540','681100','2023-01-01',10000,5,?)""",
              (rt.now(),))
    aid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for y in ('2023', '2024'):
        c.execute("INSERT INTO fixed_asset_depreciation_runs(asset_id,period_label,amount,created_at) VALUES(?,?,2000,?)", (aid, y, rt.now()))
    c.commit()
    E(c, 'VE', '2025-05-31', 'Ventes', ('411000', 60000, 0, 'Clients'), ('706000', 0, 50000, None), VAT(10000, 20.0, 50000))
    E(c, 'AC', '2025-05-31', 'Achats', ('606400', 70000, 0, None), ('401000', 0, 70000, 'Fourn'))
    res = dispose_fixed_asset(c, c.execute('SELECT * FROM fixed_assets WHERE id=?', (aid,)).fetchone(), '2025-06-30', 7000, 20)
    assert near(res['complementary'], 991.78, 0.01) and near(res['vnc'], 5008.22, 0.01) and near(res['gain'], 1991.78, 0.01)
    l = liasse_2033(c, Y0, Y1)
    assert l['balanced'] and near(l['total_actif'], 82400) and near(l['resultat'], -19000)
    ax = annexes_2033(c, Y0, Y1, None, profile={}, settings={'deficits_anterieurs': 5000})
    assert near(ax['pv_tot']['court_terme'], 1991.78) and ax['pv_tot']['long_terme'] == 0
    d = {x['code']: x['amount'] for x in ax['deficits']}
    assert d == {'982': 5000, '983': 0, '984': 5000, '860': 19000, '870': 24000}
    assert ax['resultat_fiscal']['benefice'] == 0 and near(ax['resultat_fiscal']['deficit'], 19000)
    assert next(r for r in ax['immo'] if r['fin'] or r['debut'])['diminutions'] == 10000
    c.close()
