from pathlib import Path

TEMPLATE = Path("templates/invoicing_detail.html").read_text(encoding="utf-8")

def test_history_exposes_human_readable_event_labels_and_hides_technical_detail():
    assert "Historique de facturation électronique" in TEMPLATE
    assert "Transmission à WeInvoice" in TEMPLATE
    assert "Échec de transmission" in TEMPLATE
    assert "Synchronisation du statut" in TEMPLATE
    assert "Webhook WeInvoice reçu" in TEMPLATE
    # Version client finale : le détail technique reste en backend
    # mais n'est plus exposé dans l'interface utilisateur.
    assert "ev.detail or '—'" not in TEMPLATE
