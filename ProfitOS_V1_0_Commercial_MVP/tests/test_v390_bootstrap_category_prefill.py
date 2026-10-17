from pathlib import Path
from jinja2 import Environment

ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def test_template_parses():
    Environment().parse(T)

def test_suggested_category_is_rendered_even_if_not_in_configured_list():
    assert "sug.category not in categories" in T
    assert '<option value="{{ sug.category }}" selected>{{ sug.category }}</option>' in T

def test_existing_configured_category_selection_still_works():
    assert "sug.category==cat" in T

def test_salary_keeps_safe_account_and_no_vat_guess():
    block=B.split("def _bootstrap_accounting_suggestion(tx):",1)[1].split("def _accounting_suggestion",1)[0]
    assert "'641000'" in block
    assert "vat_rate=None" in block
