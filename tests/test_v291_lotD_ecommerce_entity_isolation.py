from pathlib import Path
T=Path("profitos/routes/ecommerce.py").read_text(encoding="utf-8")

def _sync():
    return T.split("def ecommerce_sync(platform):",1)[1]

def test_ecommerce_duplicate_detection_is_scoped_to_current_entity():
    b=_sync()
    assert "entity_id = current_entity_id()" in b
    assert "WHERE platform=? AND entity_id IS ?" in b
    assert "(platform, entity_id)" in b

def test_ecommerce_invoice_sequence_count_is_entity_scoped():
    b=_sync()
    assert "SELECT COUNT(*) n FROM outgoing_invoices WHERE entity_id IS ?" in b
    assert "(entity_id,)" in b

def test_ecommerce_import_mapping_records_entity():
    b=_sync()
    assert "INSERT INTO ecommerce_imported_orders(platform,entity_id,external_order_id,invoice_id,imported_at)" in b
    assert "(platform, entity_id, o['external_id'], new_invoice_id, now())" in b
