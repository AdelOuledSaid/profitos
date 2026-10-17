from pathlib import Path
SRC=(Path(__file__).resolve().parents[1]/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")

def test_v374_bank_fee_like_percent_is_psycopg2_safe():
    b=SRC.split("def _bank_transaction_states",1)[1].split("@app.route(\"/banking\")",1)[0]
    assert "LIKE '627%%'" in b
    assert "LIKE '627%'" not in b.replace("LIKE '627%%'", "")
