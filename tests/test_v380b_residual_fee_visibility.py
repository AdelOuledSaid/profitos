from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T = (ROOT / "templates/banking.html").read_text(encoding="utf-8")


def test_template():
    Environment().parse(T)


def test_fee_visibility_uses_actual_allocated_amount():
    block = B.split("fee_eligible=(", 1)[1].split("states[tx['id']]", 1)[0]
    assert "allocated > .005" in block
    assert "float(supplier) > .005" not in block
    assert "round(allocated*0.02,2)" in B


def test_fee_button_on_transaction_row():
    assert "{% if wf.fee_eligible %}" in T
    assert "Comptabiliser l'écart en frais bancaire" in T
