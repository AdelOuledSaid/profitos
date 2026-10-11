from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BANK=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")

def test_v369_balanced_fee_entry():
    b=BANK.split("def banking_transaction_book_fee",1)[1].split('@app.post("/banking/transaction/<int:tx_id>/ignore")',1)[0]
    # PostgreSQL/psycopg2 requires literal % to be escaped when parameters are bound.
    assert "code LIKE '627%%'" in b and "code LIKE '512%%'" in b
    assert "source_type='bank_fee'" in b
    assert "'bank_fee',tx_id" in b
    assert "residual" in b

def test_v369_fee_route_keeps_small_residual_guards():
    b=BANK.split("def banking_transaction_book_fee",1)[1].split('@app.post("/banking/transaction/<int:tx_id>/ignore")',1)[0]
    assert "residual > 5.00" in b
    assert "reference*0.02" in b
    assert "float(tx['amount'] or 0) >= 0" in b
