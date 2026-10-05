from pathlib import Path
from jinja2 import Environment
ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def test_v380_template_parses():
    Environment().parse(T)

def test_v380_state_exposes_fee_eligibility_after_allocation():
    assert "fee_eligible=(" in B
    assert "float(tx['amount'] or 0) < 0" in B
    assert "allocated > .005" in B
    assert "remaining > .005" in B
    assert "remaining <= 5.00" in B
    assert "round(allocated*0.02,2)" in B
    assert "'fee_eligible':fee_eligible" in B

def test_v380_transaction_row_keeps_fee_action_after_match_suggestion_disappears():
    assert "{% if wf.fee_eligible %}" in T
    assert "banking_transaction_book_fee" in T
    assert "Comptabiliser l'écart en frais bancaire" in T
