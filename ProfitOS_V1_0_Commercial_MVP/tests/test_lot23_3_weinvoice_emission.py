from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_lot233_api_contract():
    w=(ROOT/'profitos/weinvoice.py').read_text(encoding='utf-8')
    assert 'def submit_invoice_file(' in w and 'X-Org-Id' in w and 'Idempotency-Key' in w and '/v1/invoices' in w and 'files=files' in w
def test_lot233_persistence_and_route():
    r=(ROOT/'profitos/runtime.py').read_text(encoding='utf-8'); i=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8'); t=(ROOT/'templates/invoicing_detail.html').read_text(encoding='utf-8')
    for col in ('weinvoice_invoice_id','weinvoice_status','weinvoice_sent_at','weinvoice_last_error','weinvoice_idempotency_key'): assert col in r
    assert 'def invoicing_send_weinvoice(' in i and 'render_facturx_pdf(inv, company_row)' in i and 'weinvoice_idempotency_key' in i and 'Transmettre à WeInvoice' in t
def test_lot233_duplicate_guard():
    i=(ROOT/'profitos/routes/invoicing.py').read_text(encoding='utf-8')
    assert "if 'weinvoice_invoice_id' in inv.keys() and inv['weinvoice_invoice_id']" in i and 'Facture déjà transmise à WeInvoice' in i
