from pathlib import Path

R = Path("profitos/routes/invoicing.py").read_text(encoding="utf-8")


def test_v352_ppf_event_type_keeps_v336_contract():
    assert "c, inv, 'acquittement'" in R
    for fragment in (
        "'250': 'ACK_250_ACCEPTED'",
        "'251': 'ACK_251_REJECTED'",
        "'500': 'ACK_500_RECEVABLE'",
        "'501': 'ACK_501_INADMISSIBLE'",
        "'601': 'ACK_601_REJECTED'",
    ):
        assert fragment in R


def test_v352_missing_remote_id_audit_remains_present():
    start = R.index("def invoicing_send_weinvoice(")
    end = R.index("def invoicing_sync_weinvoice(", start)
    block = R[start:end]
    guard = block.index("if not remote_id:", block.index("remote_id = data.get("))
    persist = block.index(
        "UPDATE outgoing_invoices SET weinvoice_invoice_id=?,weinvoice_status=?,weinvoice_sent_at=?",
        guard,
    )
    failure = block[guard:persist]
    assert "_record_einvoice_event(c,inv,'submission_failed'" in failure
    assert "weinvoice_invoice_id=?" not in failure
    assert "weinvoice_status=?" not in failure
