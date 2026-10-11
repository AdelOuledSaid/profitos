from pathlib import Path
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")

def test_review_account_prefixes_avoid_psycopg_percent_placeholders():
    assert "LEFT(l.account_code,3)='471'" in R
    assert "LEFT(l.account_code,3) IN ('401','411')" in R
    block=R.split("def run_review_diagnostics",1)[1].split("def automatic_review_item_statuses",1)[0]
    assert "account_code LIKE '471" not in block
    assert "account_code LIKE '401" not in block
    assert "account_code LIKE '411" not in block
