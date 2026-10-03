from pathlib import Path
W=Path("profitos/weinvoice.py").read_text(encoding="utf-8")

def test_einvoice_event_insert_has_exact_parameter_arity():
    start=W.index("INSERT OR IGNORE INTO einvoice_events", W.index("def handle_invoice_status_webhook"))
    block=W[start:start+800]
    assert "VALUES(?,?,'weinvoice','webhook_status',?,?,?,?,?,?)" in block
    # 10 columns = 2 bound ids + 2 SQL constants + 6 bound event fields.
    values=block[block.index("VALUES("):block.index('"""', block.index("VALUES("))]
    assert values.count("?") == 8
