from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
T = (ROOT / "templates" / "invoicing_detail.html").read_text(encoding="utf-8")

def test_outbound_weinvoice_212_cashed_in_label():
    assert "'CASHED_IN':'Encaissée'" in T
