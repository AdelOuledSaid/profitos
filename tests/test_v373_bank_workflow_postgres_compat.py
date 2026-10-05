from pathlib import Path
SRC=(Path(__file__).resolve().parents[1]/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")

def test_v373_request_path_has_no_sqlite_workflow_ddl():
    b=SRC.split("def _ensure_bank_workflow_table",1)[1].split("def _bank_transaction_states",1)[0]
    assert 'c.execute("""CREATE TABLE' not in b
    assert "AUTOINCREMENT" not in b
    assert "return None" in b

def test_v373_workflow_logic_is_preserved():
    b=SRC.split("def _bank_transaction_states",1)[1].split("@app.route(\"/banking\")",1)[0]
    assert "_ensure_bank_workflow_table(c)" in b
    assert "bank_transaction_workflow" in b
    assert "bank_accounting_validations" in b
