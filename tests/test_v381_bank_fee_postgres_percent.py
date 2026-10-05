from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
B=(ROOT/"profitos/routes/bank_sync.py").read_text(encoding="utf-8")
def test_v381_book_fee_escapes_postgres_like_percent():
    route=B.split("def banking_transaction_book_fee",1)[1]
    assert "LIKE '627%%'" in route
    assert "LIKE '512%%'" in route
    assert "LIKE '627%'" not in route.replace("LIKE '627%%'","")
    assert "LIKE '512%'" not in route.replace("LIKE '512%%'","")
