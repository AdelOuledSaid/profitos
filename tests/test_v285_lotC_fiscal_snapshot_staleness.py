from pathlib import Path
T=Path("profitos/routes/accounting.py").read_text(encoding="utf-8")
H=Path("templates/fiscal_workpapers.html").read_text(encoding="utf-8")

def test_validated_fiscal_workpaper_detects_accounting_snapshot_changes():
    block=T.split("def fiscal_workpapers():",1)[1].split("\n    @app.",1)[0]
    assert "existing['status'] == 'validated'" in block
    assert "snapshot_max_entry_id" in block
    assert "debit_total - float(existing['debit_total']" in block
    assert "credit_total - float(existing['credit_total']" in block
    assert "stale=stale" in block

def test_fiscal_workpaper_ui_warns_when_validated_snapshot_is_stale():
    assert "{% if stale %}" in H
    assert "Dossier fiscal à recontrôler." in H
    assert "La validation enregistrée ne doit plus être considérée comme à jour." in H
