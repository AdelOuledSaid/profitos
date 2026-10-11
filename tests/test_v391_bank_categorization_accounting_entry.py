from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
def R(): return B.split("def banking_transaction_categorize(tx_id):",1)[1]
def test_state_requires_entry(): assert "e.source_type='bank_categorization'" in B and "e.source_id=v.bank_transaction_id" in B
def test_bq_entry(): assert "VALUES('BQ'" in R() and "'bank_categorization',tx_id" in R() and "BANKCAT-{tx_id}" in R()
def test_balanced_direction():
 r=R(); assert "first=(account_code,amount,0.0); second=(bank_account['code'],0.0,amount)" in r and "first=(bank_account['code'],amount,0.0); second=(account_code,0.0,amount)" in r
def test_512_safe(): assert "code LIKE '512%%'" in R()
def test_atomic(): assert R().count("c.commit()") == 1 and "c.rollback()" in R()
def test_idempotent_precheck(): assert "source_type='bank_categorization' AND source_id=?" in R() and "déjà comptabilisée" in R()
def test_orphan_repair_no_double_learning(): assert "if signature and not existing_validation:" in R()
