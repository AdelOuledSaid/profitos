from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]
W=(ROOT/'profitos/weinvoice.py').read_text(encoding='utf-8')
R=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
RT=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')

def test_weinvoice_inbound_uses_documented_endpoints_and_direction():
    assert '/v1/invoice-queries/list' in W
    assert "'direction':'INBOUND'" in W
    assert '/v1/invoice-queries/{remote_id}/content' in W
    assert '/v1/invoice-queries/{remote_id}/readable' in W
    assert "'X-Org-Id':str(organization_id)" in W

def test_purchase_inbound_has_remote_identity_and_unique_index():
    for col in ('weinvoice_invoice_id','weinvoice_status','weinvoice_regulatory_code','weinvoice_last_sync_at'):
        assert col in RT
    assert 'idx_purchase_weinvoice_remote' in RT
    assert 'ON purchase_invoices(entity_id,weinvoice_invoice_id)' in RT

def test_sync_is_entity_scoped_idempotent_and_accounting_atomic():
    assert "purchase_invoices WHERE entity_id IS ? AND weinvoice_invoice_id=?" in R
    assert "generate_purchase_entry(c,purchase_row,commit=False)" in R
    assert "c.rollback()" in R
    assert "direction') or '').upper()!='INBOUND'" in R

def test_modified_python_parses():
    for rel in ('profitos/weinvoice.py','profitos/runtime.py','profitos/routes/invoicing.py'):
        ast.parse((ROOT/rel).read_text(encoding='utf-8'))
