from pathlib import Path
A=Path("profitos/accounting.py").read_text(encoding="utf-8")
C=Path("profitos/routes/cloud_storage.py").read_text(encoding="utf-8")

def test_purchase_entry_can_join_caller_transaction():
    b=A.split("def generate_purchase_entry",1)[1].split("\ndef generate_purchase_payment_entry",1)[0]
    assert "(conn, purchase, commit=True):" in b
    assert "commit=commit" in b

def test_cloud_import_commits_purchase_accounting_and_marker_atomically():
    b=C.split("def cloud_storage_sync(provider):",1)[1]
    assert "generate_purchase_entry(c, purchase_row, commit=False)" in b
    marker="INSERT INTO cloud_storage_imported_files(connection_id,provider_file_id,purchase_invoice_id,imported_at)"
    assert marker in b
    assert b.index("generate_purchase_entry(c, purchase_row, commit=False)") < b.index(marker) < b.index("c.commit()", b.index(marker))

def test_cloud_import_rolls_back_purchase_when_accounting_fails():
    b=C.split("def cloud_storage_sync(provider):",1)[1]
    assert "except AccountingError as e:" in b
    assert "c.rollback()" in b
    assert "path.unlink(missing_ok=True)" in b
    assert "continue" in b
