from pathlib import Path

DETAIL = Path("templates/invoicing_detail.html").read_text(encoding="utf-8")

def test_cancelled_220_has_french_label():
    assert "\'CANCELLED\':\'Annulée\'" in DETAIL

def test_cancelled_label_applies_to_main_and_history_mapping():
    assert "status_labels.get((inv.weinvoice_status or '')|upper" in DETAIL
    assert "status_labels.get((ev.status or '')|upper" in DETAIL

def test_template_compiles():
    from jinja2 import Environment
    Environment().parse(DETAIL)
