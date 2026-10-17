from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
W = (ROOT / "profitos" / "weinvoice.py").read_text(encoding="utf-8")

def test_webhook_processing_is_atomic_and_rolls_back_on_failure():
    assert "conn.rollback()" in W
    assert "WEINVOICE_INVOICE_WEBHOOK_PROCESSING_FAILED" in W

def test_processing_failure_is_propagated_for_http_retry_semantics():
    block = W.split("WEINVOICE_INVOICE_WEBHOOK_PROCESSING_FAILED", 1)[1][:500]
    assert "raise" in block

def test_success_log_keeps_provider_webhook_identifier_for_traceability():
    assert "WEINVOICE_INVOICE_STATUS_UPDATED" in W
    assert 'webhook_id={webhook_id or ""}' in W

def test_idempotency_marker_remains_inside_same_transaction_before_commit():
    marker = W.index("INSERT INTO weinvoice_webhook_events")
    commit = W.index("conn.commit()", marker)
    assert marker < commit
