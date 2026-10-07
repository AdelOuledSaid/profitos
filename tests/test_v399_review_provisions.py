from pathlib import Path
import ast
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def test_v399_provisions_are_detected_from_ledger():
    assert "PROVISIONS_REVIEW_REQUIRED" in R
    assert "LEFT(l.account_code,2)='15'" in R
    assert "LEFT(l.account_code,4)='6815'" in R
    assert "LEFT(l.account_code,4)='7815'" in R

def test_v399_never_auto_green_provisions():
    block=R.split("# Provisions (point 13)",1)[1].split("# Justificatifs fournisseurs",1)[0]
    assert "'state':'ok'" not in block
    assert "'state':'review'" in block
    assert "Aucune provision comptable détectée" in block
    assert "validation humaine requise" in block
