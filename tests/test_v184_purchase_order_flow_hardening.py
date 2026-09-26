from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INV = (ROOT / 'profitos/routes/invoicing.py').read_text(encoding='utf-8')
RUNTIME = (ROOT / 'profitos/runtime.py').read_text(encoding='utf-8')


def test_purchase_orders_are_entity_scoped_end_to_end():
    assert 'SELECT * FROM purchase_orders WHERE entity_id IS ? ORDER BY order_date DESC,id DESC' in INV
    assert '(supplier_id,supplier_name,order_number,order_date,expected_delivery_date,status,notes,created_at,created_by,entity_id)' in INV
    assert 'SELECT * FROM purchase_orders WHERE id=? AND entity_id IS ?' in INV
    assert 'SELECT status FROM purchase_orders WHERE id=? AND entity_id IS ?' in INV
    assert "UPDATE purchase_orders SET status='sent' WHERE id=? AND entity_id IS ?" in INV
    assert 'UPDATE purchase_orders SET status=? WHERE id=? AND entity_id IS ?' in INV
    assert "UPDATE purchase_orders SET status='cancelled' WHERE id=? AND entity_id IS ?" in INV


def test_purchase_order_numbering_and_supplier_selection_are_entity_scoped():
    assert 'prefix = f"BC-E{eid}-{year}-" if eid else f"BC-{year}-"' in INV
    assert 'WHERE order_number LIKE ? AND entity_id IS ?' in INV
    assert 'SELECT id,name FROM suppliers WHERE entity_id IS ? ORDER BY name' in INV


def test_purchase_invoice_mutations_are_entity_scoped():
    assert 'SELECT * FROM purchase_invoices WHERE id=? AND entity_id IS ?' in INV
    assert "UPDATE purchase_invoices SET status='paid',paid_at=? WHERE id=? AND entity_id IS ?" in INV
    assert 'UPDATE purchase_invoices SET document_path=? WHERE id=? AND entity_id IS ?' in INV
    assert "WHERE id=? AND entity_id IS ?\",\n            (validator_email, now(), purchase_id, eid)" in INV
    assert "WHERE id=? AND entity_id IS ?\",\n            (validator_email, now(), reason or None, purchase_id, eid)" in INV


def test_purchase_order_schema_has_entity_id_for_new_and_legacy_databases():
    block = RUNTIME[RUNTIME.index('CREATE TABLE IF NOT EXISTS purchase_orders('):]
    block = block[:block.index('CREATE TABLE IF NOT EXISTS purchase_order_lines(')]
    assert 'entity_id INTEGER' in block
    assert "('purchase_orders','entity_id')" in RUNTIME
