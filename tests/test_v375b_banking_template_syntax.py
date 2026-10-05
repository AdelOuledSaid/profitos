from pathlib import Path
from jinja2 import Environment
T=(Path(__file__).resolve().parents[1]/"templates"/"banking.html").read_text(encoding="utf-8")

def test_v375b_banking_template_compiles():
    Environment().parse(T)

def test_v375b_bank_fee_manual_action_is_preserved():
    assert "Comptabiliser en frais bancaire" in T
    assert "banking_transaction_book_fee" in T
    assert 'name="csrf_token"' in T
