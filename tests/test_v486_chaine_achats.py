"""v486 — Chaîne de traitement des factures fournisseurs : dix étapes, validation humaine,
exécution, contrôle du résultat."""
import re
from datetime import timedelta

import profitos.runtime as rt
from profitos import purchase_pipeline as PP
from profitos import verification as V
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, post, signup  # noqa: F401
from test_v480_mission_control import bank, cash  # noqa: F401

T = paris_today()


def supplier(org, name='Matériaux Sud', vat='FR44732829320', siret='73282932000074'):
    c = rt.tenant_cx_direct(org)
    cur = c.execute('INSERT INTO suppliers(name,siret,vat_number,created_at) VALUES(?,?,?,?)', (name, siret, vat, rt.now()))
    c.commit()
    sid = cur.lastrowid
    c.close()
    return sid


def purchase(org, number='MS-1', ht=1000.0, vat=200.0, total=None, status='pending', category='materiel', supplier_name='Matériaux Sud',
             supplier_id=None, doc='abc.pdf', days_ago=5, due_in=25):
    c = rt.tenant_cx_direct(org)
    cur = c.execute("""INSERT INTO purchase_invoices(supplier_id,supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,
                       status,created_at,document_path,category,validation_status,entity_id)
                       VALUES(?,?,?,?,?,?,?,?,'unpaid',?,?,?,?,NULL)""",
                    (supplier_id, supplier_name, number, (T - timedelta(days=days_ago)).isoformat(), (T + timedelta(days=due_in)).isoformat(),
                     ht, vat, total if total is not None else round(ht + vat, 2), rt.now(), doc, category, status))
    c.commit()
    pid = cur.lastrowid
    c.close()
    return pid


def analyse(org, pid):
    c = rt.tenant_cx_direct(org)
    a = PP.analyse(c, None, pid, T)
    c.close()
    return a


def step(a, n):
    return a['steps'][n - 1]


def text(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace('&#39;', "'").replace(' ', ' ').replace('\xa0', ' ')


# ----------------------------------------------------------- parcours nominal
def test_facture_conforme_validee_comptabilisee_et_controlee(app):
    cl, org = signup(app)
    sid = supplier(org)
    bank(org, 0, balance=5000.0)
    pid = purchase(org, supplier_id=sid)
    a = analyse(org, pid)
    assert [s['level'] for s in a['steps'][:7]] == ['pret'] * 7, [(s['name'], s['checks']) for s in a['steps'] if s['level'] != 'pret']
    assert a['actions'][0]['op'] == 'approve' and a['actions'][0]['allowed']
    assert [l['account_code'] for l in a['proposed_entry']][1:] == ['445660', '401000']
    html = cl.get(f'/facturation/achats/{pid}/traitement').get_data(as_text=True)
    t = text(html)
    for name in PP.STEP_NAMES:
        assert name in t, name
    assert 'Écriture préparée : elle sera passée à la validation' in t and 'Valider et comptabiliser' in t
    post(cl, f'/facturation/achats/{pid}/traitement', {'op': 'approve'})
    msg = flashes(cl)
    assert any('écriture du' in m and 'passée et équilibrée, facture validée' in m for m in msg), msg
    a = analyse(org, pid)
    assert a['purchase']['validation_status'] == 'approved' and a['entry'] is not None
    assert step(a, 5)['checks'][0]['label'] == 'Écriture passée et cohérente avec la facture' and step(a, 5)['level'] == 'pret'
    assert step(a, 10)['checks'][0]['ok'] is True and a['log'][0]['ok'] == 1
    # Rejouer l'action : refusée (plus proposée).
    post(cl, f'/facturation/achats/{pid}/traitement', {'op': 'approve'})
    assert any("n'est plus proposée" in m for m in flashes(cl))


# ------------------------------------------------------------------ anomalies
def test_doublon_bloque_la_validation(app):
    cl, org = signup(app)
    sid = supplier(org)
    purchase(org, number='MS-9', supplier_id=sid, status='approved')
    pid = purchase(org, number='ms-9', supplier_id=sid)
    a = analyse(org, pid)
    assert step(a, 3)['level'] == 'bloque' and a['level'] == 'bloque'
    assert not next(x for x in a['actions'] if x['op'] == 'approve')['allowed']
    assert any(x['op'] == 'reject' for x in a['actions'])
    post(cl, f'/facturation/achats/{pid}/traitement', {'op': 'approve'})
    assert any('Action non lancée' in m and 'Doublons' in m for m in flashes(cl))
    assert analyse(org, pid)['entry'] is None


def test_mentions_montants_et_taux(app):
    cl, org = signup(app)
    pid = purchase(org, number='X-1', ht=1000.0, vat=200.0, total=1250.0, doc=None)
    a = analyse(org, pid)
    s2 = {ch['label']: ch for ch in step(a, 2)['checks']}
    assert s2['Montants cohérents (HT + TVA = TTC)']['ok'] is False
    assert s2['Fournisseur identifié (SIRET ou n° de TVA)']['ok'] is None
    assert step(a, 1)['checks'][1]['ok'] is None and "TVA n'est pas déductible" in step(a, 1)['checks'][1]['fix']
    assert any(x['op'] == 'attach' for x in a['actions'])
    odd = analyse(org, purchase(org, number='X-2', ht=1000.0, vat=150.0))
    rate = next(ch for ch in step(odd, 2)['checks'] if ch['label'] == 'Taux de TVA plausible')
    assert rate['ok'] is None and 'Taux apparent 15 %' in rate['detail']


def test_ecart_au_contrat(app):
    cl, org = signup(app)
    sid = supplier(org, name='Maintenix')
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_name': 'Maintenix', 'label': 'Maintenance', 'periodicity': 'mensuel',
                                                'amount_ht': '500', 'tolerance_pct': '2'})
    flashes(cl)
    pid = purchase(org, number='MX-3', ht=560.0, vat=112.0, supplier_name='Maintenix', supplier_id=sid, category='sous_traitance')
    a = analyse(org, pid)
    chk = next(ch for ch in step(a, 4)['checks'] if ch['label'] == 'Montant conforme au contrat')
    assert chk['ok'] is False and 'Écart : 60,00 € HT' in chk['detail'].replace(' ', ' ')
    assert any(x['op'] == 'dispute' for x in a['actions']) and not a['actions'][0]['allowed']


def test_traitements_de_tva(app):
    cl, org = signup(app)
    de = supplier(org, name='Bauteile GmbH', vat='DE123456789', siret='')
    auto = analyse(org, purchase(org, number='DE-1', ht=2000.0, vat=0.0, supplier_name='Bauteile GmbH', supplier_id=de))
    s6 = step(auto, 6)
    assert auto['treatment'] == 'autoliquidation' and s6['level'] == 'a_verifier'
    assert "établi dans l'UE (DE)" in s6['checks'][0]['detail'] and '4452' in s6['checks'][0]['fix']
    btp = analyse(org, purchase(org, number='ST-1', ht=3000.0, vat=0.0, category='sous_traitance', supplier_id=supplier(org, 'Sous-Trait')
                                , supplier_name='Sous-Trait'))
    assert btp['treatment'] == 'autoliquidation' and '283, 2 nonies' in step(btp, 6)['checks'][0]['detail']
    fuel = analyse(org, purchase(org, number='CB-1', ht=100.0, vat=20.0, category='carburant'))
    assert fuel['treatment'] == 'carburant' and '80 %' in step(fuel, 6)['checks'][0]['detail']
    saas = analyse(org, purchase(org, number='SA-1', ht=50.0, vat=10.0, category='logiciel_saas'))
    assert saas['treatment'] == 'services' and 'déductible à la date du paiement' in step(saas, 6)['checks'][-1]['detail']
    franchise = analyse(org, purchase(org, number='FR-0', ht=300.0, vat=0.0, category='autre'))
    assert franchise['treatment'] == 'sans_tva' and step(franchise, 6)['level'] == 'pret'


def test_periode_close_et_ecriture_manquante(app):
    cl, org = signup(app)
    c = rt.tenant_cx_direct(org)
    c.execute('INSERT INTO accounting_entity_closure(entity_key,closed_until) VALUES(0,?)', (T.isoformat(),))
    c.commit(); c.close()
    pid = purchase(org, number='CL-1')
    a = analyse(org, pid)
    assert step(a, 5)['checks'][0]['label'] == 'Période comptable ouverte' and step(a, 5)['level'] == 'bloque'
    # Validée sans écriture : action « Générer l'écriture manquante » proposée.
    c = rt.tenant_cx_direct(org)
    c.execute('DELETE FROM accounting_entity_closure')
    c.commit(); c.close()
    pid2 = purchase(org, number='NE-1', status='approved', supplier_id=supplier(org, 'Autre Fourn'), supplier_name='Autre Fourn')
    a = analyse(org, pid2)
    assert step(a, 5)['level'] == 'bloque'
    act = next(x for x in a['actions'] if x['op'] == 'post_entry')
    assert act['allowed']
    post(cl, f'/facturation/achats/{pid2}/traitement', {'op': 'post_entry'})
    assert any("Générer l'écriture manquante : écriture du" in m for m in flashes(cl))
    assert analyse(org, pid2)['entry'] is not None


def test_tresorerie(app):
    cl, org = signup(app)
    bank(org, 0, balance=300.0)
    a = analyse(org, purchase(org, number='TR-1', supplier_id=supplier(org)))
    s7 = {ch['label']: ch for ch in step(a, 7)['checks']}
    assert s7['Intégrée à la prévision de trésorerie']['ok'] is True and s7['Solde bancaire suffisant']['ok'] is False


# ----------------------------------------------------- liste, lot, droits, MC
def test_liste_lot_droits_et_mission_control(app):
    cl, org = signup(app)
    sid = supplier(org)
    bank(org, 0, balance=50000.0)
    cash(org)
    ok1 = purchase(org, number='OK-1', supplier_id=sid)
    ok2 = purchase(org, number='OK-2', supplier_id=sid, ht=500.0, vat=100.0)
    purchase(org, number='OK-1', supplier_id=sid, status='approved')          # rend OK-1 doublon → bloquée
    t = text(cl.get('/facturation/achats/traitement').get_data(as_text=True))
    assert 'Prêtes à valider (1)' in t and 'Bloquées (' in t and 'OK-2' in t
    mc = text(cl.get('/mission-control').get_data(as_text=True))
    assert 'Factures fournisseurs : 1 prête(s) à valider' in mc and 'Valider 1 facture(s) prête(s)' in mc
    post(cl, '/facturation/achats/traitement', {'purchase_id': [str(ok1), str(ok2)]})
    msg = flashes(cl)
    assert any('1 facture(s) validée(s), comptabilisée(s) et contrôlée(s). 1 refusée(s).' in m for m in msg), msg
    assert analyse(org, ok2)['entry'] is not None and analyse(org, ok1)['entry'] is None
    # Rôle sans droit de validation.
    c = rt.tenant_cx_direct(org)
    c.close()
    ac = rt.auth_cx()
    ac.execute("UPDATE memberships SET role='COLLABORATEUR' WHERE organization_id=?", (org,))
    ac.commit(); ac.close()
    pid = purchase(org, number='OK-3', supplier_id=sid)
    c = rt.tenant_cx_direct(org)
    ok, msg = PP.execute(c, None, pid, 'approve', 'x@example.com', 'COLLABORATEUR', T)
    c.close()
    assert not ok and 'propriétaire, administrateur ou comptable' in msg
    # Isolation.
    cl2, _o2 = signup(app)
    assert cl2.get(f'/facturation/achats/{pid}/traitement').status_code == 404


def test_lot_precoche_seulement_les_conformes(app):
    cl, org = signup(app)
    sid = supplier(org)
    de = supplier(org, name='Bauteile GmbH', vat='DE123456789', siret='')
    bank(org, 0, balance=50000.0)
    a = purchase(org, number='OK-1', supplier_id=sid)
    b = purchase(org, number='DE-1', ht=2000.0, vat=0.0, supplier_name='Bauteile GmbH', supplier_id=de)
    html = cl.get('/facturation/achats/traitement').get_data(as_text=True)
    assert re.search(rf'value="{a}"\s+checked', html) and not re.search(rf'value="{b}"\s+checked', html)


def test_validation_depuis_la_fiche_passe_par_la_chaine(app):
    cl, org = signup(app)
    sid = supplier(org)
    purchase(org, number='DUP-1', supplier_id=sid, status='approved')
    dup = purchase(org, number='DUP-1', supplier_id=sid)
    post(cl, f'/facturation/achats/{dup}/valider')
    assert any('Validation refusée par les contrôles : Doublons' in m for m in flashes(cl))
    assert analyse(org, dup)['purchase']['validation_status'] == 'pending'
    ok = purchase(org, number='OK-9', supplier_id=sid, doc=None)     # « ? » (justificatif) : non bloquant
    post(cl, f'/facturation/achats/{ok}/valider')
    assert any('Facture validée' in m for m in flashes(cl))
    a = analyse(org, ok)
    assert a['entry'] is not None and a['log'][0]['action'] == 'Validée depuis la fiche' and a['log'][0]['ok'] == 1
