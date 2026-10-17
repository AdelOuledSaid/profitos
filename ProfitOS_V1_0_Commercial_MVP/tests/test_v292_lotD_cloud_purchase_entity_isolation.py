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

def test_cloud_purchase_waits_for_validation_before_accounting():
    # Une facture lue automatiquement part en validation ; l'écriture comptable est
    # passée par purchase_validate, jamais à l'import.
    b=_sync()
    assert "'pending', entity_id)" in b
    assert "generate_purchase_entry(" not in b
    v=Path("profitos/routes/invoicing.py").read_text(encoding="utf-8").split("def purchase_validate(",1)[1].split("\n    @app.route",1)[0]
    assert "generate_purchase_entry(c, approved, commit=False)" in v
