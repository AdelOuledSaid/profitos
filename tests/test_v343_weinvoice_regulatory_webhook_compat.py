from pathlib import Path

W = Path('profitos/weinvoice.py').read_text(encoding='utf-8')


def test_v341_safe_debug_log_is_preserved():
    assert 'WEINVOICE_INVOICE_WEBHOOK_DEBUG' in W
    assert "f\"event={event_name} \"" in W
    assert "f\"status={data.get('status')} \"" in W


def test_legacy_webhook_status_insert_remains_first_static_insert():
    start = W.index('def handle_invoice_status_webhook')
    first = W.index('INSERT OR IGNORE INTO einvoice_events', start)
    block = W[first:first + 800]
    assert "VALUES(?,?,'weinvoice','webhook_status',?,?,?,?,?,?)" in block


def test_regulatory_ack_support_remains_present():
    assert "'invoice.regulatory.accepted': ('ACK_250_ACCEPTED', '250')" in W
    assert "'invoice.regulatory.rejected': ('ACK_251_REJECTED', '251')" in W
    assert "'acquittement'" in W
