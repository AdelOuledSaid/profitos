from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")

def test_tenant_schema_migration_fails_closed():
    runtime = src("profitos/runtime.py")
    assert "Tenant schema migration failed" in runtime
    assert "abort(503, description='Schéma de données temporairement indisponible.')" in runtime
    assert "ne bloque jamais la requête" not in runtime

def test_dce_upload_is_entity_scoped():
    dce = src("profitos/routes/dce.py")
    assert "current_entity_id" in dce
    assert "type='GROW' AND entity_id IS ?" in dce
    assert "(opportunity_id,eid)" in dce

def test_supplier_inbox_is_disabled_by_default_and_secret_guarded():
    inv = src("profitos/routes/invoicing.py")
    assert "SUPPLIER_INBOX_WEBHOOK_ENABLED" in inv
    assert "SUPPLIER_INBOX_WEBHOOK_SECRET" in inv
    assert "X-ProfitOS-Inbox-Secret" in inv
    assert "secrets.compare_digest(expected_secret, supplied_secret)" in inv
    assert "len(expected_secret) < 32" in inv

def test_supplier_inbox_keeps_entity_routing_after_guard():
    inv = src("profitos/routes/invoicing.py")
    assert "SELECT organization_id,entity_id FROM supplier_inbox_entity_tokens WHERE token=?" in inv
    assert "'pending', entity_id" in inv
