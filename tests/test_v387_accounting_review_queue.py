from pathlib import Path
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos/routes/bank_sync.py").read_text(encoding="utf-8")
T = (ROOT / "templates/banking.html").read_text(encoding="utf-8")


def test_template_parses():
    Environment().parse(T)


def test_review_queue_has_three_explicit_buckets():
    assert "accounting_review_queue = {'stable': [], 'confirm': [], 'anomaly': []}" in B
    assert "accounting_review_queue['stable'].append(item)" in B
    assert "accounting_review_queue['confirm'].append(item)" in B
    assert "accounting_review_queue['anomaly'].append(item)" in B


def test_only_pending_transactions_enter_review_queue():
    assert "transaction_states[t['id']]['state'] != 'pending'" in B


def test_stable_bucket_uses_v386_eligibility_gate():
    assert "if sug.get('automation_eligible')" in B


def test_zero_confidence_or_contradiction_is_anomaly():
    assert "(sug.get('confidence_score') or 0) <= 0" in B
    assert "'contradictoires' in (sug.get('reason') or '').lower()" in B


def test_ui_exposes_queue_and_preserves_human_control():
    assert "File de traitement comptable" in T
    assert "Habitudes stables :" in T
    assert "À confirmer :" in T
    assert "Anomalies / contradictions :" in T
    assert "aucune écriture n'est créée sans votre validation" in T
