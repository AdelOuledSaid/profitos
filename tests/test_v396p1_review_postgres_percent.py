from pathlib import Path
R=(Path(__file__).resolve().parents[1]/"profitos"/"reviews.py").read_text(encoding="utf-8")
def test_review_like_wildcards_are_escaped_for_postgres_adapter():
    for x in ("471%%","401%%","411%%"):
        assert "LIKE '"+x+"'" in R
