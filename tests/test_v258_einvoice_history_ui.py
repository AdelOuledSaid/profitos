from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / "templates" / "invoicing_detail.html").read_text(encoding="utf-8")
ROUTES = (ROOT / "profitos" / "routes" / "invoicing.py").read_text(encoding="utf-8")


def test_history_exposes_human_readable_event_labels_and_detail():
    assert "Historique de facturation électronique" in TEMPLATE
    assert "Transmission à WeInvoice" in TEMPLATE
    assert "Échec de transmission" in TEMPLATE
    assert "Synchronisation du statut" in TEMPLATE
    assert "Webhook WeInvoice reçu" in TEMPLATE
    assert "ev.detail or '—'" in TEMPLATE


def test_history_highlights_rejections_and_failures():
    assert "ev_rejected" in TEMPLATE
    assert "submission_failed" in TEMPLATE
    assert "Rejetée" in TEMPLATE
    assert "purchase-alert-text" in TEMPLATE


def test_history_keeps_recent_50_events():
    assert "ORDER BY id DESC LIMIT 50" in ROUTES
