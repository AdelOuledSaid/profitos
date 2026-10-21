"""v473-v474 — La veille au service des marges et des factures : suivi des marges alimenté
par les indices officiels, ligne de révision de prix ajoutée à une facture brouillon."""
import json
import re

import profitos.runtime as rt
from profitos import market_watch as MW
from test_v470_encaissement import app, flashes, post, signup  # noqa: F401


def seed_bt01(org):
    c = rt.tenant_cx_direct(org)
    MW.save_settings(c, None, dict(MW.DEFAULT_SETTINGS, sector='batiment'), rt.now())
    c.close()
    ac = rt.auth_cx()
    MW.store(ac, 'BT01', {'title': 'Index BT01', 'freq': 'M', 'last_update': None,
                          'obs': [('2025-01', 130.0), ('2025-12', 134.0), ('2026-01', 140.0)]})
    ac.close()


def test_suivi_des_marges_sur_indice_de_la_veille(app):
    cl, org = signup(app)
    seed_bt01(org)
    page = cl.get('/margin-watch').get_data(as_text=True)
    assert "Index bâtiment tous corps d&#39;état (BT01)" in page            # proposé parmi les indices de la veille
    post(cl, '/margin-watch', {'form_type': 'index_name', 'veille_code': 'BT01'})
    assert any('BT01' in m for m in flashes(cl))
    post(cl, '/margin-watch', {'form_type': 'contract', 'project_name': 'Chantier Dupont', 'customer': 'Dupont',
                               'amount': '100000', 'signed_date': '2025-02-15', 'materials_share_pct': '30'})
    page = cl.get('/margin-watch').get_data(as_text=True)
    # Indice à la signature : janvier 2025 (130) ; dernier : janvier 2026 (140) → +7,7 % sur 30 % de 100 000 €.
    assert 'Chantier Dupont' in page and '130.0 → 140.0' in page and '+7.7%' in page
    assert '2' + ' ' + '308' in page or '2 308' in page or '2\xa0308' in page
    assert 'rien à saisir' in page
    post(cl, '/margin-watch', {'form_type': 'reading', 'reading_date': '2026-02-01', 'value': '141'})
    assert any('automatiquement' in m for m in flashes(cl))
    # Indice inconnu de la veille : refusé, retour à un nom saisi.
    post(cl, '/margin-watch', {'form_type': 'index_name', 'veille_code': 'INCONNU', 'index_name': 'Syntec'})
    page = cl.get('/margin-watch').get_data(as_text=True)
    assert 'érosion de marge (Syntec)' in page
    post(cl, '/margin-watch', {'form_type': 'index_name', 'index_name': 'VEILLE:BT01'})   # préfixe forgé : ignoré
    assert 'érosion de marge (INDICE)' in cl.get('/margin-watch').get_data(as_text=True)


def draft(cl, price='1000'):
    r = post(cl, '/facturation/nouvelle', {'client_name': 'Client Durand', 'client_address': '2 rue X\n75002 Paris',
                                           'client_siren': '732829320', 'client_email': 'c@example.com', 'due_date': '2026-12-31',
                                           'service_date': '2026-10-01', 'operation_nature': 'services',
                                           'label_1': 'Situation de travaux n°3', 'qty_1': '1', 'price_1': price, 'vat_1': '20'})
    return int(re.search(r'/facturation/(\d+)', r.headers['Location']).group(1))


def test_ligne_de_revision_sur_facture_brouillon(app):
    cl, org = signup(app)
    seed_bt01(org)
    inv = draft(cl)
    page = cl.get('/veille/revision?code=BT01&amount=10000&base=2025-01&new=2026-01&fixed=15&decimals=4').get_data(as_text=True)
    assert 'Brouillon n°' in page and '1,0654' in page
    data = {'code': 'BT01', 'amount': '10000', 'base': '2025-01', 'new': '2026-01', 'fixed': '15', 'decimals': '4',
            'invoice_id': str(inv), 'vat_rate': '20'}
    r = post(cl, '/veille/revision/facture', data)
    assert r.headers['Location'].endswith(f'/facturation/{inv}')
    c = rt.tenant_cx_direct(org)
    row = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (inv,)).fetchone()
    c.close()
    items = json.loads(row['line_items'])
    # 10 000 × (0,15 + 0,85 × 140 / 130) = 10 000 × 1,0654 → écart 654,00 € HT.
    assert items[-1]['line_total'] == 654.0 and 'janvier 2025 → janvier 2026' in items[-1]['label']
    assert row['subtotal'] == 1654.0 and row['total'] == round(1654 * 1.2, 2)
    # Facture émise : refus.
    post(cl, f'/facturation/{inv}/marquer-emise')
    flashes(cl)
    post(cl, '/veille/revision/facture', data)
    assert any('brouillon' in m for m in flashes(cl))
    # Baisse de l'indice qui rendrait le total négatif : refusée.
    small = draft(cl, price='100')
    post(cl, '/veille/revision/facture', dict(data, invoice_id=str(small), base='2026-01', new='2025-01', amount='100000'))
    assert any('positif' in m for m in flashes(cl))
    # Une autre entreprise ne peut pas viser ce brouillon.
    other, _ = signup(app)
    seed_bt01(_)
    post(other, '/veille/revision/facture', dict(data, invoice_id=str(small)))
    assert any('brouillon' in m for m in flashes(other))
