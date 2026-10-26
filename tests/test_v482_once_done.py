"""v482 — Once & Done : dossier entreprise unique et vérifié, saisi une fois, réutilisé par
les démarches (fiche fournisseur, mise en demeure, résiliation, délai de paiement)."""
import re
from datetime import timedelta

import profitos.runtime as rt
from profitos import corporate_tax as CT
from profitos import market_watch as MW
from profitos import once_done as OD
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, issue_invoice, post, signup  # noqa: F401

T = paris_today()


def text(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).replace('&#39;', "'").replace(' ', ' ').replace('\xa0', ' ')


def complete(cl):
    post(cl, '/demarches', {'representant': 'Alice Martin', 'qualite': 'Présidente', 'email': 'contact@atelier.example',
                            'telephone': '01 23 45 67 89', 'naf': '43.22B', 'date_creation': '2019-03-01'})
    return flashes(cl)


def test_dossier_verifie_et_saisi_une_fois(app):
    cl, org = signup(app)
    t = text(cl.get('/demarches').get_data(as_text=True))
    assert 'Dossier entreprise et démarches' in t and 'SIRET 73282932000074 ✓' in t
    assert 'Représentant légal — ? À renseigner une fois.' in t
    msgs = complete(cl)
    assert any('Enregistré une fois pour toutes' in m for m in msgs)
    # Chaque information est rangée à sa place habituelle et réutilisée.
    c = rt.tenant_cx_direct(org)
    fiscal = CT.get_fiscal_profile(c, None)
    assert (fiscal['signataire'], fiscal['signataire_qualite'], fiscal['mel'], fiscal['date_creation']) == \
        ('Alice Martin', 'Présidente', 'contact@atelier.example', '2019-03-01')
    assert MW.load_settings(c, None)['naf'] == '4322B'
    info = OD.dossier(c, None)
    c.close()
    assert info['telephone']['value'] == '01 23 45 67 89' and info['siren']['value'] == '732829320'
    assert all(v['check']['ok'] is True for v in info.values()), {k: v['check'] for k, v in info.items() if v['check']['ok'] is not True}
    # Saisie invalide refusée.
    post(cl, '/demarches', {'naf': '43X', 'email': 'pas-un-email'})
    msgs = flashes(cl)
    assert any('Code APE invalide' in m for m in msgs) and any('E-mail de contact invalide' in m for m in msgs)


def test_incoherences_bloquantes_et_mission_control(app):
    cl, org = signup(app)
    complete(cl)
    c = rt.tenant_cx_direct(org)
    c.execute("UPDATE company SET iban='FR7630006000011234567890188', vat_regime='franchise_293B' WHERE id=1")
    c.commit()
    CT.save_fiscal_profile(c, None, {'vat_filing': 'ca3_mensuelle'}, rt.now())
    info = OD.dossier(c, None)
    c.close()
    assert info['iban']['check']['ok'] is False and info['vat_regime']['check']['ok'] is False
    t = text(cl.get('/demarches').get_data(as_text=True))
    assert 'Franchise en base sur les factures mais déclarations de TVA prévues au profil fiscal' in t
    assert 'Bloqué' in t
    # Document refusé côté serveur même en forçant le POST.
    post(cl, '/demarches/fiche_fournisseur')
    assert any('Document non préparé' in m for m in flashes(cl))
    # Mission Control le signale.
    mc = text(cl.get('/mission-control').get_data(as_text=True))
    assert 'Dossier entreprise : 2 donnée(s) invalide(s) ou contradictoire(s)' in mc and 'Corriger le dossier' in mc


def test_code_ape_et_secteur_a_confirmer(app):
    cl, org = signup(app)
    complete(cl)
    post(cl, '/veille', {'action': 'settings', 'sector': 'restauration', 'naf': '4322B', 'alert_12m': '5', 'alert_1p': '2'})
    c = rt.tenant_cx_direct(org)
    info = OD.dossier(c, None)
    c.close()
    assert info['naf']['check']['ok'] is None and 'à confirmer' in info['naf']['check']['detail']


def test_fiche_fournisseur(app):
    cl, org = signup(app)
    t = text(cl.get('/demarches/fiche_fournisseur').get_data(as_text=True))
    assert 'Complétez d\'abord le dossier entreprise' in t and 'Préparer le document' not in t
    complete(cl)
    html = post(cl, '/demarches/fiche_fournisseur').get_data(as_text=True)
    t = text(html)
    assert 'Fiche de référencement fournisseur' in t and '732829320 / 73282932000074' in t
    assert 'Alice Martin, Présidente' in t and 'IBAN FR7630006000011234567890189' in t and 'Code APE 4322B' in t
    assert '<style>' not in html and 'onclick' not in html and 'data-print' in html     # politique de sécurité


def test_mise_en_demeure(app):
    cl, org = signup(app)
    complete(cl)
    inv = issue_invoice(cl, price='1000')
    c = rt.tenant_cx_direct(org)
    c.execute('UPDATE outgoing_invoices SET due_date=? WHERE id=?', ((T - timedelta(days=45)).isoformat(), inv))
    c.commit(); c.close()
    page = text(cl.get('/demarches/mise_en_demeure').get_data(as_text=True))
    assert '1 200,00 € restant dû' in page
    t = text(post(cl, '/demarches/mise_en_demeure', {'invoice_id': str(inv), 'delay_days': '8'}).get_data(as_text=True))
    assert 'mise en demeure de payer — facture n°' in t and 'reste impayée à hauteur de 1 200,00 €' in t
    assert (T + timedelta(days=8)).strftime('%d/%m/%Y') in t and "l'indemnité forfaitaire pour frais de recouvrement de 40 €" in t
    assert 'Client Durand' in t and 'Alice Martin Présidente' in t
    post(cl, '/demarches/mise_en_demeure', {'invoice_id': str(inv), 'delay_days': '3'})
    assert any('entre 8 et 30 jours' in m for m in flashes(cl))
    post(cl, '/demarches/mise_en_demeure', {'invoice_id': '999', 'delay_days': '8'})
    assert any('Choisissez une facture échue' in m for m in flashes(cl))


def test_resiliation_liee_au_contrat(app):
    cl, org = signup(app)
    complete(cl)
    c = rt.tenant_cx_direct(org)
    c.execute("INSERT INTO suppliers(name,created_at) VALUES('Télécom Plus',?)", (rt.now(),))
    sid = c.execute("SELECT id FROM suppliers WHERE name='Télécom Plus'").fetchone()['id']
    c.commit(); c.close()
    post(cl, '/contrats-fournisseurs/nouveau', {'supplier_id': str(sid), 'supplier_name': 'Télécom Plus', 'label': 'Forfait mobile',
                                                'periodicity': 'mensuel', 'amount_ht': '80', 'start_date': '2024-01-01',
                                                'end_date': (T + timedelta(days=50)).isoformat(), 'tacit_renewal': '1',
                                                'notice_days': '30'})
    flashes(cl)
    c = rt.tenant_cx_direct(org)
    kid = c.execute('SELECT id FROM supplier_contracts').fetchone()['id']
    c.close()
    post(cl, '/demarches/resiliation', {'contract_id': str(kid), 'supplier_address': ''})
    assert any("adresse du fournisseur" in m for m in flashes(cl))
    t = text(post(cl, '/demarches/resiliation', {'contract_id': str(kid), 'supplier_address': '5 avenue Opéra 75009 Paris'})
             .get_data(as_text=True))
    assert 'résiliation du contrat « Forfait mobile »' in t and 'préavis contractuel de 30 jours' in t
    assert (T + timedelta(days=50)).strftime('%d/%m/%Y') in t and 'est dépassée' not in t
    # L'adresse est rangée sur la fiche du fournisseur et reproposée la fois suivante.
    c = rt.tenant_cx_direct(org)
    assert c.execute('SELECT address FROM suppliers WHERE id=?', (sid,)).fetchone()['address'] == '5 avenue Opéra 75009 Paris'
    c.close()
    assert '5 avenue Opéra 75009 Paris' in cl.get('/demarches/resiliation').get_data(as_text=True)


def test_delai_sie_et_droits(app):
    cl, org = signup(app)
    complete(cl)
    post(cl, '/demarches/delai_sie', {'tax': 'TVA de septembre 2026', 'amount': '9 000', 'months': '30', 'reason': 'x'})
    assert any('entre 2 et 24' in m for m in flashes(cl))
    t = text(post(cl, '/demarches/delai_sie', {'tax': 'TVA de septembre 2026', 'amount': '9 000', 'months': '6',
                                                'due_date': '2026-10-24', 'reason': 'Retard de paiement d\'un client important.'})
             .get_data(as_text=True))
    assert 'demande de plan de règlement — TVA de septembre 2026' in t and '9 000,00 €' in t
    assert '6 mensualités d\'environ 1 500,00 €' in t and 'exigible le 24/10/2026' in t
    assert cl.get('/demarches/inconnue').status_code == 404
