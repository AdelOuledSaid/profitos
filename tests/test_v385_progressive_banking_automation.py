from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
B = (ROOT / "profitos" / "routes" / "bank_sync.py").read_text(encoding="utf-8")
T = (ROOT / "templates" / "banking.html").read_text(encoding="utf-8")


def test_progressive_automation_threshold_is_conservative():
    assert "confirmations >= 4 and score >= 90" in B
    assert "source in ('directionnelle', 'famille salaire')" in B
    assert "automation_eligible=" in B


def test_salary_family_requires_consensus_before_learning():
    # v393 : une famille salaire n'est proposée que si compte + TVA convergent.
    assert "choices={(r['account_code'], r['vat_rate']) for r in family}" in B
    assert "if len(choices) != 1:" in B
    assert "ambiguous=True" in B
    assert "confirmations_total=sum(int(r['confirmations'] or 0) for r in family)" in B
    assert "source='famille salaire'" in B


def test_stable_habit_keeps_human_control():
    assert "Validation humaine requise." in T
    assert "Valider" in T
