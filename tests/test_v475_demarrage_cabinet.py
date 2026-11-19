"""v475 — Démarrage guidé (« Premiers pas ») et pièces demandées par l'expert-comptable."""
import io

import profitos.runtime as rt
from profitos.accounting import create_entry
from test_v470_encaissement import app, flashes, post, signup  # noqa: F401


def entry(org):
    c = rt.tenant_cx_direct(org)
    eid = create_entry(c, 'AC', '2026-09-15', 'Achat fournitures', [{'account_code': '606400', 'debit': 100},
                                                                    {'account_code': '401000', 'credit': 100, 'auxiliary_name': 'Fourn'}])
    c.close()
    return eid


def test_premiers_pas(app):
    """v524 — Ce contrôle figeait les libellés des étapes, réécrits depuis pour être
    compris d'un non-comptable. Il porte désormais sur ce qui compte : toutes les étapes
    sont proposées avec le lien qui y mène, une seule est mise en avant comme prochaine
    action, et chacune se coche dès que le travail correspondant est fait."""
    import re
    cl, org = signup(app)
    page = cl.get('/').get_data(as_text=True)
    # chaque étape mène quelque part : c'est le lien, non le libellé, qui fait l'étape
    for cible in ('/company', '/banking', '/comptabilite/reprise', '/facturation/encaissement',
                  '/veille', '/team'):
        assert cible in page, cible
    # une seule action est mise en avant, avec sa raison d'être et le bouton qui y mène
    assert page.count('class="db-next"') == 1, 'une seule prochaine étape doit être proposée'
    bloc = page.split('class="db-next"', 1)[1].split('</div>\n  </div>', 1)[0]
    libelle = bloc.split('db-next-label">', 1)[1].split('<', 1)[0].strip()
    assert libelle, 'la prochaine étape doit être nommée'
    assert 'db-next-why' in bloc, 'la prochaine étape doit dire à quoi elle sert'
    assert 'db-next-btn' in bloc, 'la prochaine étape doit porter son bouton'
    # et elle n'est pas répétée parmi les pastilles du dessous
    pastilles = page.split('class="db-steps"', 1)[1].split('</ul>', 1)[0]
    assert libelle not in pastilles, 'la prochaine étape est répétée dans la liste'

    post(cl, '/facturation/encaissement', {'qr': '1', 'auto_reminders': '1', 'step_1': '3', 'step_2': '15', 'step_3': '30'})
    post(cl, '/veille', {'action': 'settings', 'sector': 'commerce', 'alert_12m': '5', 'alert_1p': '2'})
    entry(org)
    page = cl.get('/').get_data(as_text=True)
    faites = ' '.join(re.findall(r'<li class="done">.*?</li>', page)).lower()
    assert 'iban' in faites and 'secteur' in faites and 'comptabilité' in faites, faites


def test_pieces_demandees(app):
    cl, org = signup(app)
    e = entry(org)
    page = cl.get(f'/comptabilite/ecritures/{e}').get_data(as_text=True)
    assert 'Demander la pièce' in page
    post(cl, f'/comptabilite/ecritures/{e}/demander-piece', {'message': 'Facture du fournisseur'})
    assert any('Pièce demandée' in m for m in flashes(cl))
    post(cl, f'/comptabilite/ecritures/{e}/demander-piece', {'message': 'encore'})
    assert any('déjà ouverte' in m for m in flashes(cl))
    page = cl.get('/comptabilite/pieces-demandees').get_data(as_text=True)
    assert 'Achat fournitures' in page and 'Facture du fournisseur' in page
    assert 'Pièce demandée par' in cl.get(f'/comptabilite/ecritures/{e}').get_data(as_text=True)
    ac = rt.auth_cx()
    ac.execute("UPDATE organizations SET plan='MULTI' WHERE id=?", (org,))
    ac.commit(); ac.close()
    portfolio = cl.get('/cabinet/portefeuille').get_data(as_text=True)
    assert 'Pièces attendues' in portfolio and 'Déclarations en retard' in portfolio
    # Le rapport du lundi le signale.
    with app.test_request_context():
        digest = rt.compute_weekly_digest(org)
    assert digest['week']['documents'] == 1
    # Pièce jointe : la demande est soldée.
    with cl.session_transaction() as s:
        s['csrf_token'] = 'c' * 64
    cl.post(f'/comptabilite/ecritures/{e}/pieces', data={'csrf_token': 'c' * 64, 'piece': (io.BytesIO(b'%PDF-1.4 facture'), 'facture.pdf')},
            content_type='multipart/form-data')
    assert any('soldée' in m for m in flashes(cl))
    assert 'Aucune pièce en attente' in cl.get('/comptabilite/pieces-demandees').get_data(as_text=True)
    # Un membre sans rôle comptable ne peut pas demander de pièce.
    e2 = entry(org)
    ac = rt.auth_cx()
    ac.execute("UPDATE memberships SET role='MEMBER' WHERE organization_id=?", (org,))
    ac.commit(); ac.close()
    post(cl, f'/comptabilite/ecritures/{e2}/demander-piece', {'message': 'x'})
    c = rt.tenant_cx_direct(org)
    assert c.execute("SELECT COUNT(*) n FROM entry_document_requests WHERE entry_id=?", (e2,)).fetchone()['n'] == 0
    c.close()
