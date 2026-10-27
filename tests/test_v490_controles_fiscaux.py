"""v490 — Contrôles fiscaux croisés (déclaré ↔ comptabilisé) et dossier de validation pour
l'expert-comptable ; constats remontés dans Mission Control."""
import json
from datetime import date

import pytest

import profitos.runtime as rt
from profitos import fiscal_controls as FC
from profitos import mission_control as MC
from profitos.accounting import create_entry
from test_v458_dossiers_reference import E, VAT, app as ref_app, tenant  # noqa: F401
from test_v470_encaissement import app, signup  # noqa: F401

TODAY = date(2026, 7, 1)


def by_code(review):
    return {ck['code']: ck for s in review['sections'] for ck in s['checks']}


def declare(c, start, end, l16, status='declared', mode='manual'):
    payload = {'type': 'TVA_CA3', 'boxes': [{'code': '16', 'amount': l16}, {'code': '19', 'amount': 0}, {'code': '20', 'amount': 0}]}
    c.execute("""INSERT INTO teledeclarations(entity_id,decl_type,form,channel,period_start,period_end,payload_json,payload_hash,
                 status,mode,history_json) VALUES(NULL,'TVA_CA3','3310-CA3','EFI',?,?,?,'h',?,?,'[]')""",
              (start, end, json.dumps(payload), status, mode))
    c.commit()


def dossier(c):
    E(c, 'BQ', '2025-01-02', 'Capital', ('512000', 10000, 0, None), ('101300', 0, 10000, None))
    E(c, 'VE', '2025-06-30', 'Prestations', ('411000', 144000, 0, 'Clients'), ('706000', 0, 120000, None), VAT(24000, 20.0, 120000))
    E(c, 'VE', '2025-09-30', 'Export hors UE', ('411000', 50000, 0, 'Export'), ('706000', 0, 50000, None))
    E(c, 'AC', '2025-03-01', 'Fournitures', ('606400', 30000, 0, None), ('445660', 6000, 0, None), ('401000', 0, 36000, 'Fourn'))
    # TVA mal calculée : 1 000 € pour une base de 10 000 € à 20 %
    E(c, 'VE', '2025-11-30', 'Vente', ('411000', 11000, 0, 'Clients'), ('706000', 0, 10000, None), VAT(1000, 20.0, 10000))
    # Janvier 2026 : déclarée 2 000 €, puis une vente ajoutée après le dépôt
    E(c, 'VE', '2026-01-10', 'Vente janvier', ('411000', 12000, 0, 'Clients'), ('706000', 0, 10000, None), VAT(2000, 20.0, 10000))
    declare(c, '2026-01-01', '2026-01-31', 2000)
    E(c, 'VE', '2026-01-20', 'Vente oubliée', ('411000', 3000, 0, 'Clients'), ('706000', 0, 2500, None), VAT(500, 20.0, 2500))


def test_ecarts_tva_detectes_et_expliques(ref_app):
    c = tenant(ref_app)
    dossier(c)
    r = FC.review(c, None, TODAY)
    ck = by_code(r)
    decl = ck['tva_decl:2026-01-01']
    assert decl['ok'] is False and decl['amount'] == 500
    detail = decl['detail'].replace('\xa0', ' ').replace('\u202f', ' ')
    assert 'déclaré 2 000,00 €' in detail and 'écritures 2 500,00 €' in detail
    assert 'rectificative' in decl['fix']
    assert ck['tva_taux']['ok'] is None and 'TVA comptabilisée 25 000,00 €' in ck['tva_taux']['detail'].replace('\xa0', ' ').replace('\u202f', ' ')
    assert ck['tva_ca']['ok'] is None and "sans TVA collectée" in ck['tva_ca']['detail']
    assert ck['tva_ca']['amount'] == 50000
    assert ck['tva_liquidation']['ok'] is False          # 2025 jamais liquidée
    assert r['level'] == 'bloque' and r['counts']['blocked'] >= 2
    c.close()


def test_recapitulatif_prepare_seulement_a_mettre_a_jour(ref_app):
    c = tenant(ref_app)
    E(c, 'VE', '2026-02-10', 'Vente', ('411000', 1200, 0, 'C'), ('706000', 0, 1000, None), VAT(200, 20.0, 1000))
    declare(c, '2026-02-01', '2026-02-28', 100, status='prepared')
    ck = by_code(FC.review(c, None, TODAY))['tva_decl:2026-02-01']
    assert ck['ok'] is None and 'mettre à jour' in ck['label']
    declare(c, '2026-02-01', '2026-02-28', 200, status='declared')   # dépôt conforme, version la plus récente
    assert by_code(FC.review(c, None, TODAY))['tva_decl:2026-02-01']['ok'] is True
    c.close()


def test_simulation_jamais_comparee(ref_app):
    c = tenant(ref_app)
    E(c, 'VE', '2026-03-10', 'Vente', ('411000', 1200, 0, 'C'), ('706000', 0, 1000, None), VAT(200, 20.0, 1000))
    declare(c, '2026-03-01', '2026-03-31', 0, status='accepted', mode='simulation')
    assert 'tva_decl:2026-03-01' not in by_code(FC.review(c, None, TODAY))
    c.close()


def test_tva_a_decaisser_restee_impayee(ref_app):
    c = tenant(ref_app)
    create_entry(c, 'OD', '2026-02-28', 'Liquidation TVA', [{'account_code': '445710', 'debit': 2000, 'credit': 0},
                                                            {'account_code': '445510', 'debit': 0, 'credit': 2000}],
                 source_type='vat_liquidation', source_id=20260228)
    create_entry(c, 'OD', '2026-03-31', 'Liquidation TVA', [{'account_code': '445710', 'debit': 800, 'credit': 0},
                                                            {'account_code': '445510', 'debit': 0, 'credit': 800}],
                 source_type='vat_liquidation', source_id=20260331)
    ck = by_code(FC.review(c, None, TODAY))['tva_impayee']
    assert ck['ok'] is None and ck['amount'] == 2000
    create_entry(c, 'BQ', '2026-03-20', 'Paiement TVA février', [{'account_code': '445510', 'debit': 2000, 'credit': 0},
                                                                {'account_code': '512000', 'debit': 0, 'credit': 2000}])
    assert by_code(FC.review(c, None, TODAY))['tva_impayee']['ok'] is True
    c.close()


def test_acomptes_is_exigibles_non_verses(ref_app):
    c = tenant(ref_app)
    dossier(c)
    r = FC.review(c, None, TODAY)
    assert r['is'] and r['is']['is_total'] > 3000
    ck = by_code(r)['is_acomptes']
    assert ck['ok'] is None and ck['amount'] > 0 and '2 acompte(s)' in ck['detail']
    # Avant l'échéance de la liasse, le seul acompte exigible repose sur N-2 : pas de comparaison
    assert 'is_acomptes' not in by_code(FC.review(c, None, date(2026, 4, 1)))
    c.close()


def test_liasse_et_cloture_dans_le_dossier(ref_app):
    c = tenant(ref_app)
    dossier(c)
    r = FC.review(c, None, TODAY)
    keys = [s['key'] for s in r['sections']]
    assert keys == ['tva', 'is', 'liasse', 'cloture']
    ck = by_code(r)
    assert ck['liasse_equilibre']['ok'] is True
    assert any("impôt sur les sociétés" in x['label'] for x in r['sections'][3]['checks'])
    assert not any(x['label'].startswith('Comptes de TVA') for x in r['sections'][3]['checks'])  # pas de doublon
    c.close()


def test_constats_mission_control(ref_app):
    c = tenant(ref_app)
    dossier(c)
    found = FC.detect(c, None, TODAY)
    keys = {f['key'] for f in found}
    assert 'fiscal_controle:tva_decl:2026-01-01' in keys and 'fiscal_controle:is_acomptes' in keys
    assert 'fiscal_controle:tva_ca' not in keys            # simple justification : pas d'alerte
    f = next(f for f in found if f['key'] == 'fiscal_controle:tva_decl:2026-01-01')
    assert f['level'] == 'bloque' and f['action']['endpoint'] == 'fiscal_controls'
    findings, evaluated = MC.compute(c, None, TODAY, accounting=True)
    assert 'fiscal_controle' in evaluated and any(x['kind'] == 'fiscal_controle' for x in findings)
    findings, evaluated = MC.compute(c, None, TODAY, accounting=False)
    assert 'fiscal_controle' not in evaluated
    c.close()


def test_pages_controles_et_dossier(app):
    cl, org = signup(app)
    r = cl.get('/comptabilite/controles-fiscaux')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Contrôles fiscaux' in html and 'ProfitOS prépare, il ne transmet pas seul' in html
    d = cl.get('/comptabilite/controles-fiscaux/dossier')
    assert d.status_code == 200 and 'no-store' in d.headers.get('Cache-Control', '')
    body = d.get_data(as_text=True)
    assert 'Entreprise</td><td>—' not in body                     # nom de l'organisation à défaut de fiche
    assert "Validation de l'expert-comptable" in body and 'data-print' in body and '<style' not in body
