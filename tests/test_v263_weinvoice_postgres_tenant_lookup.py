from pathlib import Path
W=Path("profitos/weinvoice.py").read_text(encoding="utf-8")
def test_postgres_safe_tenant_lookup():
    b=W[W.index("def _tenant_connection_for_remote_invoice"):W.index("def handle_invoice_status_webhook")]
    assert "for org_id in list_organization_ids():" in b
    assert "Path(TENANTS).glob('org_*.db')" not in b
    assert "_dbmod.connect_tenant(org_id, tenant_db(org_id))" in b
def test_payload_fields_supported():
    b=W[W.index("def handle_invoice_status_webhook"):]
    assert "data.get('eInvoicingId')" in b
    assert "data.get('status')" in b
    assert "data.get('cdvCode')" in b
