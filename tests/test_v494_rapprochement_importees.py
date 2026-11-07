"""v494 — Rapprochement bancaire des factures en cours importées depuis un fichier.

Avant : la page « Mouvements bancaires à traiter » ne cherchait que parmi les factures émises
dans Veqalio ; un virement citant le numéro et le montant exact d'une facture importée restait
« sans correspondance », alors que Mission Control signalait ce même virement sur la créance."""
import io
from datetime import timedelta

import profitos.runtime as rt
from profitos import bank_imported as BI
from profitos import mission_control as MC
from profitos.accounting import paris_today
from test_v470_encaissement import app, flashes, post, signup  # noqa: F401


def fr(d):
    return d.strftime('%d/%m/%Y')


def csv_upload(cl, url, rows):
    body = '\r\n'.join(';'.join(r) for r in rows).encode('utf-8-sig')
    r = post(cl, url, {'file': (io.BytesIO(body), 'fichier.csv')}, content_type='multipart/form-data')
    assert r.status_code in (200, 302), r.status_code
    return flashes(cl)


def setup_dossier(app):
    """Trois factures importées et un relevé : un virement exact avec n° de facture, un acompte
    au nom du client (« M. et Mme »), un loyer prélevé qui correspond à une dépense importée."""
    cl, org = signup(app)
    today = paris_today()
    issue, due, paid_on = today - timedelta(days=60), today - timedelta(days=30), today - timedelta(days=20)
    csv_upload(cl, '/upload/invoices', [
        ['Numéro', 'Client', 'Montant TTC', 'Montant payé', 'Date facture', 'Échéance', 'Email client'],
        ['FA-T-101', 'Restaurant Le Bon Coin', '7 068,00', '0,00', fr(issue), fr(due), 'resto@example.com'],
        ['FA-T-102', 'M. et Mme Garnier', '5 720,00', '0,00', fr(issue), fr(due), 'garnier@example.com'],
        ['FA-T-103', 'Pharmacie Centrale', '1 200,00', '0,00', fr(issue), fr(due), 'pharma@example.com'],
    ])
    csv_upload(cl, '/upload/expenses', [
        ['Fournisseur', 'Description', 'Montant', 'Date', 'Catégorie'],
        ['SCI des Ateliers', 'Loyer atelier', '2 400,00', fr(paid_on), 'Loyer'],
    ])
    csv_upload(cl, '/upload/bank-statement', [
        ['Date', 'Libellé', 'Montant'],
        [fr(paid_on), 'VIR RESTAURANT LE BON COIN FA-T-101', '7 068,00'],
        [fr(paid_on), 'VIR M GARNIER ACOMPTE', '1 716,00'],
        [fr(paid_on), 'PRLV SCI DES ATELIERS LOYER', '-2 400,00'],
    ])
    return cl, org, today


def by_label(items, text):
    return next(x for x in items if text in (x['tx']['label'] or ''))


def test_virement_exact_propose_puis_rapproche(app):
    cl, org, today = setup_dossier(app)
    c = rt.tenant_cx_direct(org)
    props, unmatched = MC.bank_proposals(c, None, today)
    exact = by_label(props, 'FA-T-101')
    assert exact['target_kind'] == 'importee' and exact['target']['invoice_number'] == 'FA-T-101'
    assert exact['amount'] == 7068.0 and exact['confidence'] == 'Élevée'
    assert {'solde exact', 'n° facture', 'client reconnu'} <= set(exact['reasons'])
    # « M. et Mme Garnier » est reconnu dans « VIR M GARNIER » : acompte proposé, confiance faible
    acompte = by_label(props, 'GARNIER')
    assert acompte['target']['invoice_number'] == 'FA-T-102' and acompte['amount'] == 1716.0
    assert acompte['confidence'] == 'Faible'
    # le loyer prélevé est expliqué par la dépense importée
    loyer = by_label(unmatched, 'LOYER')
    assert 'dépense importée « Loyer atelier »' in loyer['reason'] and 'catégorie Loyer' in loyer['reason']

    before = len(MC.pending_transactions(c, None, today))
    done, refused = MC.apply_bank_proposals(c, None, today, [exact['id'], acompte['id']])
    assert len(done) == 2 and not refused
    assert len(MC.pending_transactions(c, None, today)) == before - 2
    rows = {r['invoice_number']: r for r in c.execute("SELECT * FROM invoices").fetchall()}
    assert rows['FA-T-101']['status'] == 'paid' and rows['FA-T-101']['paid_amount'] == 7068.0
    assert rows['FA-T-102']['status'] != 'paid' and rows['FA-T-102']['paid_amount'] == 1716.0
    # écriture 512 / 411 pour chaque encaissement
    lines = c.execute("""SELECT l.account_code, l.debit, l.credit FROM accounting_entries e
                         JOIN accounting_entry_lines l ON l.entry_id=e.id
                         WHERE e.source_type='imported_invoice_payment' ORDER BY l.id""").fetchall()
    assert sum(l['debit'] for l in lines if l['account_code'].startswith('512')) == 7068.0 + 1716.0
    assert sum(l['credit'] for l in lines if l['account_code'] == '411000') == 7068.0 + 1716.0
    # le virement n'est plus proposé une seconde fois
    props2, _u = MC.bank_proposals(c, None, today)
    assert not [p for p in props2 if p['target_kind'] == 'importee']
    c.close()
    # la créance soldée sort des impayés de Mission Control
    page = cl.get('/mission-control').get_data(as_text=True)
    assert 'FA-T-101' not in page and 'FA-T-103' in page


def test_carte_creance_libelle_paiement_trouve(app):
    cl, org, today = setup_dossier(app)
    c = rt.tenant_cx_direct(org)
    with app.test_request_context('/'):
        findings = [f for f in MC.detect_receivables(c, None, today) if f['ref']['number'] == 'FA-T-101']
    c.close()
    labels = [ch['label'] for ch in findings[0]['checks']]
    assert 'Paiement possible trouvé en banque' in labels and 'Aucun paiement déjà reçu en banque' not in labels


def test_un_virement_ne_regle_qu_une_fois(app):
    cl, org, today = setup_dossier(app)
    c = rt.tenant_cx_direct(org)
    props, _u = MC.bank_proposals(c, None, today)
    exact = by_label(props, 'FA-T-101')
    BI.reconcile_imported(c, None, exact['tx']['id'], exact['target']['id'])
    try:
        BI.reconcile_imported(c, None, exact['tx']['id'], exact['target']['id'])
        raise AssertionError('un second rapprochement aurait dû être refusé')
    except ValueError:
        pass
    assert c.execute("SELECT COUNT(*) n FROM bank_imported_allocations").fetchone()['n'] == 1
    c.close()


def test_nouvel_import_conserve_les_encaissements_rapproches(app):
    cl, org, today = setup_dossier(app)
    c = rt.tenant_cx_direct(org)
    props, _u = MC.bank_proposals(c, None, today)
    MC.apply_bank_proposals(c, None, today, [by_label(props, 'FA-T-101')['id']])
    c.close()
    issue, due = today - timedelta(days=60), today - timedelta(days=30)
    # le client réimporte le même fichier (montant payé toujours à 0)
    csv_upload(cl, '/upload/invoices', [
        ['Numéro', 'Client', 'Montant TTC', 'Montant payé', 'Date facture', 'Échéance'],
        ['FA-T-101', 'Restaurant Le Bon Coin', '7 068,00', '0,00', fr(issue), fr(due)],
        ['FA-T-103', 'Pharmacie Centrale', '1 200,00', '0,00', fr(issue), fr(due)],
    ])
    c = rt.tenant_cx_direct(org)
    row = c.execute("SELECT * FROM invoices WHERE invoice_number='FA-T-101'").fetchone()
    assert row['status'] == 'paid' and row['paid_amount'] == 7068.0
    alloc = c.execute("SELECT imported_invoice_id FROM bank_imported_allocations").fetchone()
    assert alloc['imported_invoice_id'] == row['id']
    c.close()


def test_page_mouvements_affiche_la_facture_importee(app):
    cl, org, today = setup_dossier(app)
    page = cl.get('/mission-control/banque').get_data(as_text=True)
    assert 'Facture client importée FA-T-101' in page


def test_acompte_ambigu_choisi_a_la_main(app):
    """Deux factures ouvertes du même client et un acompte sans numéro : Veqalio ne choisit pas,
    mais la page propose de choisir la facture, puis exécute le rapprochement validé."""
    cl, org = signup(app)
    today = paris_today()
    issue, due, paid_on = today - timedelta(days=60), today - timedelta(days=30), today - timedelta(days=20)
    csv_upload(cl, '/upload/invoices', [
        ['Numéro', 'Client', 'Montant TTC', 'Montant payé', 'Date facture', 'Échéance'],
        ['FA-H-1', 'Hôtel du Parc', '8 868,00', '0,00', fr(issue), fr(due)],
        ['FA-H-2', 'Hôtel du Parc', '3 804,00', '0,00', fr(issue), fr(due)],
    ])
    csv_upload(cl, '/upload/bank-statement', [['Date', 'Libellé', 'Montant'], [fr(paid_on), 'VIR HOTEL DU PARC', '2 660,40']])
    page = cl.get('/mission-control/banque').get_data(as_text=True)
    assert 'choisissez celle que ce mouvement règle' in page and 'name="choice"' in page
    c = rt.tenant_cx_direct(org)
    _p, unmatched = MC.bank_proposals(c, None, today)
    choice = next(ch for ch in unmatched[0]['choices'] if ch['number'] == 'FA-H-1')
    c.close()
    post(cl, '/mission-control/banque', {'choice': choice['id']})
    assert any('2 660,40 € rapprochés avec la facture FA-H-1' in m.replace(' ', ' ').replace('\xa0', ' ')
               for m in flashes(cl))
    c = rt.tenant_cx_direct(org)
    assert c.execute("SELECT paid_amount FROM invoices WHERE invoice_number='FA-H-1'").fetchone()['paid_amount'] == 2660.4
    assert not MC.pending_transactions(c, None, today)
    c.close()
    # un choix périmé (mouvement déjà rapproché) est refusé sans rien exécuter
    post(cl, '/mission-control/banque', {'choice': choice['id']})
    assert any("n'est plus proposé" in m for m in flashes(cl))
