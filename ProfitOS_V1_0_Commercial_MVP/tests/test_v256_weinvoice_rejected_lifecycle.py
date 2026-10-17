from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
W = (ROOT / "profitos" / "weinvoice.py").read_text(encoding="utf-8")
T = (ROOT / "templates" / "invoicing_detail.html").read_text(encoding="utf-8")

def test_confirmed_rejected_event_is_handled_explicitly():
    assert "event_name == 'invoice.status.rejected'" in W
    assert "Facture électronique rejetée par WeInvoice" in W

def test_rejection_detail_is_preserved_when_provider_sends_one():
    assert "data.get('reason')" in W
    assert "data.get('message')" in W
    assert "data.get('detail')" in W

def test_rejected_state_is_visible_to_user():
    assert "wi_rejected" in T
    assert "La facture a été rejetée." in T
    assert "Rejetée" in T

def test_no_unverified_weinvoice_status_catalog_was_added():
    # v256 ne doit pas inventer une liste de statuts fournisseur non documentée.
    assert "WEINVOICE_STATUS_MAP" not in W
