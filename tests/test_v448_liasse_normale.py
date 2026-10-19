"""v448 — Production réglementaire, lot 1 : liasse du régime normal (2050 à 2059),
impôt sur les sociétés (taux réduit, acomptes, solde) et CVAE 2026."""
from datetime import date

from support_v4xx import app, flashes, post, signup, tenant  # noqa: F401
from profitos.accounting import create_entry
from profitos.liasse_normal import liasse_normale, liasse_normale_rows, deficit_imputable
from profitos import corporate_tax as CT


def _book(c):
    E = lambda j, d, lab, lines: create_entry(c, j, d, lab, lines)
    E('BQ', '2026-01-05', 'Capital', [{'account_code': '512000', 'debit': 20000}, {'account_code': '101300', 'credit': 20000}])
    E('BQ', '2026-01-06', 'Emprunt', [{'account_code': '512000', 'debit': 12000}, {'account_code': '164000', 'credit': 12000}])
    E('VE', '2026-02-01', 'Vente', [{'account_code': '411000', 'debit': 60000, 'auxiliary_name': 'Client'},
                                     {'account_code': '706000', 'credit': 50000},
                                     {'account_code': '445710', 'credit': 10000, 'vat_rate': 20.0, 'vat_base': 50000}])
    E('AC', '2026-02-03', 'Achat', [{'account_code': '606400', 'debit': 8000}, {'account_code': '445660', 'debit': 1600},
                                     {'account_code': '401000', 'credit': 9600, 'auxiliary_name': 'Fournisseur'}])
    E('AC', '2026-02-04', 'Ordinateur', [{'account_code': '218300', 'debit': 3000}, {'account_code': '445620', 'debit': 600},
                                          {'account_code': '404000', 'credit': 3600}])
    E('OD', '2026-12-31', 'Salaires', [{'account_code': '641000', 'debit': 15000}, {'account_code': '645000', 'debit': 6000},
                                        {'account_code': '421000', 'credit': 15000}, {'account_code': '431000', 'credit': 6000}])
    E('OD', '2026-12-31', 'Dotation', [{'account_code': '681100', 'debit': 1000}, {'account_code': '281830', 'credit': 1000}])
    E('OD', '2026-12-31', 'Provision litige', [{'account_code': '681500', 'debit': 2000}, {'account_code': '151100', 'credit': 2000}])
    E('BQ', '2026-12-31', 'Intérêts', [{'account_code': '661000', 'debit': 400}, {'account_code': '512000', 'credit': 400}])
    E('BQ', '2026-12-31', 'Amende', [{'account_code': '671200', 'debit': 300}, {'account_code': '512000', 'credit': 300}])
    E('OD', '2026-12-31', 'IS', [{'account_code': '695000', 'debit': 3000}, {'account_code': '444000', 'credit': 3000}])


def test_liasse_normale_balanced_and_mapped(app):
    _, _, org, _ = signup(app, 'Liasse Normale SAS')
    c = tenant(org)
    _book(c)
    l = liasse_normale(c, date(2026, 1, 1), date(2026, 12, 31))
    c.close()
    cr = l['cr']
    assert cr['FI'] == 50000 and cr['FL'] == 50000 and cr['FR'] == 50000
    assert cr['FW'] == 8000 and cr['FY'] == 15000 and cr['FZ'] == 6000 and cr['GA'] == 1000 and cr['GD'] == 2000
    assert cr['GG'] == 50000 - 8000 - 15000 - 6000 - 1000 - 2000 == 18000
    assert cr['GR'] == 400 and cr['GV'] == -400 and cr['GW'] == 17600
    assert cr['HE'] == 300 and cr['HI'] == -300 and cr['HK'] == 3000
    assert cr['HN'] == 17600 - 300 - 3000 == 14300
    a = {x['box_brut']: x for x in l['actif']}
    p = {x['box']: x['amount'] for x in l['passif']}
    assert a['AT']['brut'] == 3000 and a['AT']['amort'] == 1000 and a['AT']['net'] == 2000
    assert a['BX']['brut'] == 60000 and a['BZ']['brut'] == 2200 and a['CF']['brut'] == 31300
    assert p['DA'] == 20000 and p['DI'] == 14300 and p['DP'] == 2000 and p['DU'] == 12000
    assert p['DX'] == 9600 and p['DZ'] == 3600 and p['DY'] == 10000 + 6000 + 3000 + 15000
    assert l['balanced'] and l['totals_a']['CO_net'] == l['totals_p']['EE']
    # 2054 / 2055 / 2056
    immo = {r['label']: r for r in l['immobilisations']}
    assert immo['Autres immobilisations corporelles']['augmentations'] == 3000
    assert l['amort_tot']['dotations'] == 1000 and l['prov_tot']['dotations'] == 2000
    vh = next(d for d in l['dettes'] if d['box'] == 'VH')
    assert vh['brut'] == 12000 and vh['un_an'] == 12000  # pas d'échéancier : présumé à moins d'un an
    # 2058-A : réintégration de l'IS et des amendes
    f = l['fiscal']
    codes = {x['code']: x['amount'] for x in f['reintegrations']}
    assert codes['WA'] == 14300 and codes['I7'] == 3000 and codes['WJ'] == 300
    assert f['XN'] == 14300 + 3000 + 300 == 17600
    # 2059-E : valeur ajoutée = CA − achats (charges de personnel non déduites)
    assert l['va']['OX'] == 50000 and l['va']['OG'] == 50000 - 8000
    rows = liasse_normale_rows(l)
    assert {r['Formulaire'] for r in rows} >= {'2050', '2051', '2052', '2053', '2057', '2058-A', '2058-B', '2058-C', '2059-E'}
    assert any(r['Case'] == 'HN' and r['Montant'] == 14300 for r in rows)


def test_deficits_and_is_rates():
    assert deficit_imputable(500_000, 300_000) == 300_000
    assert deficit_imputable(5_000_000, 3_000_000) == 1_000_000 + 1_000_000
    r = CT.compute_is(100_000, date(2026, 1, 1), date(2026, 12, 31), 2_000_000, True, acomptes=5_000)
    assert r['is_total'] == 42_500 * 0.15 + 57_500 * 0.25 == 20_750 and r['solde'] == 15_750
    assert CT.compute_is(100_000, date(2026, 1, 1), date(2026, 12, 31), 2_000_000, False)['is_total'] == 25_000
    six = CT.compute_is(100_000, date(2026, 1, 1), date(2026, 6, 30), 2_000_000, True)
    assert six['plafond_taux_reduit'] == 42_500 * 6 / 12          # prorata en mois (BOI-IS-LIQ-20-20 § 50)
    assert not CT.pme_eligible(11_000_000, date(2026, 1, 1), date(2026, 12, 31), True, True)
    assert CT.is_balance_deadline(date(2026, 12, 31)) == date(2027, 5, 15)
    assert CT.is_balance_deadline(date(2026, 6, 30)) == date(2026, 10, 15)
    assert [d.isoformat() for d in CT.instalment_dates(date(2027, 1, 1), date(2027, 12, 31))] == \
        ['2027-03-15', '2027-06-15', '2027-09-15', '2027-12-15']
    assert CT.liasse_deadline(date(2025, 12, 31)) == (date(2026, 5, 5), date(2026, 5, 20))


def test_cvae_2026_schedule():
    assert CT.cvae_rate(400_000, 2026) == 0
    assert abs(CT.cvae_rate(3_000_000, 2026) - 0.0009) < 1e-12     # 0,094 % arrondi au centième : 0,09 %
    assert CT.cvae_rate(60_000_000, 2026) == 0.0028 and CT.cvae_rate(60_000_000, 2028) == 0.0019
    assert CT.cvae_rate(60_000_000, 2030) == 0
    r = CT.compute_cvae(3_000_000, 1_200_000, date(2026, 1, 1), date(2026, 12, 31))
    assert r['cvae'] == 1080 and r['taxe_additionnelle'] == round(1080 * 0.0923) and r['declaration_obligatoire']
    capped = CT.compute_cvae(1_000_000, 950_000, date(2026, 1, 1), date(2026, 12, 31))
    assert capped['va_plafonnee'] == 800_000
    assert CT.compute_cvae(600_000, 300_000, date(2026, 1, 1), date(2026, 12, 31))['cvae'] == 0  # sous 63 € : non due
    big = CT.compute_cvae(20_000_000, 8_000_000, date(2026, 1, 1), date(2026, 12, 31))
    assert len(big['acomptes_annee_suivante']) == 2


def test_pages_settings_and_is_entry(app):
    client, _, org, _ = signup(app, 'Impot SAS', plan='BUSINESS')
    c = tenant(org)
    _book(c)
    c.close()
    html = client.get('/comptabilite/liasse-2050?exercice=courant').get_data(as_text=True)
    assert '2050 — Bilan actif' in html and 'Bilan équilibré' in html
    x = client.get('/comptabilite/liasse-2050?exercice=courant&format=xlsx')
    assert x.status_code == 200 and x.data[:2] == b'PK'
    html = client.get('/comptabilite/impot-societes?exercice=courant').get_data(as_text=True)
    assert 'Impôt sur les sociétés' in html and '2 640' in html.replace('\xa0', ' ')  # 17 600 × 15 %
    post(client, '/comptabilite/impot-societes?exercice=courant',
         {'action': 'settings', 'capital_libere': '1', 'detention_pp': '1', 'deficits_anterieurs': '7 600',
          'reint_code_1': 'WE', 'reint_label_1': 'Amortissements excédentaires', 'reint_amount_1': '1 000'})
    html = client.get('/comptabilite/impot-societes?exercice=courant').get_data(as_text=True).replace('\xa0', ' ')
    assert '11 000' in html  # 17 600 + 1 000 − 7 600
    assert 'Échéances fiscales' in html and '15/05/2027' in html


def test_is_entry_generated_once(app):
    _, _, org, _ = signup(app, 'Ecriture IS SAS')
    c = tenant(org)
    eid = CT.generate_is_entry(c, date(2026, 12, 31), 2640)
    assert eid
    import pytest
    from profitos.accounting import AccountingError
    with pytest.raises(AccountingError):
        CT.generate_is_entry(c, date(2026, 12, 31), 2640)
    row = c.execute("SELECT SUM(debit) d FROM accounting_entry_lines WHERE account_code='695000'").fetchone()
    c.close()
    assert row['d'] == 2640


def test_second_year_opening_and_unallocated_result(app):
    from profitos.accounting import generate_opening_entries
    _, _, org, _ = signup(app, 'Deuxieme Exercice SAS')
    c = tenant(org)
    _book(c)
    generate_opening_entries(c, '2026-01-01', '2026-12-31')
    create_entry(c, 'VE', '2027-03-01', 'Vente', [{'account_code': '411000', 'debit': 1200, 'auxiliary_name': 'C'},
                                                 {'account_code': '706000', 'credit': 1000},
                                                 {'account_code': '445710', 'credit': 200, 'vat_rate': 20.0, 'vat_base': 1000}])
    l = liasse_normale(c, date(2027, 1, 1), date(2027, 12, 31), previous=(date(2026, 1, 1), date(2026, 12, 31)))
    c.close()
    immo = {r['label']: r for r in l['immobilisations']}['Autres immobilisations corporelles']
    assert immo['debut'] == 3000 and immo['augmentations'] == 0
    p = {x['box']: x['amount'] for x in l['passif']}
    assert p['DI'] == l['cr']['HN'] == 1000 and p['DH'] == 14300
    assert l['previous']['cr']['HN'] == 14300 and l['balanced']
    assert any('non encore affecté' in m for m in l['checks'])


def test_2057_loan_split_follows_schedule(app):
    from profitos.loans import create_loan
    _, _, org, _ = signup(app, 'Echeances SAS')
    c = tenant(org)
    create_loan(c, 'Banque', 12000, 0, '2026-01-01', 60)  # 200 €/mois sans intérêt
    l = liasse_normale(c, date(2026, 1, 1), date(2026, 12, 31))
    c.close()
    vh = next(d for d in l['dettes'] if d['box'] == 'VH')
    assert vh['brut'] == 12000  # aucune échéance réglée dans ProfitOS
    assert vh['un_an'] + vh['un_cinq'] + vh['plus_cinq'] == 12000
    assert 2000 <= vh['un_an'] <= 3000 and vh['plus_cinq'] == 0


def test_no_single_percent_like_literal_in_sql():
    """Sous PostgreSQL (psycopg2), un « % » littéral dans une requête paramétrée casse la
    requête : passer le motif en paramètre ou doubler le « % »."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'profitos'
    pat = re.compile(r"LIKE '[^'%]*[^%']%'")
    bad = []
    for f in root.rglob('*.py'):
        if f.name == 'gdpr_export.py':  # requête propre à SQLite (sqlite_master)
            continue
        for n, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1):
            if pat.search(line):
                bad.append(f'{f.name}:{n}')
    assert not bad, bad
