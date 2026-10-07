from pathlib import Path
import ast
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)
def test_v402_is_read_only_and_entity_scoped():
    assert "INVOICE_ACCOUNTING_INCOMPLETE" in R
    assert "NOT EXISTS" in R
    assert "e.source_id=i.id OR e.reference=i.number" in R
    assert "e.source_id=p.id OR e.reference=p.invoice_number" in R
def test_v402_ui_keeps_human_final_validation():
    block=R.split("# Exhaustivité factures clients/fournisseurs",1)[1].split("# Rapprochement bancaire",1)[0]
    assert "validation finale manuelle" in block
    assert "UPDATE " not in block and "INSERT " not in block and "DELETE " not in block
