from pathlib import Path
T=Path("profitos/routes/cloud_storage.py").read_text(encoding="utf-8")

def _sync():
    return T.split("def cloud_storage_sync(provider):",1)[1]

def test_cloud_purchase_import_uses_current_entity():
    b=_sync()
    assert "from profitos.entities import current_entity_id" in b
    assert "entity_id = current_entity_id()" in b
    assert "validation_status,entity_id)" in b
    assert "'pending', entity_id)" in b

def test_cloud_purchase_accounting_uses_entity_owned_purchase():
    b=_sync()
    assert "SELECT * FROM purchase_invoices WHERE id=?" in b
    assert "generate_purchase_entry(c, purchase_row, commit=False)" in b
