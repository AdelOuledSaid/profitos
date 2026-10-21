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
    cl, org = signup(app)
    page = cl.get('/').get_data(as_text=True)
    for label in ('Banque connectée ou relevé importé', 'Comptabilité reprise (FEC ou à-nouveaux)',
                  'IBAN et relances automatiques réglés', 'Secteur de la veille confirmé',
                  'Un collègue ou votre expert-comptable invité'):
        assert label in page, label
    post(cl, '/facturation/encaissement', {'qr': '1', 'auto_reminders': '1', 'step_1': '3', 'step_2': '15', 'step_3': '30'})
    post(cl, '/veille', {'action': 'settings', 'sector': 'commerce', 'alert_12m': '5', 'alert_1p': '2'})
    entry(org)
    page = cl.get('/').get_data(as_text=True)
    import re
    done = re.findall(r'<li class="done">.*?</li>', page)
    labels = ' '.join(done)
    assert 'IBAN et relances' in labels and 'Secteur de la veille' in labels and 'Comptabilité reprise' in labels


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
