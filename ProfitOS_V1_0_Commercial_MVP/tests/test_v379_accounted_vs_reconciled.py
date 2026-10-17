from pathlib import Path
from jinja2 import Environment
ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T=(ROOT/"templates/banking.html").read_text(encoding="utf-8")

def test_v379_template_parses():
    Environment().parse(T)

def test_v379_accounting_does_not_fake_invoice_reconciliation():
    assert "elif accounting_validation:" in B
    assert "state='accounted'" in B
    segment=B.split("allocated=round(float(customer)+float(supplier)+float(fee),2)",1)[1].split("states[tx['id']]",1)[0]
    assert "allocated=total" not in segment

def test_v379_accounted_transactions_remain_reconciliation_candidates():
    assert "state'] in ('pending','accounted')" in B
    assert "for tx in reconciliation_candidates:" in B

def test_v379_accounting_form_only_for_pending():
    assert "if transaction_states[t['id']]['state']=='pending'" in B
    assert "{% if wf.state == 'pending' %}" in T

def test_v379_ui_distinguishes_accounted_from_reconciled():
    assert "wf.state == 'accounted'" in T
    assert "<strong>Comptabilisée</strong>" in T
    assert "Rapprochement facture encore possible" in T

def test_v379_supplier_ui_allows_accounted_candidates():
    assert "in ('pending','accounted')" in T
