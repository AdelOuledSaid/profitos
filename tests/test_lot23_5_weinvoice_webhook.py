from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_invoice_webhook_is_csrf_exempt():
    s=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
    assert '/webhooks/weinvoice/invoice-status' in s

def test_invoice_webhook_passes_webhook_id_for_idempotency():
    s=(ROOT/'profitos/routes/main.py').read_text(encoding='utf-8')
    assert 'weinvoice_handle_invoice_status_webhook(payload,webhook_id=webhook_id)' in s

def test_webhook_has_signature_freshness_and_event_dedupe():
    s=(ROOT/'profitos/weinvoice.py').read_text(encoding='utf-8')
    assert 'tolerance_seconds=300' in s
    assert 'weinvoice_webhook_events' in s
    assert "payload.get('event_id')" in s
    assert '_tenant_connection_for_remote_invoice' in s
