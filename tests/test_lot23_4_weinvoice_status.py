from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_timeline_endpoint_and_invoice_read():
    s=(ROOT/'profitos/weinvoice.py').read_text(encoding='utf-8')
    assert '/v1/invoice-queries/{e_invoicing_id}/timeline' in s
    assert "credential_set='invoicing'" in s
    assert "'X-Org-Id'" in s

def test_manual_sync_route_and_db_fields():
    s=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
    r=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8')
    assert '/weinvoice/synchroniser' in s
    assert 'get_invoice_timeline' in s
    assert 'weinvoice_regulatory_code' in r
    assert 'weinvoice_last_sync_at' in r

def test_signed_invoice_webhook_route():
    s=(ROOT/'profitos/routes/main.py').read_text(encoding='utf-8')
    w=(ROOT/'profitos/weinvoice.py').read_text(encoding='utf-8')
    assert '/webhooks/weinvoice/invoice-status' in s
    assert 'weinvoice_verify_webhook' in s
    assert "event_name.startswith('invoice.status.')" in w
    assert "data.get('eInvoicingId')" in w
