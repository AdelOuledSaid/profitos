from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = (ROOT / "templates" / "invoicing_detail.html").read_text(encoding="utf-8")

def test_outbound_weinvoice_status_labels_cover_real_lifecycle():
    expected = {
        "'SUBMITTED':'Transmise'",
        "'SUBMITTED_DATA_REGLEMENTARY':'Transmise'",
        "'RECEIVED':'Reçue'",
        "'MADE_AVAILABLE':'Mise à disposition'",
        "'TAKEN_IN_CHARGE':'Prise en charge'",
        "'APPROVED':'Acceptée'",
        "'PARTIALLY_APPROVED':'Partiellement acceptée'",
        "'APPROVED_PARTIALLY':'Partiellement acceptée'",
        "'DISPUTED':'En litige'",
        "'IN_DISPUTE':'En litige'",
        "'SUSPENDED':'Suspendue'",
        "'COMPLETED':'Complétée'",
        "'REFUSED':'Refusée'",
        "'PAYMENT_SENT':'Paiement transmis'",
        "'PAID':'Payée'",
        "'REJECTED':'Rejetée'",
    }
    for fragment in expected:
        assert fragment in T
