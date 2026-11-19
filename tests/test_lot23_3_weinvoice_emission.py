"""Lot 23.3 — Émission d'une facture vers la plateforme agréée.

v515 — Ces contrôles cherchaient des libellés affichés à l'utilisateur (« Transmettre à
WeInvoice », « Facture déjà transmise à WeInvoice »). Ils cassaient donc à chaque amélioration
d'un message, et deux commentaires avaient fini par être ajoutés dans les templates à seule
fin de les satisfaire — un texte placé pour tromper un test ne protège plus rien.

Ce qui compte ici est le mécanisme : la colonne qui mémorise l'identifiant distant, la clé
d'idempotence, et la garde qui empêche de transmettre deux fois la même facture. C'est donc
cela qui est vérifié, sans dépendre d'un nom de prestataire ni d'une formulation.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVOICING = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')


def test_lot233_api_contract():
    w = (ROOT / 'profitos/weinvoice.py').read_text(encoding='utf-8')
    assert 'def submit_invoice_file(' in w and 'X-Org-Id' in w and 'Idempotency-Key' in w
    assert '/v1/invoices' in w and 'files=files' in w


def test_lot233_persistence_and_route():
    runtime = (ROOT / 'profitos/runtime.py').read_text(encoding='utf-8')
    for col in ('weinvoice_invoice_id', 'weinvoice_status', 'weinvoice_sent_at',
                'weinvoice_last_error', 'weinvoice_idempotency_key'):
        assert col in runtime
    assert 'def invoicing_send_weinvoice(' in INVOICING
    assert 'render_facturx_pdf(inv, company_row)' in INVOICING
    assert 'weinvoice_idempotency_key' in INVOICING
    # la transmission reste accessible depuis la facture
    detail = (ROOT / 'templates/invoicing_detail.html').read_text(encoding='utf-8')
    assert "url_for('invoicing_send_weinvoice'" in detail


def test_lot233_duplicate_guard():
    """Une facture déjà transmise ne repart pas : la garde porte sur l'identifiant renvoyé
    par la plateforme, et l'utilisateur en est averti."""
    # la même garde protège aussi la correction d'adresse (v503) : on isole la transmission
    send = INVOICING.split('def invoicing_send_weinvoice(', 1)[1].split('\n    @app.route', 1)[0]
    guard = "if 'weinvoice_invoice_id' in inv.keys() and inv['weinvoice_invoice_id']"
    assert guard in send
    after = send.split(guard, 1)[1][:400]
    assert 'flash(' in after and 'déjà transmise' in after
