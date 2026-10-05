from pathlib import Path


INV = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def _send_block():
    start = INV.index("def invoicing_send_weinvoice(")
    end = INV.index("def invoicing_sync_weinvoice(", start)
    return INV[start:end]


def test_v349_success_response_without_remote_id_is_rejected_before_persistence():
    block = _send_block()

    remote = block.index("remote_id = data.get(")
    missing = block.index("if not remote_id:", remote)
    persist = block.index(
        "UPDATE outgoing_invoices SET weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_sent_at=?",
        remote,
    )

    # La réponse fournisseur est validée avant toute persistance d'un envoi réussi.
    assert remote < missing < persist

    missing_block = block[missing:persist]

    # Une réponse 2xx incomplète doit devenir un échec explicite.
    assert "weinvoice_last_error=?" in missing_block
    assert "_record_einvoice_event(c,inv,'submission_failed'" in missing_block
    assert "c.commit()" in missing_block
    assert "return redirect(" in missing_block

    # Aucun faux identifiant/statut de transmission ne doit être écrit.
    assert "weinvoice_invoice_id=?" not in missing_block
    assert "weinvoice_status=?" not in missing_block
    assert "_record_einvoice_event(c,inv,'submitted'" not in missing_block


def test_v349_success_is_recorded_only_after_nonempty_remote_id():
    block = _send_block()

    remote = block.index("remote_id = data.get(")
    guard = block.index("if not remote_id:", remote)
    persist = block.index(
        "UPDATE outgoing_invoices SET weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_sent_at=?",
        guard,
    )
    submitted = block.index("_record_einvoice_event(c,inv,'submitted'", persist)

    assert remote < guard < persist < submitted
