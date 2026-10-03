from pathlib import Path
W=Path("profitos/weinvoice.py").read_text(encoding="utf-8")
B=W[W.index("def handle_invoice_status_webhook"):]

def test_received_webhook_fallback():
    assert "'invoice.status.received': ('RECEIVED', 202)" in B
    assert "status = fallback[0]" in B
    assert "cdv = fallback[1]" in B

def test_deposited_and_rejected_fallbacks():
    assert "'invoice.status.deposited': ('DEPOSITED', 200)" in B
    assert "'invoice.status.rejected': ('REJECTED', 213)" in B

def test_remote_id_variants():
    assert "data.get('eInvoicingId')" in B
    assert "data.get('einvoicingId')" in B
    assert "data.get('invoiceId')" in B
