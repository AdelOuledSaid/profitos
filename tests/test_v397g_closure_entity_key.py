from pathlib import Path
import ast

R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
ast.parse(R)

def test_strict_closure_uses_integer_entity_key():
    block=R.split("def automatic_review_item_statuses",1)[1].split("def latest_review_diagnostics",1)[0]
    assert "entity_key=int(review_entity_id) if review_entity_id is not None else 0" in block
    assert "else 'global'" not in block
