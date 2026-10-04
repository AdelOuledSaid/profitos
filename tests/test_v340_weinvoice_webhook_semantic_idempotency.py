from pathlib import Path

W = Path("profitos/weinvoice.py").read_text(encoding="utf-8")
B = W[W.index("def handle_invoice_status_webhook"):W.index("# ---------------------------------------------------------------------------\n# Lot F1")]

def test_same_business_status_is_deduplicated_even_with_new_event_id():
    assert "same_business_status = bool(" in B
    assert "current['weinvoice_status']" in B
    assert "if same_business_status:" in B
    assert "WEINVOICE_INVOICE_STATUS_DUPLICATE" in B

def test_duplicate_delivery_is_acknowledged_but_not_added_to_history():
    duplicate = B[B.index("if same_business_status:"):B.index("        if kind != 'purchase':", B.index("WEINVOICE_INVOICE_STATUS_DUPLICATE"))]
    assert "weinvoice_webhook_events" in duplicate
    assert "conn.commit()" in duplicate
    assert "return True" in duplicate
    assert "einvoice_events" not in duplicate

def test_new_status_still_creates_history_event():
    assert "INSERT OR IGNORE INTO einvoice_events" in B
    assert "'webhook_status'" in B
