from pathlib import Path
W=Path("profitos/weinvoice.py").read_text(encoding="utf-8")
R=Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")
def test_force_status_uses_management_key_and_org_context():
    block=W[W.index("def sandbox_force_invoice_status"):W.index("# ---------------------------------------------------------------------------\n# Vérification Standard Webhooks")]
    assert "credential_set='management'" in block
    assert "'X-Org-Id': str(organization_id)" in block
    assert "if not organization_id:" in block
def test_route_passes_weinvoice_org():
    assert "organization_id=settings['weinvoice_company_id']" in R
