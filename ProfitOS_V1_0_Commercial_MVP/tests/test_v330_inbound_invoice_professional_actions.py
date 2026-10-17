from pathlib import Path
T=(Path(__file__).resolve().parents[1]/"templates/purchase_detail.html").read_text(encoding="utf-8")

def test_einvoice_panel_precedes_standard_details():
    assert T.index("Facturation électronique") < T.index("Détails de la facture")

def test_provider_brand_is_not_rendered_in_inbound_note():
    assert "Reçue par facturation électronique{% else %}{{ p.notes }}" in T

def test_received_invoice_has_direct_business_actions():
    for label in ("Prendre en charge","Accepter","Accepter partiellement","Suspendre","Signaler le paiement","Refuser"):
        assert label in T
    assert 'name="action" value="approve"' in T
    assert 'name="action" value="take-in-charge"' in T
