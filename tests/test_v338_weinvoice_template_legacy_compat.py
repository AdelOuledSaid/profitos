from pathlib import Path
from jinja2 import Environment

DETAIL = Path('templates/invoicing_detail.html').read_text(encoding='utf-8')

def test_history_keeps_legacy_provider_status_mapping_expression():
    assert "status_labels.get((ev.status or '')|upper" in DETAIL

def test_template_compiles():
    Environment().parse(DETAIL)
