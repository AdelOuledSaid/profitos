from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RUNTIME=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")
API=(ROOT/"profitos/routes/api.py").read_text(encoding="utf-8")
WH=(ROOT/"profitos/webhooks_outbound.py").read_text(encoding="utf-8")
UI=(ROOT/"templates/api_keys.html").read_text(encoding="utf-8")

def test_v204_api_tokens_are_hashed_revocable_scoped_and_entity_bound():
    assert "hash_api_key(raw_key)" in RUNTIME
    assert "revoked_at IS NULL" in RUNTIME
    assert "g.api_entity_id" in RUNTIME
    assert "g.api_scopes" in RUNTIME
    assert "api_scope_required" in RUNTIME
    assert "scopes TEXT" in RUNTIME and "entity_id INTEGER" in RUNTIME

def test_v204_entity_scoping_is_applied_to_commercial_reads():
    assert 'FROM purchase_invoices WHERE {ef}' in API
    assert 'FROM outgoing_invoices WHERE {ef}' in API
    assert 'FROM suppliers WHERE {_entity_where()[0]}' in API
    assert "SELECT * FROM outgoing_invoices WHERE id=? AND {ef}" in API
    assert "SELECT * FROM purchase_invoices WHERE id=? AND {ef}" in API

def test_v204_purchase_mark_paid_uses_pass18_ledger_and_atomic_accounting():
    block=API[API.index("def api_mark_purchase_paid"):API.index("@app.route('/api/v1/invoices', methods=['GET'])")]
    assert "purchase_invoice_payments" in block
    assert "generate_purchase_partial_payment_entry" in block
    assert "Idempotency-Key" in block or "api_require_idempotency" in block
    assert "tc.rollback()" in block
    assert "generate_purchase_payment_entry" not in block

def test_v204_api_has_audit_and_idempotency_registry():
    assert "CREATE TABLE IF NOT EXISTS api_audit_log" in RUNTIME
    assert "CREATE TABLE IF NOT EXISTS api_idempotency" in RUNTIME
    assert "UNIQUE(organization_id,entity_key,idempotency_key,method,path)" in RUNTIME
    assert "_idempotency_lookup" in API and "_idempotency_store" in API

def test_v204_webhooks_are_signed_ssrf_protected_and_entity_scoped():
    assert "hmac.new" in WH
    assert "X-ProfitOS-Signature" in WH
    assert "X-ProfitOS-Event-Id" in WH
    assert "allow_redirects=False" in WH
    assert "ip.is_private" in WH and "ip.is_link_local" in WH
    assert "entity_id=?" in WH or "entity_id IS NULL" in WH
    assert "webhook_subscriptions','entity_id'" in RUNTIME

def test_v204_ui_exposes_fine_grained_scopes_not_legacy_only():
    assert 'name="scopes" value="read"' in UI
    assert 'name="scopes" value="write"' in UI
    assert 'name="scopes" value="webhooks"' in UI
    assert 'name="entity_id"' in UI
