from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SRC=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_v371_accounting_validation_is_detected():
    block=SRC.split("def _bank_transaction_states",1)[1].split('@app.route("/banking")',1)[0]
    assert "accounting_validation=c.execute" in block
    assert "bank_accounting_validations" in block

def test_v371_validated_accounting_has_distinct_accounted_state():
    # v379 corrects the old semantics: accounting != invoice reconciliation.
    block=SRC.split("def _bank_transaction_states",1)[1].split('@app.route("/banking")',1)[0]
    assert "elif accounting_validation:" in block
    assert "state='accounted'" in block
    assert "allocated=total" not in block

def test_v371_accounted_remains_invoice_matchable():
    assert "state'] in ('pending','accounted')" in SRC
