from profitos.db import _translate_statement, _sqlite_placeholders_to_pg, _translate_ddl

def test_placeholders():
    assert _sqlite_placeholders_to_pg("SELECT * FROM t WHERE a=? AND b=?") == "SELECT * FROM t WHERE a=%s AND b=%s"

def test_last_insert():
    assert _translate_statement("SELECT last_insert_rowid()") == "SELECT lastval() AS id"

def test_scalar_max():
    assert "GREATEST(" in _translate_statement("SELECT MAX(amount-paid_amount,0) x FROM invoices")

def test_ddl_serial():
    assert "SERIAL PRIMARY KEY" in _translate_ddl("CREATE TABLE x(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT)")
