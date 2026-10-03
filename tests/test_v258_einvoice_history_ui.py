from pathlib import Path

TEMPLATE = Path("templates/invoicing_detail.html").read_text(encoding="utf-8")

def test_history_exposes_final_customer_event_labels_and_hides_technical_detail():
    assert "Historique de facturation électronique" in TEMPLATE
    assert "Transmission électronique" in TEMPLATE
    assert "Synchronisation du statut" in TEMPLATE
    assert "Mise à jour du statut" in TEMPLATE
    assert "ev.detail or '—'" not in TEMPLATE
