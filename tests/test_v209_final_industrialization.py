from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
API=(ROOT/"profitos/routes/api.py").read_text(encoding="utf-8")
RT=(ROOT/"profitos/runtime.py").read_text(encoding="utf-8")

def block(name,next_name=None):
    a=API.index(f"def {name}")
    b=API.index(f"def {next_name}",a) if next_name else len(API)
    return API[a:b]

def test_v209_all_public_api_create_writes_require_idempotency():
    for name,next_name in [
        ("api_create_purchase_invoice","api_create_expense_report"),
        ("api_create_expense_report","api_create_invoice"),
        ("api_create_invoice","api_list_purchase_invoices"),
        ("api_create_supplier","api_list_entities")]:
        b=block(name,next_name)
        assert "api_require_idempotency()" in b
        assert "_idempotency_lookup(key)" in b
        assert "_idempotency_store(key,201,result)" in b
        assert "_api_audit('POST',request.path,'write',201,key)" in b

def test_v209_api_expense_reports_are_entity_scoped():
    b=block("api_create_expense_report","api_create_invoice")
    assert "expense_reports(employee_email,period_label,status,created_at,entity_id)" in b
    assert "_api_eid()" in b

def test_v209_pending_api_purchase_is_not_posted_before_approval():
    b=block("api_create_purchase_invoice","api_create_expense_report")
    assert "validation_status': 'pending'" in b
    assert "generate_purchase_entry(tc, row)" not in b
    assert "not posted to accounting before human approval" in b

def test_v209_api_invoice_numbering_is_entity_scoped_and_serialized():
    b=block("api_create_invoice","api_list_purchase_invoices")
    assert "BEGIN IMMEDIATE" in b
    assert "ef,ep=_entity_where()" in b
    assert "WHERE {ef} AND invoice_number LIKE ?" in b
    assert "SELECT COUNT(*) n FROM outgoing_invoices" not in b

def test_v209_entity_bound_key_cannot_enumerate_other_entities():
    b=block("api_list_entities","api_keys")
    assert "if _api_eid() is not None" in b
    assert "resolve_entity(tc,_api_eid())" in b

def test_v209_production_controls_remain_enabled():
    assert "def csrf_protect" in RT
    assert "secrets.compare_digest(token,sent)" in RT
    assert "def init_rate_limiter" in RT
    assert "def production_dependency_status" in RT
    assert "def database_readiness" in RT
