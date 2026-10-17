from pathlib import Path

R = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def _send_block():
    start = R.index("def invoicing_send_weinvoice(")
    end = R.index("def invoicing_sync_weinvoice(", start)
    return R[start:end]


def test_v359_success_is_persisted_only_after_remote_id_validation():
    block = _send_block()
    remote_id = block.index("remote_id =")
    missing_guard = block.index("if not remote_id:", remote_id)
    persist_success = block.index("SET weinvoice_invoice_id=?", missing_guard)

    assert remote_id < missing_guard < persist_success


def test_v359_missing_remote_id_records_failure_without_fake_success():
    block = _send_block()
    start = block.index("if not remote_id:")
    end = block.index("SET weinvoice_invoice_id=?", start)
    missing = block[start:end]

    assert "weinvoice_last_error=?" in missing
    assert "_record_einvoice_event(c,inv,'submission_failed'" in missing
    assert "'submitted'" not in missing
    assert "weinvoice_status=?" not in missing
    assert "weinvoice_invoice_id=?" not in missing


def test_v359_success_records_submitted_only_after_remote_identity_is_saved():
    block = _send_block()
    persist = block.index("SET weinvoice_invoice_id=?")
    event = block.index("_record_einvoice_event(c,inv,'submitted'", persist)
    commit = block.index("c.commit()", event)

    assert persist < event < commit
