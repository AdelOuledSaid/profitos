from pathlib import Path

W=Path("profitos/weinvoice.py").read_text(encoding="utf-8")
R=Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")
T=Path("templates/invoicing_detail.html").read_text(encoding="utf-8")

def test_force_status_uses_documented_sandbox_contract():
    assert "/v1/_sandbox/einvoicing/{e_invoicing_id}/force-status" in W
    assert "WEINVOICE_ENV != 'sandbox'" in W
    assert "X-Org-Id" in W[W.index("def sandbox_force_invoice_status"):W.index("# ---------------------------------------------------------------------------\n# Vérification Standard Webhooks")]
    assert "occurredAt" in W

def test_force_status_restricts_documented_codes():
    assert "{200,202,203,204,205,206,207,208,209,210,211,212,213,220}" in W

def test_ui_exposes_safe_first_transition_scenarios():
    assert 'name="sandbox_status"' in T
    assert 'value="202"' in T
    assert 'value="213"' in T
    assert "Simuler le statut Sandbox" in T
    assert "request.form.get('sandbox_status','213')" in R

def test_legacy_history_contract_preserved():
    assert "Historique de transmission" in T
