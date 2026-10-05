from pathlib import Path

R = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def _send_block():
    start = R.index("def invoicing_send_weinvoice(")
    end = R.index("def invoicing_sync_weinvoice(", start)
    return R[start:end]


def test_v356_existing_remote_id_blocks_before_real_provider_submit():
    block = _send_block()
    guard = block.index("if 'weinvoice_invoice_id' in inv.keys() and inv['weinvoice_invoice_id']:")
    submit = block.index("submit_invoice_file(")
    assert guard < submit


def test_v356_guard_does_not_clear_remote_identity():
    block = _send_block()
    submit = block.index("submit_invoice_file(")
    before_submit = block[:submit]
    assert "weinvoice_invoice_id=NULL" not in before_submit
    assert "weinvoice_invoice_id=''" not in before_submit


def test_v356_submit_reuses_persisted_idempotency_key():
    block = _send_block()
    idem = block.index("inv['weinvoice_idempotency_key']")
    persist = block.index("SET weinvoice_idempotency_key=?", idem)
    submit = block.index("submit_invoice_file(", persist)
    submit_call = block[submit:submit + 500]
    assert idem < persist < submit
    assert ", idem)" in submit_call
