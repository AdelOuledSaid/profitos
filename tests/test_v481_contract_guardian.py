"""v481 — Contract Guardian : contrats fournisseurs, écarts de prix (indexés sur la veille),
préavis de résiliation, factures hors contrat, hausses sans contrat, doublons."""
import re
from datetime import date, timedelta

import pytest

import profitos.runtime as rt
from profitos import contract_guardian as CG
from profitos import market_watch as MW
from profitos import verification as V
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, post, signup  # noqa: F401

T = paris_today()


def purchase(org, supplier, number, days_ago, ht, ttc=None, supplier_id=None):
    c = rt.tenant_cx_direct(org)
    d = (T - timedelta(days=days_ago)).isoformat()
    cur = c.execute("""INSERT INTO purchase_invoices(supplier_id,supplier_name,invoice_number,issue_date,due_date,subtotal,
                       vat_amount,total,status,created_at,validation_status,entity_id)
                       VALUES(?,?,?,?,?,?,?,?,'unpaid',?,'approved',NULL)""",
                    (supplier_id, supplier, number, d, d, ht, round((ttc or ht * 1.2) - ht, 2), ttc or round(ht * 1.2, 2), rt.now()))
    c.commit()
    pid = cur.lastrowid
    c.close()
    return pid


def text_of(cl, url):
    html = cl.get(url).get_data(as_text=True)
    return html, re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace('&#39;', "'").replace(' ', ' ').replace('\xa0', ' ')


# --------------------------------------------------------------------- unitaires
def test_noms_et_calendrier():
    assert CG.supplier_key('Orange Business SAS') == CG.supplier_key('ORANGE BUSINESS') == 'orange business'
    assert CG.supplier_key('Sté Électricité Dupont SARL') == 'electricite dupont'
    assert CG.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    k = {'end_date': '2025-03-31', 'tacit_renewal': 1, 'renewal_months': 12, 'notice_days': 90, 'status': 'actif'}
    end, deadline = CG.current_term(k, date(2026, 10, 26))
    assert end == date(2027, 3, 31) and deadline == date(2026, 12, 31)
    assert CG.current_term({'end_date': None}, T) == (None, None)
    fixed = {'start_date': '2026-01-01', 'end_date': '2026-06-30', 'tacit_renewal': 0, 'status': 'actif'}
    assert CG.covers(fixed, date(2026, 3, 1), T) and not CG.covers(fixed, date(2026, 7, 2), T)
    assert not CG.covers(fixed, date(2025, 12, 1), T)


@pytest.mark.parametrize('data, message', [
    ({'supplier_name': '', 'label': 'x'}, 'obligatoires'),
    ({'supplier_name': 'A', 'label': 'x', 'periodicity': 'hebdo'}, 'Périodicité'),
    ({'supplier_name': 'A', 'label': 'x', 'periodicity': 'mensuel', 'amount_ht': -1}, 'positifs'),
    ({'supplier_name': 'A', 'label': 'x', 'periodicity': 'mensuel', 'tacit_renewal': '1'}, 'date de fin'),
    ({'supplier_name': 'A', 'label': 'x', 'periodicity': 'mensuel', 'index_code': 'DFR'}, 'indice de prix'),
    ({'supplier_name': 'A', 'label': 'x', 'periodicity': 'mensuel', 'index_code': 'CA_HOTELS'}, 'indice de prix'),
    ({'supplier_name': 'A', 'label': 'x', 'periodicity': 'mensuel', 'index_code': 'BT01'}, 'période de référence'),
    ({'supplier_name': 'A', 'label': 'x', 'periodicity': 'mensuel', 'start_date': '2026-05-01', 'end_date': '2026-01-01'}, 'précède'),
])
def test_saisie_controlee(data, message):
    with pytest.raises(CG.ContractError, match=message):
        CG.clean(data)


# ------------------------------------------------------------- écarts de prix
def test_ecart_prix_fixe_et_tolerance(app):
    cl, org = signup(app)
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'Maintenix SAS', 'label': 'Maintenance informatique',
                                                'periodicity': 'mensuel', 'amount_ht': '500,00', 'tolerance_pct': '2',
                                                'start_date': (T - timedelta(days=300)).isoformat()})
    assert any('Contrat enregistré' in m for m in flashes(cl))
    ok = purchase(org, 'MAINTENIX', 'M-1', 60, 505.0)          # +1 % : dans la tolérance
    high = purchase(org, 'Maintenix', 'M-2', 30, 560.0)        # +12 %
    other = purchase(org, 'Autre SARL', 'X-1', 20, 9999.0)
    html, text = text_of(cl, '/contrats-fournisseurs')
    assert 'Facture M-2 (Maintenix) : 60,00 € HT au-dessus du contrat' in text and 'M-1' not in text and 'X-1' not in text
    assert '+60,00 € HT soit +12 % (tolérance 2 %)' in text
    assert '60,00 € HT facturés au-delà de vos contrats' in text
    # Le même constat apparaît dans Mission Control.
    _h, mc = text_of(cl, '/mission-control')
    assert 'Facture M-2 (Maintenix) : 60,00 € HT au-dessus du contrat' in mc and 'Contrats fournisseurs' in mc


def test_ecart_indexe_sur_la_veille(app):
    cl, org = signup(app)
    ac = rt.auth_cx()
    MW.store(ac, 'BT01', {'title': "Index BT01", 'freq': 'M', 'last_update': None,
                          'obs': [('2026-01', 130.0), ('2026-06', 136.5)]})
    ac.close()
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'Bati Pro', 'label': 'Entretien des locaux', 'periodicity': 'mensuel',
                                                'amount_ht': '1000', 'tolerance_pct': '1', 'index_code': 'BT01',
                                                'index_base_period': '2026-01', 'start_date': '2026-01-01'})
    flashes(cl)
    c = rt.tenant_cx_direct(org)
    c.execute("""INSERT INTO purchase_invoices(supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,
                 created_at,validation_status,entity_id) VALUES('Bati Pro','B-7','2026-07-05','2026-08-05',1050,210,1260,'unpaid',?,'approved',NULL),
                 ('Bati Pro','B-8','2026-08-05','2026-09-05',1100,220,1320,'unpaid',?,'approved',NULL)""", (rt.now(), rt.now()))
    c.commit(); c.close()
    _h, text = text_of(cl, '/contrats-fournisseurs')
    # Indice +5 % (136,5 / 130) → 1 050 € attendus : B-7 conforme, B-8 au-dessus de 50 €.
    assert 'B-7' not in text and 'Facture B-8 (Bati Pro) : 50,00 € HT au-dessus du contrat' in text
    assert 'juin 2026 = 136.5 pour janvier 2026 = 130' in text
    # Indice suspendu : plus de conclusion, « comparaison impossible ».
    ac = rt.auth_cx()
    MW.store(ac, 'BT01', error='Métadonnées contradictoires : x — ' + MW.SUSPENDED)
    ac.close()
    _h, text = text_of(cl, '/contrats-fournisseurs')
    assert 'comparaison au contrat impossible' in text and 'indice suspendu ou non mis à jour' in text
    assert 'au-dessus du contrat' not in text


# ---------------------------------------------------------- préavis et périodes
def test_preavis_et_facture_hors_contrat(app):
    cl, org = signup(app)
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'Télécom Plus', 'label': 'Forfait mobile', 'periodicity': 'mensuel',
                                                'amount_ht': '80', 'start_date': '2024-01-01',
                                                'end_date': (T + timedelta(days=40)).isoformat(), 'tacit_renewal': '1',
                                                'renewal_months': '12', 'notice_days': '30'})
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'Loc Matériel', 'label': 'Location nacelle', 'periodicity': 'mensuel',
                                                'amount_ht': '300', 'start_date': '2026-01-01',
                                                'end_date': (T - timedelta(days=60)).isoformat()})
    flashes(cl)
    purchase(org, 'Loc Matériel', 'L-9', 10, 300.0)
    _h, text = text_of(cl, '/contrats-fournisseurs')
    deadline = (T + timedelta(days=10)).strftime('%d/%m/%Y')
    assert f"Contrat « Forfait mobile » (Télécom Plus) : résiliation possible jusqu'au {deadline}" in text
    assert 'Critique' in text                                   # 10 jours : urgent
    assert 'Facture L-9 (Loc Matériel) datée après la fin du contrat « Location nacelle »' in text
    # Résilié : plus de préavis.
    c = rt.tenant_cx_direct(org)
    kid = c.execute("SELECT id FROM supplier_contracts WHERE label='Forfait mobile'").fetchone()['id']
    c.close()
    html, _t = text_of(cl, f'/contrats-fournisseurs/{kid}')
    assert 'Forfait mobile' in html and 'Supprimer ce contrat' in html
    post(cl, f'/contrats-fournisseurs/{kid}', {'supplier_name': 'Télécom Plus', 'label': 'Forfait mobile', 'periodicity': 'mensuel',
                                              'amount_ht': '80', 'start_date': '2024-01-01',
                                              'end_date': (T + timedelta(days=40)).isoformat(), 'tacit_renewal': '1',
                                              'renewal_months': '12', 'notice_days': '30', 'status': 'resilie',
                                              'terminated_on': T.isoformat()})
    flashes(cl)
    _h, text = text_of(cl, '/contrats-fournisseurs')
    assert 'résiliation possible' not in text and '(résilié le' in text


# ---------------------------------------------------- hausses, doublons, saisie
def test_hausse_sans_contrat_et_doublons(app):
    cl, org = signup(app)
    for i, (days, ttc) in enumerate([(150, 120.0), (120, 120.0), (90, 120.0), (60, 120.0), (30, 150.0)]):
        purchase(org, 'CloudSoft', f'C-{i}', days, round(ttc / 1.2, 2), ttc)
    a = purchase(org, 'Imprim Express', 'I-100', 12, 400.0)
    b = purchase(org, 'Imprim Express', 'I-101', 8, 400.0)
    html, text = text_of(cl, '/contrats-fournisseurs')
    assert 'CloudSoft : dernière facture 150,00 € TTC pour 120,00 € habituellement' in text
    assert 'Aucun contrat enregistré pour ce fournisseur' in text and 'Enregistrer le contrat' in text
    assert '/contrats-fournisseurs/nouveau?fournisseur=CloudSoft' in html
    assert 'Doublon possible : Imprim Express, factures I-100 et I-101' in text
    assert "même montant 480,00 € TTC à 4 jour(s) d'écart" in text
    # Le lien pré-remplit le fournisseur ; avec un contrat, la hausse n'est plus « sans contrat ».
    form = cl.get('/contrats-fournisseurs/nouveau?fournisseur=CloudSoft').get_data(as_text=True)
    assert 'value="CloudSoft"' in form
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'CloudSoft', 'label': 'Logiciel', 'periodicity': 'mensuel',
                                                'amount_ht': '125', 'tolerance_pct': '0'})
    flashes(cl)
    _h, text = text_of(cl, '/contrats-fournisseurs')
    assert 'habituellement' not in text and 'Facture C-4 (CloudSoft) : 0,00 € HT' not in text
    # Saisie invalide : message, code 400.
    r = post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'X', 'label': 'Y', 'periodicity': 'mensuel',
                                                    'amount_ht': 'abc'})
    assert r.status_code == 400 and 'Montant ou pourcentage invalide' in r.get_data(as_text=True)


def test_isolement_et_suivi(app):
    cl, org = signup(app)
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'Maintenix', 'label': 'Maintenance', 'periodicity': 'mensuel',
                                                'amount_ht': '500'})
    flashes(cl)
    cl2, org2 = signup(app)
    assert 'Maintenance' not in cl2.get('/contrats-fournisseurs').get_data(as_text=True)
    c = rt.tenant_cx_direct(org)
    kid = c.execute('SELECT id FROM supplier_contracts').fetchone()['id']
    c.close()
    assert cl2.get(f'/contrats-fournisseurs/{kid}').status_code == 404
    # « J'ai traité » depuis la page contrats : retour sur la page contrats.
    pid = purchase(org, 'Maintenix', 'M-9', 5, 700.0)
    cl.get('/contrats-fournisseurs')
    r = post(cl, '/mission-control/suivi', {'key': f'contrat:ecart:{pid}', 'op': 'fait', 'next': '/contrats-fournisseurs'})
    assert r.headers['Location'].endswith('/contrats-fournisseurs')
    _h, text = text_of(cl, '/contrats-fournisseurs')
    assert 'Traité, résultat en contrôle (1)' in text
    r = post(cl, '/mission-control/suivi', {'key': f'contrat:ecart:{pid}', 'op': 'reporter', 'next': 'https://evil.example'})
    assert r.headers['Location'].endswith('/mission-control')
    # Supprimer.
    post(cl, f'/contrats-fournisseurs/{kid}/supprimer')
    assert any('Contrat supprimé' in m for m in flashes(cl))
