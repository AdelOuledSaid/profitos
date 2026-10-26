"""v484 — Mission Control résout : actions automatisables séparées des tâches manuelles,
montants qualifiés, « ce qui manque » explicite, déclaration « traité » contrôlée par les
données, statut des créances anciennes, mission bancaire résolue par propositions validées."""
import re
from datetime import timedelta

import profitos.runtime as rt
from profitos import mission_control as MC
from profitos import verification as V
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, issue_invoice, post, signup  # noqa: F401
from test_v480_mission_control import bank, cash, mail, overdue, page  # noqa: F401

T = paris_today()


def inv_number(org, inv):
    c = rt.tenant_cx_direct(org)
    n = c.execute('SELECT invoice_number FROM outgoing_invoices WHERE id=?', (inv,)).fetchone()['invoice_number']
    c.close()
    return n


def purchase(org, number, total, supplier='Matériaux Sud'):
    c = rt.tenant_cx_direct(org)
    cur = c.execute("""INSERT INTO purchase_invoices(supplier_name,invoice_number,issue_date,due_date,subtotal,vat_amount,total,status,
                       created_at,validation_status,entity_id) VALUES(?,?,?,?,?,0,?,'unpaid',?,'approved',NULL)""",
                    (supplier, number, (T - timedelta(days=40)).isoformat(), (T + timedelta(days=3)).isoformat(), total, total, rt.now()))
    c.commit()
    pid = cur.lastrowid
    c.close()
    return pid


# ------------------------------------------------------------- montants et groupes
def test_groupes_et_montant_qualifie(app, monkeypatch):
    mail(monkeypatch, [])
    cl, org = signup(app)
    inv = issue_invoice(cl)
    overdue(org, inv, 20)
    cash(org)
    bank(org, 0, balance=20000.0, txs=[(-45.0, 'PRLV TELECOM', 12), (-1300.0, 'VIR LOYER', 15)])
    purchase(org, 'MS-1', 800.0)
    html, text = page(cl)
    assert 'Action automatisable (1)' in text and 'Envoyer la relance n°1' in text
    assert 'Prêt à traiter manuellement' in text and 'Préparer le virement' in text
    assert 'Volume de mouvements à vérifier (entrées et sorties cumulées) : 1 345,00 €' in text.replace(' ', ' ')
    assert "ce n'est ni une perte, ni une somme à récupérer" in text
    assert 'Restant dû : 1 200,00 €' in text.replace(' ', ' ')
    assert 'Action automatisable' in text and 'À traiter manuellement' in text      # étiquettes par carte


# ------------------------------------------------------------ ce qui manque
def test_ce_qui_manque_est_explicite(app):
    cl, org = signup(app)
    inv = issue_invoice(cl, client_email='')
    overdue(org, inv, 20)
    cash(org)
    _h, text = page(cl)
    assert 'Ce qui manque pour agir' in text
    assert 'Aucun paiement déjà reçu en banque : Connectez la banque ou importez un relevé récent' in text
    assert "E-mail du client renseigné : Ajoutez l'e-mail du client sur la facture." in text


# ------------------------------------------------------------ créances anciennes
def test_creance_ancienne_demande_une_decision(app, monkeypatch):
    sent = []
    mail(monkeypatch, sent)
    cl, org = signup(app)
    inv = issue_invoice(cl, price='4000')
    overdue(org, inv, 356)
    cash(org)
    bank(org, 0)
    html, text = page(cl)
    assert 'impayée depuis 356 jour(s)' in text and "À vérifier avant d'agir (1)" in text
    assert 'Statut de la créance confirmé : Indiquez ci-dessous si elle est toujours due, en litige ou douteuse' in text
    assert 'Enregistrer le statut' in html and 'Envoyer la relance' not in html
    key = {'source': 'outgoing', 'invoice_id': str(inv), 'next': '/mission-control'}
    post(cl, '/mission-control/creance', dict(key, status='litige', note=''))
    assert any('Précisez le motif' in m for m in flashes(cl))
    post(cl, '/mission-control/creance', dict(key, status='litige', note='Prestation contestée par le client'))
    assert any('« En litige »' in m for m in flashes(cl))
    html, text = page(cl)
    assert "Action impossible pour l'instant (1)" in text and 'Prestation contestée par le client' in text
    assert 'Quand le litige est réglé, choisissez « Toujours due »' in text
    # Le litige bloque aussi la relance manuelle depuis la facture.
    post(cl, f'/facturation/{inv}/relancer')
    assert any('relance suspendue' in m for m in flashes(cl)) and 'client@example.com' not in sent
    # Litige réglé : toujours due → relance possible.
    post(cl, '/mission-control/creance', dict(key, status='due', note=''))
    html, text = page(cl)
    assert 'Action automatisable (1)' in text and 'Toujours due, confirmé le' in text and 'Envoyer la relance n°1' in html
    # Créance inexistante : refusée.
    post(cl, '/mission-control/creance', dict(key, invoice_id='9999', status='due'))
    assert any('introuvable' in m for m in flashes(cl))


# --------------------------------------------------------------- « Je l'ai traité »
def test_declaration_traitee_controlee_par_les_donnees(app):
    cl, org = signup(app)
    cash(org)
    bank(org, 0, txs=[(-45.0, 'PRLV TELECOM', 12), (-20.0, 'FRAIS', 10)])
    _h, text = page(cl)
    assert "Je l'ai traité : vérifier" in text
    post(cl, '/mission-control/suivi', {'key': 'banque:a_traiter', 'op': 'fait'})
    msg = flashes(cl)
    assert any("n'est pas soldé" in m and 'reste « en contrôle »' in m for m in msg), msg
    html, text = page(cl)
    assert 'Déclaré traité, pas encore confirmé par les données' in text
    assert '65,00 € au moment de l\'action, 65,00 € aujourd\'hui' in text.replace(' ', ' ')
    # Rien n'a changé dans les données : après la date de contrôle, le point est rouvert.
    c = rt.tenant_cx_direct(org)
    c.execute("UPDATE verification_items SET check_after=? WHERE item_key='banque:a_traiter'", ((T - timedelta(days=1)).isoformat(),))
    c.commit(); c.close()
    _h, text = page(cl)
    assert 'Toujours présent au contrôle du' in text
    c = rt.tenant_cx_direct(org)
    assert [r['status'] for r in V.items(c, None) if r['item_key'] == 'banque:a_traiter'] == [V.OPEN]
    c.close()


# ------------------------------------------------------------- mission bancaire
def test_mission_bancaire_resolue(app):
    cl, org = signup(app)
    cash(org)
    a = issue_invoice(cl, price='1000')                 # 1 200 € TTC
    b = issue_invoice(cl, price='500')                  # 600 € TTC
    na, nb = inv_number(org, a), inv_number(org, b)
    p = purchase(org, 'MS-77', 960.0)
    bank(org, 0, txs=[(1200.0, f'VIR CLIENT DURAND {na}', 9), (600.0, f'VIR DURAND {nb}', 10),
                      (-960.0, 'VIR MATERIAUX SUD MS-77', 11), (-45.0, 'PRLV TELECOM', 12)])
    html, text = page(cl)
    assert 'Examiner 3 proposition(s) de rapprochement' in text and 'Action automatisable' in text
    assert 'Préparé par ProfitOS, à valider' in text
    html = cl.get('/mission-control/banque').get_data(as_text=True)
    t = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace(' ', ' ').replace('\xa0', ' ')
    assert 'Propositions de rapprochement (3)' in t and 'À traiter manuellement (1)' in t
    assert f'Facture client {na}' in t and 'Facture fournisseur MS-77' in t and 'n° facture' in t
    assert 'PRLV TELECOM' in t and 'frais, abonnement ou dépense à comptabiliser' in t
    ids = re.findall(r'name="proposal" value="([^"]+)"', html)
    assert len(ids) == 3
    # Validation de deux propositions + une proposition périmée : seules les valides sont exécutées.
    chosen = [x for x in ids if x.endswith(f':facture:{a}') or x.endswith(f':achat:{p}')]
    post(cl, '/mission-control/banque', {'proposal': chosen + ['999:facture:999']})
    msg = flashes(cl)
    assert any('2 rapprochement(s) validé(s) et exécuté(s) ; mouvements en attente : 4 → 2 ; 1 refusé(s)' in m for m in msg), msg
    assert any("n'est plus proposé" in m for m in msg)
    c = rt.tenant_cx_direct(org)
    assert c.execute('SELECT status FROM outgoing_invoices WHERE id=?', (a,)).fetchone()['status'] == 'paid'
    assert c.execute('SELECT status FROM outgoing_invoices WHERE id=?', (b,)).fetchone()['status'] == 'sent'
    assert c.execute('SELECT status FROM purchase_invoices WHERE id=?', (p,)).fetchone()['status'] == 'paid'
    assert c.execute('SELECT COUNT(*) n FROM bank_invoice_allocations').fetchone()['n'] == 1
    assert c.execute('SELECT COUNT(*) n FROM accounting_entries').fetchone()['n'] >= 2      # écritures de règlement
    item = [r for r in V.items(c, None) if r['item_key'] == 'banque:a_traiter'][0]
    c.close()
    assert item['status'] == V.IN_CONTROL and '4 → 2' in item['action_result']
    # Rejouer la même validation : plus rien n'est exécuté deux fois.
    post(cl, '/mission-control/banque', {'proposal': chosen})
    assert any('0 rapprochement(s)' in m and '2 refusé(s)' in m for m in flashes(cl))


def test_une_facture_pour_un_seul_mouvement(app):
    cl, org = signup(app)
    a = issue_invoice(cl, price='1000')
    na = inv_number(org, a)
    bank(org, 0, txs=[(1200.0, f'VIR DURAND {na}', 9), (1200.0, 'VIR DURAND', 10)])
    c = rt.tenant_cx_direct(org)
    proposals, unmatched = MC.bank_proposals(c, None, T)
    c.close()
    assert len(proposals) == 1 and proposals[0]['tx']['label'].endswith(na)
    assert any('déjà proposée pour un autre mouvement' in u['reason'] for u in unmatched)


def test_virement_citant_une_autre_facture(app):
    cl, org = signup(app)
    a = issue_invoice(cl, price='1000')
    old = issue_invoice(cl, price='4000')
    overdue(org, old, 40)
    overdue(org, a, 20)
    na = inv_number(org, a)
    bank(org, 0, txs=[(1200.0, f'VIR CLIENT DURAND {na}', 9)])
    c = rt.tenant_cx_direct(org)
    inv_old = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (old,)).fetchone()
    assert MC.probable_bank_payment(c, inv_old, 4800.0) is None          # le virement cite FA…-001, pas celle-ci
    inv_a = c.execute('SELECT * FROM outgoing_invoices WHERE id=?', (a,)).fetchone()
    assert MC.probable_bank_payment(c, inv_a, 1200.0) is not None
    proposals, _u = MC.bank_proposals(c, None, T)
    c.close()
    assert [p['target']['id'] for p in proposals] == [a]
