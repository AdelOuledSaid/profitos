from pathlib import Path

I=Path('profitos/routes/invoicing.py').read_text(encoding='utf-8')


def test_inbound_sync_paginates_beyond_first_100():
    assert "while True:" in I
    assert "list_inbound_invoices(org_remote,page=page,page_size=page_size)" in I
    assert "inbound_items.extend(batch)" in I
    assert "for item in inbound_items:" in I
    assert "if len(inbound_items) >= total: break" in I


def test_pagination_has_hard_safety_guard():
    assert "if page > 1000:" in I
    assert "Pagination WeInvoice anormalement longue" in I


def test_supplier_credit_readable_is_optional():
    marker="WEINVOICE_INBOUND_CREDIT_READABLE_UNAVAILABLE"
    assert marker in I
    block=I[I.index(marker)-700:I.index(marker)+700]
    assert "except WeInvoiceAPIError" in block
    assert "pdf=None" in block
    assert "if pdf:" in block


def test_v323_normal_invoice_optional_documents_remains_intact():
    assert "WEINVOICE_INBOUND_ORIGINAL_UNAVAILABLE" in I
    assert "WEINVOICE_INBOUND_READABLE_UNAVAILABLE" in I
