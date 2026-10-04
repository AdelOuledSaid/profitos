from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = (ROOT / "templates" / "invoicing_detail.html").read_text(encoding="utf-8")
R = (ROOT / "profitos" / "routes" / "invoicing.py").read_text(encoding="utf-8")


def test_251_and_501_keep_main_business_status_readable():
    assert "'SUBMITTED_DATA_REGLEMENTARY_REJECTED':'Transmise'" in T
    assert "'SUBMITTED_DATA_REGLEMENTARY_INADMISSIBLE':'Transmise'" in T


def test_ppf_acknowledgements_are_logged_separately():
    expected = {
        "'250': 'ACK_250_ACCEPTED'",
        "'251': 'ACK_251_REJECTED'",
        "'500': 'ACK_500_RECEVABLE'",
        "'501': 'ACK_501_INADMISSIBLE'",
        "'601': 'ACK_601_REJECTED'",
    }
    for fragment in expected:
        assert fragment in R
    assert "c, inv, 'acquittement'" in R
    assert "data.get('latestAcquittement')" in R


def test_601_is_presented_as_ack_not_invoice_rejection():
    assert "'ACK_601_REJECTED': 'Statuts obligatoires rejetés'" in T
    assert "'acquittement': 'Acquittement PPF'" in T
    assert "'RECEIVED':'Reçue'" in T
