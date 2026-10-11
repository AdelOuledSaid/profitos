from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def test_delivery_note_routes_are_entity_scoped():
    s = source('profitos/routes/invoicing.py')
    assert "SELECT * FROM delivery_notes WHERE id=? AND entity_id IS ?" in s
    assert "SELECT status FROM delivery_notes WHERE id=? AND entity_id IS ?" in s
    assert "UPDATE delivery_notes SET status='delivered' WHERE id=? AND entity_id IS ?" in s
    assert "UPDATE delivery_notes SET linked_invoice_id=? WHERE id=? AND entity_id IS ?" in s


def test_proforma_is_entity_scoped_and_uses_entity_identity():
    s = source('profitos/routes/invoicing.py')
    block = s[s.index('def invoicing_proforma'):s.index("@app.route('/facturation/livraisons')")]
    assert "outgoing_invoices WHERE id=? AND entity_id IS ?" in block
    assert 'resolve_entity(c,eid)' in block


def test_gocardless_tables_are_entity_scoped():
    s = source('profitos/runtime.py')
    assert 'CREATE TABLE IF NOT EXISTS gocardless_mandates' in s
    assert 'CREATE TABLE IF NOT EXISTS gocardless_payments' in s
    assert "('gocardless_mandates','entity_id')" in s
    assert "('gocardless_payments','entity_id')" in s


def test_gocardless_route_blocks_cross_entity_invoice_and_duplicate_collection():
    s = source('profitos/routes/gocardless_baas.py')
    assert "outgoing_invoices WHERE id=? AND entity_id IS ?" in s
    assert "gocardless_mandates WHERE id=? AND entity_id IS ?" in s
    assert "gocardless_payments WHERE invoice_id=? AND entity_id IS ?" in s
    assert 'Un prélèvement GoCardless actif existe déjà pour cette facture.' in s
