from pathlib import Path
import ast
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def test_v400_detects_doubtful_receivable_accounts():
    assert "DOUBTFUL_RECEIVABLES_REVIEW" in R
    assert "LEFT(l.account_code,3)='416'" in R
    assert "LEFT(l.account_code,3)='491'" in R
    assert "LEFT(l.account_code,5)='68174'" in R
    assert "LEFT(l.account_code,5)='78174'" in R

def test_v400_never_auto_green_doubtful_receivables():
    block=R.split("# Créances douteuses / litigieuses (point 14)",1)[1].split("# Justificatifs fournisseurs",1)[0]
    assert "'state':'ok'" not in block
    assert "'state':'review'" in block
    assert "ne peuvent pas être exclus automatiquement" in block
