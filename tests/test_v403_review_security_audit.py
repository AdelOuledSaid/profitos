from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]
R=(ROOT/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def auto_block():
    return R.split("def automatic_review_item_statuses",1)[1].split("def latest_review_diagnostics",1)[0]

def diagnostic_block():
    return R.split("def run_review_diagnostics",1)[1].split("def automatic_review_item_statuses",1)[0]

def test_v403_auto_status_is_read_only():
    b=auto_block()
    for sql in ("UPDATE review_items","INSERT INTO review_items","DELETE FROM review_items",
                "UPDATE bank_transactions","INSERT INTO accounting_entries",
                "UPDATE accounting_entries","DELETE FROM accounting_entries"):
        assert sql not in b

def test_v403_human_only_sensitive_items_never_green():
    b=auto_block()
    for start,end in [
        ("# Provisions (point 13)","# Créances douteuses / litigieuses (point 14)"),
        ("# Créances douteuses / litigieuses (point 14)","# Justificatifs fournisseurs"),
    ]:
        part=b.split(start,1)[1].split(end,1)[0]
        assert "'state':'ok'" not in part
        assert "'state':'review'" in part

def test_v403_closure_requires_real_closed_until():
    b=auto_block()
    assert "SELECT closed_until FROM accounting_entity_closure" in b
    assert "closed_until" in b
    assert "year_end" in b
    assert "Période à clôturer" in b

def test_v403_entity_scoping_present_on_critical_checks():
    d=diagnostic_block()
    assert "entity_id" in d
    assert "JOIN bank_accounts a" in d
    assert "a.entity_id" in d
    assert "i.entity_id" in d
    assert "p.entity_id" in d

def test_v403_bank_and_invoice_checks_are_detection_only():
    d=diagnostic_block()
    bank=d.split("# v401",1)[1].split("# v402",1)[0]
    inv=d.split("# v402",1)[1].split("entity_key=",1)[0]
    for part in (bank,inv):
        assert "UPDATE " not in part
        assert "DELETE " not in part
        assert "INSERT INTO " not in part

def test_v403_no_false_green_for_provisions_or_doubtful_receivables():
    b=auto_block()
    assert "Aucune provision comptable détectée" in b
    assert "ne peuvent pas être exclus automatiquement" in b

def test_v403_final_review_remains_manual():
    # Automatic status helper must never check checklist items.
    assert "UPDATE review_items" not in auto_block()
