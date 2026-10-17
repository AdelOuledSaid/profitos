from pathlib import Path

T = Path("templates/invoicing_detail.html").read_text(encoding="utf-8")


def test_outbound_cashed_in_ack_variants_keep_business_label():
    for status in ("CASHED_IN", "CASHED_IN_ACCEPTED", "CASHED_IN_REJECTED", "CASHED_IN_INADMISSIBLE"):
        assert f"\'{status}\':\'Encaissée\'" in T
