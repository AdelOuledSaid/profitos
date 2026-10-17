from pathlib import Path


def test_webhook_debug_log_is_present_and_safe():
    src = Path("profitos/weinvoice.py").read_text(encoding="utf-8")
    assert "WEINVOICE_INVOICE_WEBHOOK_DEBUG" in src
    assert "event={event_name}" in src
    assert "status={data.get('status')}" in src
    assert "cdv={data.get('cdvCode')}" in src
    assert "id={data.get('eInvoicingId') or data.get('einvoicingId') or data.get('invoiceId')}" in src
