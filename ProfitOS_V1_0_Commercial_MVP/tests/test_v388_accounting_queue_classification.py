from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T = (ROOT / "templates/banking.html").read_text(encoding="utf-8")


def test_template_parses():
    Environment().parse(T)


def test_queue_has_four_distinct_buckets():
    assert "accounting_review_queue = {'stable': [], 'confirm': [], 'no_suggestion': [], 'contradiction': []}" in B


def test_true_contradiction_is_separated_from_missing_suggestion():
    assert "accounting_review_queue['contradiction'].append(item)" in B
    assert "accounting_review_queue['no_suggestion'].append(item)" in B
    assert "elif (sug.get('confidence_score') or 0) <= 0:" in B


def test_contradiction_is_checked_before_zero_confidence():
    contradiction = B.index("accounting_review_queue['contradiction'].append(item)")
    no_suggestion = B.index("accounting_review_queue['no_suggestion'].append(item)")
    assert contradiction < no_suggestion


def test_ui_names_four_categories_clearly():
    assert "Habitudes stables :" in T
    assert "À confirmer :" in T
    assert "Sans suggestion :" in T
    assert "Contradictions :" in T
    assert "Anomalies / contradictions :" not in T


def test_no_suggestion_is_not_described_as_an_anomaly():
    assert "accounting_review_queue.no_suggestion|length" in T
    assert "accounting_review_queue.contradiction|length" in T
