from pathlib import Path

T = Path('profitos/routes/invoicing.py').read_text(encoding='utf-8')


def test_inbound_content_remains_mandatory():
    assert 'content=get_inbound_invoice_content(org_remote,remote_id)' in T


def test_original_file_is_optional_for_normal_inbound_invoice():
    marker = "raw_original=b''"
    assert marker in T
    block = T[T.index(marker):T.index("subtotal=float", T.index(marker))]
    assert 'download_inbound_invoice_original(org_remote,remote_id)' in block
    assert 'except WeInvoiceAPIError as exc:' in block
    assert 'WEINVOICE_INBOUND_ORIGINAL_UNAVAILABLE' in block


def test_readable_pdf_is_optional_for_normal_inbound_invoice():
    start = T.index("if _is_inbound_credit_note(item,raw_original):")
    normal = T.index("WEINVOICE_INBOUND_READABLE_UNAVAILABLE", start)
    block = T[normal-500:normal+500]
    assert 'download_inbound_invoice_readable(org_remote,remote_id)' in block
    assert 'pdf=None' in block
    assert 'if pdf:' in block


def test_missing_documents_do_not_increment_failed_directly():
    assert "WEINVOICE_INBOUND_ORIGINAL_UNAVAILABLE','WARNING'" in T
    assert "WEINVOICE_INBOUND_READABLE_UNAVAILABLE','WARNING'" in T
    assert 'failed.append(f"{remote_id}: {type(exc).__name__}")' in T
