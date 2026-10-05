from pathlib import Path

R = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def _sync_block():
    start = R.index("def invoicing_sync_weinvoice(")
    # stop at next route after sync, or EOF
    marker = "\n    @app.route("
    end = R.find(marker, start + 10)
    return R[start:] if end == -1 else R[start:end]


def test_v358_sync_requires_existing_remote_id_before_timeline_call():
    block = _sync_block()
    guard = block.index("if not remote_id:")
    timeline = block.index("get_invoice_timeline(")
    assert guard < timeline


def test_v358_sync_without_remote_id_does_not_call_provider_or_mutate_status():
    block = _sync_block()
    guard = block.index("if not remote_id:")
    timeline = block.index("get_invoice_timeline(")
    missing_block = block[guard:timeline]

    assert "return redirect(" in missing_block
    assert "weinvoice_status=?" not in missing_block
    assert "weinvoice_regulatory_code=?" not in missing_block
    assert "_record_einvoice_event" not in missing_block


def test_v358_successful_sync_clears_old_error_only_after_timeline_success():
    block = _sync_block()
    timeline = block.index("get_invoice_timeline(")
    update = block.index("weinvoice_status=?", timeline)
    clear_error = block.index("weinvoice_last_error=NULL", update)

    assert timeline < update < clear_error
