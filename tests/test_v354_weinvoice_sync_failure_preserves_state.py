from pathlib import Path

R = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def _sync_block():
    start = R.index("def invoicing_sync_weinvoice(")
    end = R.index("def invoicing_test_weinvoice_webhook_sandbox(", start)
    return R[start:end]


def test_v354_timeline_failure_only_records_last_error():
    block = _sync_block()
    call = block.index("data = get_invoice_timeline(")
    exc = block.index("except (WeInvoiceAPIError, WeInvoiceConfigError) as e:", call)
    redirect = block.index("return redirect(", exc)
    error_block = block[exc:redirect]

    assert "weinvoice_last_error=?" in error_block
    assert "c.commit()" in error_block

    # Une erreur fournisseur ne doit jamais écraser le dernier état valide.
    assert "weinvoice_status=?" not in error_block
    assert "weinvoice_regulatory_code=?" not in error_block
    assert "_record_einvoice_event" not in error_block


def test_v354_status_is_updated_only_after_successful_timeline_response():
    block = _sync_block()
    call = block.index("data = get_invoice_timeline(")
    exc = block.index("except (WeInvoiceAPIError, WeInvoiceConfigError) as e:", call)
    success_update = block.index(
        "UPDATE outgoing_invoices SET weinvoice_status=?,weinvoice_regulatory_code=?",
        exc,
    )
    assert call < exc < success_update


def test_v354_provider_error_does_not_clear_existing_remote_id():
    block = _sync_block()
    call = block.index("data = get_invoice_timeline(")
    exc = block.index("except (WeInvoiceAPIError, WeInvoiceConfigError) as e:", call)
    redirect = block.index("return redirect(", exc)
    error_block = block[exc:redirect]

    assert "weinvoice_invoice_id=?" not in error_block
    assert "weinvoice_invoice_id=NULL" not in error_block
