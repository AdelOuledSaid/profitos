from pathlib import Path

R = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def test_v351_ppf_ack_mapping_restored():
    expected = {
        "'250': 'ACK_250_ACCEPTED'",
        "'251': 'ACK_251_REJECTED'",
        "'500': 'ACK_500_RECEVABLE'",
        "'501': 'ACK_501_INADMISSIBLE'",
        "'601': 'ACK_601_REJECTED'",
    }
    for fragment in expected:
        assert fragment in R
    assert "data.get('latestAcquittement')" in R
    # Compatibilité avec le contrat historique v336.
    assert "c, inv, 'acquittement'" in R


def test_v351_missing_remote_id_failure_audit_is_preserved():
    start = R.index("def invoicing_send_weinvoice(")
    end = R.index("def invoicing_sync_weinvoice(", start)
    block = R[start:end]
    remote = block.index("remote_id = data.get(")
    guard = block.index("if not remote_id:", remote)
    persist = block.index(
        "UPDATE outgoing_invoices SET weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_sent_at=?",
        guard,
    )
    failure = block[guard:persist]
    assert "weinvoice_last_error=?" in failure
    assert "_record_einvoice_event(c,inv,'submission_failed'" in failure
    assert "weinvoice_invoice_id=?" not in failure
    assert "weinvoice_status=?" not in failure
