"""v460 — Fiabilité des calculs : dossiers de référence complémentaires.

Chaque cas est passé en écritures (ou appliqué directement au calcul) et comparé à des
montants calculés à la main à partir des notices officielles (3310-CA3, 2571, 2572,
1329-AC / 1329-DEF, 2050 à 2057) et du BOFiP. Détail : docs/validation-fiscale.md, § 4.
Tolérance : 1 €.
"""
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import create_app, corporate_tax as CT, teledeclaration as T
from profitos.config import DevelopmentConfig
from profitos.accounting import AccountingError, create_entry
from profitos.ledger import add_fiscal_year
from profitos.liasse_normal import liasse_normale
from profitos.liasse_annexes import annexes as annexes_normal, form_2065
from profitos.loans import create_loan, pay_installment
from profitos.vat_return import ca3_form, generate_liquidation_entry

IDENTITY = {'name': 'Exemple SAS', 'siren': '732829320', 'siret': '73282932000074', 'address': '1 rue Test',
            'postal_code': '75001', 'city': 'Paris'}


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
    return create_entry(c, journal, day, label, [l if isinstance(l, dict) else
                                                 dict(zip(('account_code', 'debit', 'credit', 'auxiliary_name'), l)) for l in lines])


def VAT(amount, rate, base):
    return {'account_code': '445710', 'credit': amount, 'vat_rate': rate, 'vat_base': base}


def near(a, b, tol=1.0):
    return abs(float(a) - float(b)) <= tol


def balance(c, account):
    r = c.execute('SELECT COALESCE(SUM(debit-credit),0) n FROM accounting_entry_lines WHERE account_code=?', (account,)).fetchone()
    return round(float(r['n']), 2)


# ----------------------------------------------------------------------------------------
# Dossier D — CA3 mensuelles : crédit de TVA reporté d'un mois sur l'autre (T1 2026)
# ----------------------------------------------------------------------------------------
def dossier_d_january(c):
    E(c, 'VE', '2026-01-20', 'Ventes janvier', ('411000', 12000, 0, 'Clients'), ('706000', 0, 10000, None), VAT(2000, 20.0, 10000))
    E(c, 'AC', '2026-01-10', 'Machine', ('215400', 50000, 0, None), ('445620', 10000, 0, None), ('404000', 0, 60000, 'Fabricant'))
    E(c, 'AC', '2026-01-15', 'Achats janvier', ('606100', 5000, 0, None), ('445660', 1000, 0, None), ('401000', 0, 6000, 'Fourn'))


def dossier_d_february(c):
    E(c, 'VE', '2026-02-20', 'Ventes février', ('411000', 24000, 0, 'Clients'), ('706000', 0, 20000, None), VAT(4000, 20.0, 20000))
    E(c, 'AC', '2026-02-15', 'Achats février', ('606100', 10000, 0, None), ('445660', 2000, 0, None), ('401000', 0, 12000, 'Fourn'))


def dossier_d_march(c):
    E(c, 'VE', '2026-03-20', 'Ventes mars 20 %', ('411000', 60000, 0, 'Clients'), ('706000', 0, 50000, None), VAT(10000, 20.0, 50000))
    E(c, 'VE', '2026-03-25', 'Ventes mars 10 %', ('411000', 22000, 0, 'Clients'), ('706000', 0, 20000, None), VAT(2000, 10.0, 20000))
    E(c, 'AC', '2026-03-15', 'Achats mars', ('606100', 15000, 0, None), ('445660', 3000, 0, None), ('401000', 0, 18000, 'Fourn'))


def test_dossier_d_ca3_credit_reporte(app):
    c = tenant(app)
    dossier_d_january(c)
    jan = ca3_form(c, '2026-01-01', '2026-01-31')
    # Janvier : TVA brute 2 000 ; déductible 10 000 (immobilisation) + 1 000 → crédit 9 000.
    assert (jan['l16'], jan['l19'], jan['l20'], jan['l22'], jan['l23']) == (2000, 10000, 1000, 0, 11000)
    assert (jan['l25'], jan['l27'], jan['l28'], jan['l32']) == (9000, 9000, 0, 0)
    generate_liquidation_entry(c, '2026-01-01', '2026-01-31')
    assert balance(c, '445670') == 9000 and balance(c, '445710') == 0 and balance(c, '445660') == 0
    with pytest.raises(AccountingError):
        generate_liquidation_entry(c, '2026-01-01', '2026-01-31')         # une seule liquidation
    # Après liquidation, la CA3 de janvier reste lisible (écriture de liquidation exclue).
    assert ca3_form(c, '2026-01-01', '2026-01-31')['l25'] == 9000

    dossier_d_february(c)
    feb = ca3_form(c, '2026-02-01', '2026-02-28')
    # Février : 4 000 − (2 000 + crédit reporté 9 000) → nouveau crédit 7 000.
    assert (feb['l16'], feb['l20'], feb['l22'], feb['l23'], feb['l25'], feb['l32']) == (4000, 2000, 9000, 11000, 7000, 0)
    generate_liquidation_entry(c, '2026-02-01', '2026-02-28')
    assert balance(c, '445670') == 7000

    dossier_d_march(c)
    mar = ca3_form(c, '2026-03-01', '2026-03-31')
    # Mars : TVA brute 10 000 + 2 000 = 12 000 − (3 000 + crédit 7 000) → 2 000 à payer.
    by_rate = {r['rate']: r for r in mar['rates']}
    assert near(by_rate[20.0]['base'], 50000) and near(by_rate[10.0]['vat'], 2000)
    assert (mar['l01'], mar['l16'], mar['l20'], mar['l22'], mar['l23'], mar['l25'], mar['l28'], mar['l32']) == \
        (70000, 12000, 3000, 7000, 10000, 0, 2000, 2000)
    built = T.build_declaration(c, 'TVA_CA3', date(2026, 3, 1), date(2026, 3, 31), identity=IDENTITY)
    boxes = {b['code']: b['amount'] for b in built['payload']['boxes'] if not b.get('kind')}
    assert boxes['22'] == 7000 and boxes['32'] == 2000 and built['payload']['payment']['amount'] == 2000
    assert not any('période précédente' in w for w in built['warnings'])
    generate_liquidation_entry(c, '2026-03-01', '2026-03-31')
    # Fin du trimestre : crédit entièrement imputé, 2 000 € de TVA à décaisser.
    assert balance(c, '445670') == 0 and balance(c, '445510') == -2000
    for account in ('445710', '445620', '445660'):
        assert balance(c, account) == 0
    c.close()


def test_dossier_d_mois_precedent_non_liquide(app):
    """Sans liquidation de janvier, le crédit ne peut pas être reporté : ProfitOS le signale
    au lieu de préparer silencieusement une CA3 de février sans la ligne 22."""
    c = tenant(app)
    dossier_d_january(c)
    dossier_d_february(c)
    feb = ca3_form(c, '2026-02-01', '2026-02-28')
    assert feb['l22'] == 0
    built = T.build_declaration(c, 'TVA_CA3', date(2026, 2, 1), date(2026, 2, 28), identity=IDENTITY)
    assert any('période précédente' in w for w in built['warnings'])
    c.close()


# ----------------------------------------------------------------------------------------
# Dossier E — acomptes et solde d'IS (2571 / 2572)
# ----------------------------------------------------------------------------------------
Y25 = (date(2025, 1, 1), date(2025, 12, 31))
Y26 = (date(2026, 1, 1), date(2026, 12, 31))


def test_dossier_e_acomptes_et_solde_is():
    # IS 2025 : bénéfice 100 000, PME → 15 % × 42 500 + 25 % × 57 500 = 6 375 + 14 375.
    ref = CT.compute_is(100000, *Y25, 900000, True)
    assert ref['is_total'] == 20750
    # Acomptes 2026 : 6,25 % × 57 500 + 3,75 % × 42 500 = 3 593,75 + 1 593,75 = 5 187,50 → 5 188.
    inst = CT.next_instalments(ref['is_total'], *Y26, *Y25)
    assert [(a['date'], a['amount']) for a in inst] == [(date(2026, 3, 15), 5188), (date(2026, 6, 15), 5188),
                                                        (date(2026, 9, 15), 5188), (date(2026, 12, 15), 5188)]
    # Solde 2026 (2572) : bénéfice 135 500 → IS 6 375 + 25 % × 93 000 = 29 625 ;
    # − acomptes versés 4 × 5 188 = 20 752 → solde 8 873.
    is26 = CT.compute_is(135500, *Y26, 1000000, True, acomptes=4 * 5188)
    assert is26['is_total'] == 29625 and is26['solde'] == 8873
    # Exercice moins bénéficiaire : excédent d'acomptes à restituer.
    low = CT.compute_is(60000, *Y26, 1000000, True, acomptes=4 * 5188)
    assert low['is_total'] == 6375 + 4375 and low['solde'] == 10750 - 20752
    # Crédits d'impôt imputés sur le solde.
    cred = CT.compute_is(135500, *Y26, 1000000, True, credits=1500, acomptes=4 * 5188)
    assert cred['solde'] == 8873 - 1500
    assert CT.is_balance_deadline(Y26[1]) == date(2027, 5, 15)
    assert CT.is_balance_deadline(date(2026, 6, 30)) == date(2026, 10, 15)


def test_dossier_e_seuil_de_3000_euros():
    # IS de référence 3 000 € exactement (20 000 × 15 %) : dispense (« n'excède pas 3 000 € »).
    assert CT.compute_is(20000, *Y25, 200000, True)['is_total'] == 3000
    assert CT.instalment_amount(3000, *Y25) == 0 and CT.next_instalments(3000, *Y26, *Y25) == []
    # 3 001 € : quatre acomptes de 750 €.
    assert CT.compute_is(20007, *Y25, 200000, True)['is_total'] == 3001
    assert [a['amount'] for a in CT.next_instalments(3001, *Y26, *Y25)] == [750] * 4
    cal = CT.fiscal_calendar(*Y25, CT.compute_is(20000, *Y25, 200000, True), None)
    assert not [e for e in cal if e['form'] == '2571']


def _book_profit(c, start, profit, day):
    E(c, 'VE', day, 'Prestations', ('411000', profit, 0, 'Clients'), ('706000', 0, profit, None))


def test_dossier_e_declaration_2571_dispensee(app):
    c = tenant(app)
    add_fiscal_year(c, '2025-01-01', '2025-12-31')
    _book_profit(c, Y25[0], 20000, '2025-06-30')                       # IS 3 000 € : dispense
    b = T.build_declaration(c, 'IS_2571', *Y25, identity=IDENTITY, due_date='2026-03-15')
    assert any('Dispense' in e for e in b['errors']) and not b['payload']['payment']
    # Montant saisi malgré la dispense (versement volontaire) : avertissement, pas de blocage.
    v = T.build_declaration(c, 'IS_2571', *Y25, identity=IDENTITY, due_date='2026-03-15', instalment_amount=500)
    assert not any('Dispense' in e for e in v['errors']) and any('Dispense' in w for w in v['warnings'])
    _book_profit(c, Y25[0], 7, '2025-07-01')                           # IS 3 001 €
    b = T.build_declaration(c, 'IS_2571', *Y25, identity=IDENTITY, due_date='2026-03-15')
    assert not b['errors'] and b['payload']['payment']['amount'] == 750
    c.close()


def test_liste_des_declarations_sans_acompte_en_cas_de_dispense(app, monkeypatch):
    import uuid
    for var in ('EDI_PARTNER_ENABLED', 'EDI_SIMULATION_ENABLED', 'FISCAL_DECLARATIONS_ENABLED', 'FISCAL_DECLARATIONS_PILOT_ORGS'):
        monkeypatch.delenv(var, raising=False)
    csrf = 'x' * 32
    cl = app.test_client()
    with cl.session_transaction() as s:
        s['csrf_token'] = csrf
    cl.post('/signup', data={'csrf_token': csrf, 'accept_terms': '1', 'full_name': 'Alice Martin',
                             'email': f'{uuid.uuid4().hex[:10]}@example.com', 'password': 'Brume-Cactus-Velo-48',
                             'company_name': 'Exemple SAS'})
    with cl.session_transaction() as s:
        org_id, uid = s['org_id'], s['user_id']
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='MULTI',status='ACTIVE_PAID' WHERE id=?", (org_id,))
    ac.execute("UPDATE users SET email_verified=1,ui_mode='expert' WHERE id=?", (uid,))
    ac.commit(); ac.close()
    today = rt.paris_today()
    closed_year = today.year - 1
    c = rt.tenant_cx_direct(org_id)
    _book_profit(c, None, 20000, f'{closed_year}-06-30')                 # IS de l'exercice clos : 3 000 €
    c.close()
    html = cl.get('/comptabilite/teledeclarations').get_data(as_text=True)
    assert 'dispense pour l' in html and "Acompte d'IS du" not in html
    c = rt.tenant_cx_direct(org_id)
    _book_profit(c, None, 7, f'{closed_year}-07-01')                     # 3 001 € : quatre acomptes
    c.close()
    html = cl.get('/comptabilite/teledeclarations').get_data(as_text=True)
    assert html.count("Acompte d'IS du") == 4 and 'dispense pour l' not in html


# ----------------------------------------------------------------------------------------
# Dossier F — CVAE aux seuils (notices 1329-AC 2026 et 1329-DEF 2026)
# ----------------------------------------------------------------------------------------
def cvae(ca, va, year=2026, start=None, end=None):
    return CT.compute_cvae(ca, va, start or date(year, 1, 1), end or date(year, 12, 31))


def test_dossier_f_taux_arrondi_au_centieme():
    # Exemples de la notice 1329-DEF (barème 2025) : 0,055 % → 0,06 % ; 0,114 % → 0,11 % ;
    # 0,184 % → 0,18 %.
    assert CT.cvae_rate(2_700_000, 2025) == pytest.approx(0.0006)
    assert CT.cvae_rate(6_200_000, 2025) == pytest.approx(0.0011)
    assert CT.cvae_rate(40_100_000, 2025) == pytest.approx(0.0018)
    assert CT.cvae_rate(60_000_000, 2025) == pytest.approx(0.0019)
    # Barème 2026 (taux maximal relevé à 0,28 %).
    assert CT.cvae_rate(2_000_000, 2026) == pytest.approx(0.0006)      # 0,0564 %
    assert CT.cvae_rate(3_000_000, 2026) == pytest.approx(0.0009)      # 0,094 %
    assert CT.cvae_rate(7_600_000, 2026) == pytest.approx(0.0021)      # 0,2051 %
    assert CT.cvae_rate(60_000_000, 2026) == pytest.approx(0.0028)


def test_dossier_f_seuils_2026():
    # Déclaration 1330-CVAE obligatoire au-delà de 152 500 € de CA.
    assert not cvae(152_500, 100_000)['declaration_obligatoire']
    assert cvae(152_501, 100_000)['declaration_obligatoire']
    # Sous 500 000 € : taux nul.
    assert cvae(500_000, 400_000)['cvae'] == 0
    # CA 1 M€ : 0,0188 % → 0,02 % × 600 000 = 120 € < dégrèvement de 188 € → 0.
    r = cvae(1_000_000, 600_000)
    assert near(r['cotisation_brute'], 120) and r['degrevement'] == 188 and r['cvae'] == 0
    # Juste sous 2 M€ : 0,06 % × 1 000 000 = 600 − 188 = 412.
    assert cvae(1_999_999, 1_000_000)['cvae'] == 412
    # 2 M€ : plus de dégrèvement → 600 ; taxe additionnelle 9,23 % → 55 ; pas d'acompte.
    r = cvae(2_000_000, 1_000_000)
    assert (r['degrevement'], r['cvae'], r['taxe_additionnelle'], r['acomptes_annee_suivante']) == (0, 600, 55, [])
    # 3 M€ : 0,09 % × 2 000 000 = 1 800 > 1 500 → acomptes de 900 les 15 juin et 15 septembre 2027.
    r = cvae(3_000_000, 2_000_000)
    assert r['cvae'] == 1800 and r['taxe_additionnelle'] == 166
    assert [(a['date'], a['amount']) for a in r['acomptes_annee_suivante']] == [(date(2027, 6, 15), 900), (date(2027, 9, 15), 900)]
    # Seuil de recouvrement : 0,08 % × 78 750 = 63 € → non due ; 0,08 % × 80 000 = 64 € → due.
    assert cvae(2_500_000, 78_750)['cvae'] == 0
    assert cvae(2_500_000, 80_000)['cvae'] == 64
    # Plafonnement de la valeur ajoutée : 80 % du CA jusqu'à 7,6 M€, 85 % au-delà.
    r = cvae(7_600_000, 7_000_000)
    assert r['plafond_va_pct'] == 80 and near(r['va_plafonnee'], 6_080_000) and r['cvae'] == 12768
    r = cvae(7_600_001, 7_000_000)
    assert r['plafond_va_pct'] == 85 and r['cvae'] == 13566


def test_dossier_f_cvae_2025():
    # 2025 : barème à 0,19 %, dégrèvement de 125 €, taxe additionnelle 13,84 %,
    # contribution complémentaire de 47,4 % de la CVAE due.
    r = cvae(2_700_000, 1_000_000, 2025)
    assert (r['cvae'], r['contribution_complementaire'], r['taxe_additionnelle']) == (600, 284, 83)
    assert r['total'] == 600 + 284 + 83
    r = cvae(1_500_000, 1_000_000, 2025)                                  # 0,0252 % → 0,03 %
    assert (r['cotisation_brute'], r['degrevement'], r['cvae'], r['contribution_complementaire']) == (300, 125, 175, 83)
    assert cvae(2_700_000, 1_000_000, 2026)['contribution_complementaire'] == 0


def test_dossier_f_exercice_de_16_mois():
    # Exemple de la notice : 4 200 000 € sur 16 mois → 3 150 000 € pour le taux (0,0976 % → 0,10 %).
    start, end = date(2025, 9, 1), date(2026, 12, 31)
    r = cvae(4_200_000, 3_000_000, start=start, end=end)
    assert near(r['turnover_12m'], 3_150_000) and r['taux'] == pytest.approx(0.001)
    # La valeur ajoutée n'est pas ramenée à 12 mois ; plafond 80 % du CA réel (3 360 000).
    assert near(r['va_plafonnee'], 3_000_000) and r['cvae'] == 3000
    # Le seuil de 7,6 M€ du plafonnement s'apprécie sur le CA réel de la période :
    # 8 M€ sur 16 mois (6 M€ ramenés à 12) → plafond 85 % ; taux 0,1664 % → 0,17 %.
    r = cvae(8_000_000, 7_000_000, start=start, end=end)
    assert r['plafond_va_pct'] == 85 and near(r['va_plafonnee'], 6_800_000) and r['cvae'] == 11560


# ----------------------------------------------------------------------------------------
# Dossier G — liasse 2050 avec emprunt (2057) et provisions (2056), exercice 2025
# ----------------------------------------------------------------------------------------
def dossier_g(c):
    E(c, 'AN', '2025-01-01', 'À-nouveaux', ('512000', 50000, 0, None), ('101300', 0, 42000, None), ('151100', 0, 8000, None))
    create_loan(c, 'Banque', 120000, 0, '2025-01-01', 60)               # 60 × 2 000 €, du 01/02/2025 au 01/01/2030
    for inst in c.execute("SELECT id FROM loan_installments WHERE due_date<='2025-12-31' ORDER BY due_date").fetchall():
        pay_installment(c, inst['id'])                                   # 11 échéances payées en 2025
    E(c, 'VE', '2025-06-30', 'Ventes', ('411000', 100000, 0, 'Clients'), ('706000', 0, 100000, None))
    E(c, 'BQ', '2025-09-30', 'Encaissements', ('512000', 70000, 0, None), ('411000', 0, 70000, 'Clients'))
    E(c, 'AC', '2025-05-31', 'Achats', ('607000', 40000, 0, None), ('401000', 0, 40000, 'Fourn'))
    E(c, 'OD', '2025-12-31', 'Provision nouveau litige', ('681500', 3000, 0, None), ('151100', 0, 3000, None))
    E(c, 'OD', '2025-12-31', 'Reprise litige soldé', ('151100', 5000, 0, None), ('781500', 0, 5000, None))
    E(c, 'OD', '2025-12-31', 'Dépréciation client douteux', ('681700', 2000, 0, None), ('491000', 0, 2000, None))


def test_dossier_g_emprunt_et_provisions(app):
    c = tenant(app)
    dossier_g(c)
    l = liasse_normale(c, *Y25)
    # Résultat : 100 000 − 40 000 − 3 000 + 5 000 − 2 000 = 60 000 (prêt à taux zéro : pas d'intérêts).
    assert near(l['cr']['HN'], 60000)
    actif = {a['box_brut']: a for a in l['actif']}
    passif = {p['box']: p['amount'] for p in l['passif']}
    # Banque : 50 000 + 120 000 − 22 000 + 70 000 = 218 000 ; clients 30 000 dépréciés de 2 000.
    assert near(actif['CF']['brut'], 218000)
    assert near(actif['BX']['brut'], 30000) and near(actif['BX']['amort'], 2000) and near(actif['BX']['net'], 28000)
    # Passif : capital 42 000, résultat 60 000, provision pour risques 6 000, emprunt 98 000,
    # fournisseurs 40 000 → 246 000 = actif net 218 000 + 28 000.
    assert near(passif['DA'], 42000) and near(passif['DI'], 60000)
    assert near(passif['DP'], 6000) and near(l['totals_p']['DR'], 6000)
    assert near(passif['DU'], 98000) and near(passif['DX'], 40000)
    assert l['balanced'] and near(l['totals_a']['CO_net'], 246000) and near(l['totals_p']['EE'], 246000)
    # 2057 : capital restant dû 98 000 = 24 000 à un an (échéances 2026) + 74 000 de 1 à 5 ans.
    vh = next(d for d in l['dettes'] if d['box'] == 'VH')
    assert (vh['brut'], vh['un_an'], vh['un_cinq'], vh['plus_cinq']) == (98000, 24000, 74000, 0)
    clients = next(x for x in l['creances'] if x['box'] == 'UX')
    assert near(clients['brut'], 30000) and near(clients['un_an'], 30000)
    # 2056 : provision pour litiges 8 000 + 3 000 − 5 000 = 6 000 ; dépréciation clients 2 000.
    ax = annexes_normal(c, *Y25, None, profile={}, settings={})
    prov = {r['codes'][0]: r for r in ax['prov_2056']}
    assert (prov['4A']['debut'], prov['4A']['dotations'], prov['4A']['reprises'], prov['4A']['fin']) == (8000, 3000, 5000, 6000)
    assert (prov['6T']['debut'], prov['6T']['dotations'], prov['6T']['fin']) == (0, 2000, 2000)
    tot = {r['codes'][0]: r for r in ax['prov_2056_tot']}
    assert (tot['5Z']['debut'], tot['5Z']['fin']) == (8000, 6000)
    assert (tot['7B']['dotations'], tot['7B']['fin']) == (2000, 2000)
    assert (tot['7C']['debut'], tot['7C']['dotations'], tot['7C']['reprises'], tot['7C']['fin']) == (8000, 5000, 5000, 8000)
    split = {r['codes'][0]: r for r in ax['prov_2056_split']}
    assert (split['UE']['dotations'], split['UE']['reprises']) == (5000, 5000)
    # Compte de résultat : dotations aux provisions pour risques (GD) 3 000 ; reprises (FP) 5 000.
    assert near(l['cr']['GD'], 3000) and near(l['cr']['FP'], 5000)
    c.close()


# ----------------------------------------------------------------------------------------
# Dossier H — premier exercice de 18 mois (1er juillet 2025 – 31 décembre 2026)
# ----------------------------------------------------------------------------------------
H = (date(2025, 7, 1), date(2026, 12, 31))


def test_dossier_h_premier_exercice_de_18_mois(app):
    c = tenant(app)
    add_fiscal_year(c, '2025-07-01', '2026-12-31')
    E(c, 'BQ', '2025-07-01', 'Capital', ('512000', 10000, 0, None), ('101300', 0, 10000, None))
    E(c, 'VE', '2026-06-30', 'Prestations', ('411000', 600000, 0, 'Clients'), ('706000', 0, 600000, None))
    E(c, 'AC', '2026-06-30', 'Achats', ('604000', 400000, 0, None), ('401000', 0, 400000, 'Fourn'))
    l = liasse_normale(c, *H)
    assert l['duree_mois'] == 18 and near(l['cr']['HN'], 200000) and near(l['fiscal']['XN'], 200000)
    # PME : CA ramené à 12 mois 400 000 ≤ 10 M€ ; plafond du taux réduit 42 500 × 18 / 12 = 63 750.
    assert CT.pme_eligible(600000, *H, True, True)
    res = CT.compute_is(200000, *H, 600000, True)
    assert res['plafond_taux_reduit'] == 63750 and res['base_taux_normal'] == 136250
    assert res['is_total'] == 43625                                       # 9 562,50 + 34 062,50
    # Aucun acompte pendant le premier exercice : rien à déposer pour l'exercice « précédent ».
    assert CT.instalment_amount(0, date(2024, 7, 1), date(2025, 6, 30)) == 0
    # Acomptes de 2027 : IS ramené à 12 mois 43 625 × 12 / 18 = 29 083,33 ; quart 7 270,83 → 7 271.
    cal = CT.fiscal_calendar(*H, res, None)
    inst = [e for e in cal if e['form'] == '2571']
    assert [(e['date'], e['amount']) for e in inst] == [(date(2027, 3, 15), 7271), (date(2027, 6, 15), 7271),
                                                        (date(2027, 9, 15), 7271), (date(2027, 12, 15), 7271)]
    assert next(e for e in cal if e['form'] == '2572')['date'] == date(2027, 5, 15)
    # La 2571 de l'exercice suivant reprend ce calcul.
    b = T.build_declaration(c, 'IS_2571', *H, identity=IDENTITY, due_date='2027-03-15')
    boxes = {x['code']: x['amount'] for x in b['payload']['boxes']}
    # (seule anomalie : l'exercice n'est pas terminé à la date des tests si elle précède le 31/12/2026)
    assert set(b['errors']) <= {'Période non terminée.'} and boxes['AC'] == 7271 and boxes['IR'] == 43625 and boxes['IR12'] == 29083
    # 2065 : durée de l'exercice.
    f = form_2065(IDENTITY, {}, l, res, *H)
    assert f['duree_mois'] == 18 and f['benefice_taux_15'] == 63750
    # CVAE 2026 : CA ramené à 12 mois 400 000 < 500 000 → taux nul, mais déclaration due (> 152 500).
    cv = CT.compute_cvae(600000, 200000, *H)
    assert near(cv['turnover_12m'], 400000) and cv['cvae'] == 0 and cv['declaration_obligatoire']
    c.close()


def test_dossier_h_durees_et_contribution_sociale():
    # BOI-IS-LIQ-20-20 § 50 : du 1er janvier au 15 septembre = 8,5 mois → plafond 30 104 €.
    short = CT.compute_is(100000, date(2025, 1, 1), date(2025, 9, 15), 500000, True)
    assert near(short['plafond_taux_reduit'], 30104.17, 0.01)
    assert short['is_total'] == 21990                                     # 4 515,63 + 17 473,96
    # Exercice décalé de 12 mois : plafond entier.
    assert CT.compute_is(100000, date(2025, 3, 15), date(2026, 3, 14), 500000, True)['plafond_taux_reduit'] == 42500
    # PME : CA de 15 M€ sur 18 mois = 10 M€ ramenés à 12 mois → éligible ; 15 000 001 → non.
    assert CT.pme_eligible(15_000_000, *H, True, True)
    assert not CT.pme_eligible(15_000_002, *H, True, True)
    # Contribution sociale (BOI-IS-AUT-10-10) : abattement 763 000 × 18 / 12 = 1 144 500.
    big = CT.compute_is(5_000_000, *H, 200_000_000, False)
    assert big['is_total'] == 1_250_000 and big['contribution_sociale'] == round((1_250_000 - 1_144_500) * 0.033)
