from pathlib import Path
from jinja2 import Environment


def test_invoicing_detail_jinja_syntax_compiles():
    root = Path(__file__).resolve().parents[1]
    source = (root / "templates" / "invoicing_detail.html").read_text(encoding="utf-8")
    Environment().parse(source)


def test_ppf_ack_labels_remain_present():
    root = Path(__file__).resolve().parents[1]
    source = (root / "templates" / "invoicing_detail.html").read_text(encoding="utf-8")
    for token in ("ACK_251_REJECTED", "ACK_501_INADMISSIBLE", "ACK_601_REJECTED"):
        assert token in source
