from pathlib import Path

R = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def _send_block():
    start = R.index("def invoicing_send_weinvoice(")
    end = R.index("def invoicing_sync_weinvoice(", start)
    return R[start:end]


def test_v357_idempotency_key_is_persisted_before_provider_call():
    block = _send_block()
    persist = block.index("SET weinvoice_idempotency_key=?")
    commit = block.index("c.commit()", persist)
    submit = block.index("submit_invoice_file(", commit)
    assert persist < commit < submit


def test_v357_existing_idempotency_key_is_reused():
    block = _send_block()
    assert "inv['weinvoice_idempotency_key']" in block
    assert " or f\"profitos-" in block
    submit = block.index("submit_invoice_file(")
    call = block[submit:submit + 600]
    assert ", idem)" in call


def test_v357_provider_failure_does_not_clear_idempotency_key():
    block = _send_block()
    start = block.index("except (WeInvoiceAPIError, WeInvoiceConfigError) as e:")
    end = block.index("remote_id =", start)
    error_block = block[start:end]

    assert "weinvoice_last_error=?" in error_block
    assert "weinvoice_idempotency_key=NULL" not in error_block
    assert "weinvoice_idempotency_key=''" not in error_block
    assert "weinvoice_invoice_id=?" not in error_block
    assert "_record_einvoice_event(c,inv,'submission_failed'" in error_block
