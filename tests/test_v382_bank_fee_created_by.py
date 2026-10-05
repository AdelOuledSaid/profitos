from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
def test_v382_bank_fee_does_not_call_undefined_current_user_email():
    route=B.split("def banking_transaction_book_fee",1)[1]
    assert "current_user_email()" not in route
    assert "session.get('user_email') or session.get('email') or 'system'" in route
def test_v382_keeps_postgres_percent_fix():
    route=B.split("def banking_transaction_book_fee",1)[1]
    assert "LIKE '627%%'" in route
    assert "LIKE '512%%'" in route
