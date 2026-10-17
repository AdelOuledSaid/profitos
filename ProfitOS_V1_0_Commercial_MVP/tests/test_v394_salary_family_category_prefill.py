from pathlib import Path

B=(Path(__file__).resolve().parents[1]/"profitos"/"routes"/"bank_sync.py").read_text(encoding="utf-8")


def test_stable_debit_salary_family_prefills_canonical_category():
    assert "directional == 'debit:salaire'" in B
    assert "learned.get('account_code') == '641000'" in B
    assert "learned['category']='Salaires et paie'" in B


def test_salary_family_keeps_existing_safety_guards():
    assert "choices={(r['account_code'], r['vat_rate']) for r in family}" in B
    assert "if len(choices) != 1:" in B
    assert "source in ('directionnelle', 'famille salaire')" in B
    assert "confirmations >= 4 and score >= 90" in B
