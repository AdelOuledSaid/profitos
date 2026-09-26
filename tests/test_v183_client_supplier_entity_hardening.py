from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INV = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'profitos/runtime.py').read_text(encoding='utf-8')


def test_client_records_are_entity_scoped():
    assert 'INSERT INTO invoicing_clients(name,email,address,siret,vat_number,phone,notes,created_at,updated_at,siren,entity_id)' in INV
    assert "SELECT * FROM invoicing_clients WHERE id=? AND entity_id IS ?" in INV
    assert "WHERE cl.entity_id IS ? ORDER BY lower(cl.name)" in INV


def test_supplier_records_are_entity_scoped():
    assert 'INSERT INTO suppliers(name,email,phone,address,siret,vat_number,notes,iban,bic,created_at,entity_id)' in INV
    assert 'SELECT id FROM suppliers WHERE id=? AND entity_id IS ?' in INV
    assert 'UPDATE suppliers SET email=?,phone=?,address=?,iban=?,bic=? WHERE id=? AND entity_id IS ?' in INV


def test_purchase_views_are_entity_scoped():
    assert 'SELECT * FROM purchase_invoices WHERE entity_id IS ? ORDER BY due_date ASC, id DESC' in INV
    assert 'SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?' in INV


def test_legacy_schema_migrates_new_entity_columns():
    for marker in ["('invoicing_clients','entity_id')", "('suppliers','entity_id')", "('purchase_orders','entity_id')"]:
        assert marker in RUNTIME
